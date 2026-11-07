"""Supervise the Python device client from the Hermes plugin process.

``plugin.register()`` asks this module to start the controlled-computer side
of Hermes-Relay when the install's role is ``client`` or ``both``.  The relay
host lives in :mod:`plugin.relay.embedded`; this module is its client-side
counterpart and deliberately uses only the standard library so importing it
never pulls the server/plugin dependency stack into a client install.

Two layouts have to work with one package:

* repo checkout — ``<repo>/plugin/relay/device``; the child runs
  ``python -m plugin.relay.device`` with the repo root as cwd so the ``plugin``
  package is importable.
* installed plugin — ``~/.hermes/plugins/hermes-relay/relay/device``; there is
  no ``plugin`` package on ``sys.path``, so the child runs
  ``python -m relay.device`` with the plugin folder as cwd.

The layout is derived from ``Path(__file__)`` by :func:`detect_client_layout`,
never from a hardcoded prefix.  ``relay.device`` may only be imported with the
plugin folder on ``sys.path`` (which ``python -m`` does for its cwd).

Supervision contract:

* one child per plugin process; a live pid file or a live ``_process`` is
  treated as "already running" and no second child is spawned.
* the child is detached, logs to ``$HERMES_HOME/logs/hermes-relay-device.log``,
  and its pid is recorded in ``$HERMES_HOME/relay/hermes-relay-device.pid``.
* a child spawned by this process is terminated when the plugin exits.
* without a stored, unexpired session token the child is *not* started; the
  reason is logged once with the exact pairing command to run.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .identity import (
    ClientIdentity,
    IdentityError,
    hermes_home,
    normalize_relay_url,
)
from .settings import ConfigError, RoleSettings, resolve_settings

logger = logging.getLogger(__name__)

LOG_FILE_NAME = "hermes-relay-device.log"
PID_FILE_NAME = "hermes-relay-device.pid"

REPO_MODULE = "plugin.relay.device"
INSTALLED_MODULE = "relay.device"

#: ``subprocess`` is module-level so tests can substitute a fake module without
#: patching the global ``subprocess`` object (which other tests may be using).
_process: subprocess.Popen[bytes] | None = None
_atexit_registered = False


@dataclass(frozen=True)
class ClientLayout:
    """How to import and launch ``relay.device`` from this install."""

    module: str
    cwd: Path
    plugin_dir: Path

    @property
    def is_repo_layout(self) -> bool:
        return self.module == REPO_MODULE


@dataclass(frozen=True)
class DeviceLaunchPlan:
    """A validated child launch, ready for ``subprocess.Popen``."""

    argv: tuple[str, ...]
    cwd: Path
    module: str
    log_path: Path
    state_path: Path
    url: str


@dataclass(frozen=True)
class DeviceLaunchDecision:
    """Either a launch plan or the reason the client must not start."""

    plan: DeviceLaunchPlan | None
    reason: str
    url: str | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.plan is not None


@dataclass(frozen=True)
class DeviceStartResult:
    """Outcome of :func:`start_device_client` for logs and tests."""

    started: bool
    reason: str
    pid: int | None = None
    url: str | None = None


def plugin_package_dir() -> Path:
    """Directory of the plugin package (``plugin/`` or ``hermes-relay/``)."""
    return Path(__file__).resolve().parents[2]


def detect_client_layout(plugin_dir: Path | str | None = None) -> ClientLayout:
    """Derive the module name and cwd the child needs for this install.

    A folder literally named ``plugin`` is a repo-style checkout (or a symlink
    to one): its parent is the import root for ``plugin.relay.device``.  Any
    other folder is the installed plugin root; its own path is the import root
    for ``relay.device``.  ``Path(__file__).resolve()`` follows symlinks, so a
    development symlink at ``~/.hermes/plugins/hermes-relay`` resolves to the
    repo checkout and uses the repo form.
    """
    base = (
        Path(plugin_dir).expanduser().resolve()
        if plugin_dir is not None
        else plugin_package_dir()
    )
    if base.name == "plugin":
        return ClientLayout(module=REPO_MODULE, cwd=base.parent, plugin_dir=base)
    return ClientLayout(module=INSTALLED_MODULE, cwd=base, plugin_dir=base)


def device_log_path(*, create: bool = False) -> Path:
    """``$HERMES_HOME/logs/hermes-relay-device.log``."""
    logs = hermes_home() / "logs"
    if create:
        try:
            logs.mkdir(parents=True, exist_ok=True)
        except OSError:  # pragma: no cover - unwritable home is reported at spawn
            logger.warning("Cannot create relay log directory %s", logs, exc_info=True)
    return logs / LOG_FILE_NAME


def device_pid_path() -> Path:
    """``$HERMES_HOME/relay/hermes-relay-device.pid``."""
    return hermes_home() / "relay" / PID_FILE_NAME


def build_device_argv(
    settings: RoleSettings,
    *,
    layout: ClientLayout | None = None,
    python: str | None = None,
    workdir: Path | str | None = None,
) -> tuple[str, ...]:
    """Build the argv for the detached device client child.

    ``--cwd`` is passed explicitly: the child's process cwd is the plugin
    directory (required for ``-m`` imports), but desktop tool handlers should
    run somewhere useful such as the user's home directory.
    """
    resolved = layout or detect_client_layout()
    executable = str(python or sys.executable)
    argv = [executable, "-m", resolved.module, "run"]
    if settings.relay_url:
        argv.extend(["--url", settings.relay_url])
    argv.extend(["--state", str(settings.client_state)])
    argv.extend(["--cwd", str(Path(workdir).expanduser() if workdir else Path.home())])
    return tuple(argv)


def _target_relay_url(
    settings: RoleSettings, identity: ClientIdentity
) -> tuple[str | None, str]:
    """Resolve the URL whose stored token gates the launch.

    Returns ``(url, reason)``; ``reason`` is ``"ok"``, ``"no-url"`` or
    ``"invalid-url"``.  A configured ``relay_url`` wins; otherwise the active
    stored pairing supplies the URL, matching ``relay.device run``.
    """
    if settings.relay_url:
        try:
            return normalize_relay_url(settings.relay_url), "ok"
        except IdentityError as exc:
            return None, f"invalid-url:{exc}"
    active = identity.active_record()
    if active is None:
        return None, "no-url"
    return active[0], "ok"


def plan_device_launch(
    settings: RoleSettings,
    *,
    identity: ClientIdentity | None = None,
    layout: ClientLayout | None = None,
    python: str | None = None,
    workdir: Path | str | None = None,
    validate_state: bool = True,
) -> DeviceLaunchDecision:
    """Decide whether this role/settings pair may start the device client.

    ``validate_state=False`` skips token loading; it exists for diagnostics and
    tests that only exercise argv/layout construction.
    """
    if not settings.runs_client:
        return DeviceLaunchDecision(None, "not-client", detail=settings.role)

    if validate_state:
        try:
            current = identity if identity is not None else ClientIdentity.load(
                settings.client_state
            )
        except IdentityError as exc:
            return DeviceLaunchDecision(None, "state-unreadable", detail=str(exc))
        url, reason = _target_relay_url(settings, current)
        if url is None:
            return DeviceLaunchDecision(
                None, reason.split(":", 1)[0], detail=reason.partition(":")[2]
            )
        record = current.record_for(url)
        if record is None:
            return DeviceLaunchDecision(None, "no-token", url=url)
        if record.is_expired():
            return DeviceLaunchDecision(None, "token-expired", url=url)
    else:
        try:
            url = (
                normalize_relay_url(settings.relay_url)
                if settings.relay_url
                else None
            )
        except IdentityError as exc:
            return DeviceLaunchDecision(None, "invalid-url", detail=str(exc))
        if url is None:
            return DeviceLaunchDecision(None, "no-url")

    resolved_layout = layout or detect_client_layout()
    plan = DeviceLaunchPlan(
        argv=build_device_argv(
            settings,
            layout=resolved_layout,
            python=python,
            workdir=workdir,
        ),
        cwd=resolved_layout.cwd,
        module=resolved_layout.module,
        log_path=device_log_path(),
        state_path=Path(settings.client_state),
        url=url,
    )
    return DeviceLaunchDecision(plan, "ok", url=url)


def startup_message(reason: str, settings: RoleSettings, detail: str = "") -> str:
    """Human-readable, actionable explanation for a refused launch."""
    state = settings.client_state
    if reason == "no-token":
        return (
            f"Hermes-Relay role={settings.role} but no stored session token in "
            f"{state}; the device client was not started. Pair first: "
            f"`hermes-relay-client pair --url <relay-url> --code <code>` "
            f"(or `python -m relay.device pair ...` / `hermes relay client pair ...`)."
        )
    if reason == "token-expired":
        return (
            f"Hermes-Relay role={settings.role} but the stored session token in "
            f"{state} has expired; the device client was not started. Re-pair with "
            f"`hermes-relay-client pair --code <code>`."
        )
    if reason == "no-url":
        return (
            f"Hermes-Relay role={settings.role} but no relay URL is configured and "
            "the client store has no active pairing; the device client was not "
            "started. Set HERMES_RELAY_URL (or relay_url in "
            "plugins.entries.hermes-relay.settings) and pair first."
        )
    if reason == "state-unreadable":
        return (
            f"Hermes-Relay role={settings.role} but the client store at {state} "
            f"cannot be read ({detail or 'unknown error'}); the device client was "
            "not started. Move the file aside and re-pair."
        )
    if reason == "invalid-url":
        return (
            f"Hermes-Relay role={settings.role} but relay_url is not a ws:// or "
            f"wss:// URL ({detail or 'invalid'}); the device client was not started."
        )
    return f"Hermes-Relay role={settings.role}: device client not started ({reason})."


# ── pid bookkeeping ─────────────────────────────────────────────────────────


def _read_pid_file() -> dict | None:
    path = device_pid_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    pid = raw.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return None
    return raw


def _write_pid_file(pid: int, url: str) -> None:
    path = device_pid_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"pid": pid, "url": url, "started_at": time.time()}) + "\n",
            encoding="utf-8",
        )
        os.chmod(path, 0o600)
    except OSError:
        logger.warning("Could not record device client pid at %s", path, exc_info=True)


def _remove_pid_file(*, pid: int | None = None) -> None:
    """Remove the pid file, optionally only when it still names ``pid``."""
    path = device_pid_path()
    if pid is not None:
        current = _read_pid_file()
        if current is None or current.get("pid") != pid:
            return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.debug("Could not remove device pid file %s", path, exc_info=True)


def _windows_pid_alive(pid: int) -> bool:
    import ctypes  # pragma: no cover - Windows only

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - Windows only
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _pid_is_device_client(pid: int) -> bool:
    """Best-effort guard against a recycled pid from a stale pid file."""
    if not _pid_alive(pid):
        return False
    if os.name == "nt":  # pragma: no cover - Windows only
        return True
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        # No procfs (e.g. macOS): liveness is all we can check.
        return True
    args = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
    return any(arg in {REPO_MODULE, INSTALLED_MODULE} for arg in args)


def client_running() -> bool:
    """True when this or another plugin process already runs the client."""
    global _process

    if _process is not None:
        if _process.poll() is None:
            return True
        _process = None

    record = _read_pid_file()
    if record is None:
        return False
    if _pid_is_device_client(int(record["pid"])):
        return True
    _remove_pid_file()
    return False


# ── lifecycle ───────────────────────────────────────────────────────────────


def _creation_flags() -> int:
    if os.name != "nt":  # pragma: no cover - platform branch
        return 0
    return (  # pragma: no cover - Windows only
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
    )


def start_device_client(
    settings: RoleSettings | None = None,
    *,
    layout: ClientLayout | None = None,
    python: str | None = None,
    workdir: Path | str | None = None,
) -> DeviceStartResult:
    """Start the detached device client when the role and token allow it.

    Idempotent: a live child or pid file yields ``already-running`` and no
    second process.  Returns a result object instead of raising so plugin
    registration can log and continue.
    """
    global _process, _atexit_registered

    if settings is None:
        try:
            settings = resolve_settings()
        except ConfigError as exc:
            logger.warning("Invalid Hermes-Relay settings: %s", exc)
            return DeviceStartResult(False, "invalid-settings")

    if not settings.runs_client:
        return DeviceStartResult(False, "not-client")

    if client_running():
        record = _read_pid_file() or {}
        pid = record.get("pid") if isinstance(record, dict) else None
        logger.info(
            "Hermes-Relay device client already running (pid=%s); not starting another",
            pid if pid is not None else "unknown",
        )
        return DeviceStartResult(True, "already-running", pid=pid)

    decision = plan_device_launch(
        settings, layout=layout, python=python, workdir=workdir
    )
    if decision.plan is None:
        logger.warning("%s", startup_message(decision.reason, settings, decision.detail))
        return DeviceStartResult(False, decision.reason, url=decision.url)

    plan = decision.plan
    log_path = device_log_path(create=True)
    env = os.environ.copy()
    env.setdefault("HERMES_HOME", str(hermes_home()))
    # The child is always the client half, even for role=both.
    env["HERMES_RELAY_ROLE"] = "client"

    handle = None
    try:
        # The descriptor must stay open across Popen and is closed in finally;
        # a context manager would close it before the child inherits it.
        handle = open(log_path, "ab", buffering=0)  # noqa: SIM115
        process = subprocess.Popen(
            list(plan.argv),
            cwd=str(plan.cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            creationflags=_creation_flags(),
        )
    except OSError:
        logger.exception("Could not start the Hermes-Relay device client")
        return DeviceStartResult(False, "spawn-failed", url=plan.url)
    finally:
        # The child inherited its own copy of the descriptor; keeping the
        # parent's copy open would leak one fd per plugin reload.
        if handle is not None:
            try:
                handle.close()
            except OSError:  # pragma: no cover - best effort
                pass

    _process = process
    _write_pid_file(process.pid, plan.url)

    if not _atexit_registered:
        atexit.register(stop_device_client)
        _atexit_registered = True

    logger.info(
        "Started Hermes-Relay device client pid=%s via `python -m %s run` "
        "(url %s, cwd %s, log %s)",
        process.pid,
        plan.module,
        plan.url,
        plan.cwd,
        log_path,
    )
    return DeviceStartResult(True, "started", pid=process.pid, url=plan.url)


def stop_device_client() -> None:
    """Terminate the child spawned by *this* plugin process, if any.

    A client started by another process (e.g. a systemd unit) is left alone;
    only the pid file this process wrote is removed.
    """
    global _process

    process = _process
    _process = None
    if process is None:
        return

    pid = getattr(process, "pid", None)
    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - hard kill failed
                logger.warning("Device client pid=%s did not exit after kill", pid)
        except OSError:  # pragma: no cover - already gone
            pass
    if isinstance(pid, int):
        _remove_pid_file(pid=pid)
    logger.info("Stopped Hermes-Relay device client pid=%s", pid)


__all__ = [
    "INSTALLED_MODULE",
    "LOG_FILE_NAME",
    "PID_FILE_NAME",
    "REPO_MODULE",
    "ClientLayout",
    "DeviceLaunchDecision",
    "DeviceLaunchPlan",
    "DeviceStartResult",
    "build_device_argv",
    "client_running",
    "detect_client_layout",
    "device_log_path",
    "device_pid_path",
    "plan_device_launch",
    "plugin_package_dir",
    "start_device_client",
    "startup_message",
    "stop_device_client",
]
