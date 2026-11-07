"""Background job API for the Python relay client.

Wire-compatible with ``desktop/src/tools/handlers/jobs.ts``:

* ``desktop_job_start``  → ``{job_id, pid, started_at_ms, command, cwd, shell, log_paths}``
* ``desktop_job_status`` → meta fields + ``state`` + ``log_paths``
* ``desktop_job_logs``   → tail/window of stdout/stderr plus ``state``
* ``desktop_job_cancel`` → ``{job_id, ok, signaled, already_exited}``
* ``desktop_job_list``   → ``{jobs, count, truncated}``

Jobs live on disk under ``~/.hermes/desktop-jobs/<job_id>/`` with
``meta.json``, ``stdout.log`` and ``stderr.log``.  The on-disk metadata is the
source of truth, so a restarted client can still report on jobs it did not
spawn.  ``HERMES_RELAY_JOBS_DIR`` overrides the root for tests or sandboxed
installs (the TypeScript client has the same layout hard-coded).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from ..dispatch import ToolContext, ToolError

DEFAULT_LOG_LIMIT = 8 * 1024
DEFAULT_LIST_LIMIT = 100

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_WRITE_LOCK = threading.Lock()
# Live children owned by this process: job_id → Popen. Only used to make
# cancel() immediate; status/list always read meta.json.
_live_jobs: dict[str, subprocess.Popen[bytes]] = {}


def jobs_root() -> Path:
    override = os.environ.get("HERMES_RELAY_JOBS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".hermes" / "desktop-jobs"


def job_dir(job_id: str) -> Path:
    if not _JOB_ID_RE.match(job_id) or job_id in {".", ".."}:
        raise ToolError(f'invalid "job_id": {job_id!r}')
    return jobs_root() / job_id


def _new_job_id() -> str:
    return secrets.token_hex(8)


def _require_str(args: dict[str, Any], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or value == "":
        raise ToolError(f'missing or invalid "{name}" argument')
    return value


def _optional_str(args: dict[str, Any], name: str) -> str | None:
    value = args.get(name)
    return value if isinstance(value, str) and value else None


def _arg_int(args: dict[str, Any], name: str, default: int, *, minimum: int = 0) -> int:
    value = args.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    value = int(value)
    return value if value >= minimum else default


def _arg_bool(args: dict[str, Any], name: str, default: bool) -> bool:
    value = args.get(name)
    return value if isinstance(value, bool) else default


def read_meta(job_id: str) -> dict[str, Any] | None:
    path = job_dir(job_id) / "meta.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def write_meta(meta: dict[str, Any]) -> None:
    directory = job_dir(str(meta["job_id"]))
    directory.mkdir(parents=True, exist_ok=True)
    temp = directory / f"meta.json.tmp-{os.getpid()}-{threading.get_ident()}"
    payload = json.dumps(meta, indent=2, sort_keys=True)
    with _WRITE_LOCK:
        try:
            temp.write_bytes(payload.encode("utf-8"))
            os.replace(temp, directory / "meta.json")
        except OSError as exc:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise ToolError(f"cannot write job metadata: {exc}") from exc


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) is not a no-op on Windows (it can terminate the
        # process), so probe with OpenProcess/GetExitCodeProcess instead.
        try:
            import ctypes

            process_query_limited_information = 0x1000
            still_active = 259
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                return bool(ok) and exit_code.value == still_active
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001 - conservative fallback
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _signal_name(returncode: int | None) -> str | None:
    if returncode is None or returncode >= 0:
        return None
    try:
        return signal.Signals(-returncode).name
    except ValueError:
        return f"SIG{-returncode}"


def _compute_state(job_id: str, meta: dict[str, Any]) -> str:
    live = _live_jobs.get(job_id)
    if live is not None and live.poll() is None:
        return "running"
    exit_code = meta.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        if meta.get("killed_by") in {"user", "signal"}:
            return "killed"
        return "exited" if exit_code == 0 else "failed"
    pid = meta.get("pid")
    if isinstance(pid, int) and pid > 0 and _pid_alive(pid):
        return "running"
    return "unknown"


def _watch(job_id: str, process: subprocess.Popen[bytes]) -> None:
    """Record the exit status once the child terminates (daemon thread)."""
    try:
        returncode = process.wait()
    except Exception:  # noqa: BLE001 - a watcher must never crash the process
        return
    try:
        meta = read_meta(job_id)
        if meta is None or meta.get("pid") != process.pid:
            return
        if isinstance(meta.get("exit_code"), int):
            return
        meta["ended_at_ms"] = int(time.time() * 1000)
        meta["exit_code"] = returncode
        name = _signal_name(returncode)
        if name is not None:
            meta["signal"] = name
            meta.setdefault("killed_by", "signal")
        write_meta(meta)
    except Exception:  # noqa: BLE001
        pass
    finally:
        _live_jobs.pop(job_id, None)


def _shell() -> tuple[str, list[str]]:
    if sys.platform == "win32":
        return "cmd", ["/c"]
    return "bash", ["-lc"]


# ── desktop_job_start ───────────────────────────────────────────────────────


async def job_start(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    command = _require_str(args, "command")
    cwd = _optional_str(args, "cwd") or ctx.cwd
    job_id = _optional_str(args, "job_id") or _new_job_id()
    directory = job_dir(job_id)  # validates the id before touching disk
    stdin_text = _optional_str(args, "stdin")
    env_override = args.get("env")
    if env_override is not None and not isinstance(env_override, dict):
        raise ToolError('"env" must be an object')
    env = dict(os.environ)
    if isinstance(env_override, dict):
        for key, value in env_override.items():
            if isinstance(key, str) and key:
                env[key] = str(value)

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ToolError(f"cannot create job directory: {exc}") from exc

    stdout_path = directory / "stdout.log"
    stderr_path = directory / "stderr.log"
    shell, shell_prefix = _shell()
    argv = [shell, *shell_prefix, command]

    try:
        stdout_file = stdout_path.open("ab")
        stderr_file = stderr_path.open("ab")
    except OSError as exc:
        raise ToolError(f"cannot open job logs: {exc}") from exc

    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=sys.platform != "win32",
            close_fds=True,
        )
    except FileNotFoundError as exc:
        stdout_file.close()
        stderr_file.close()
        raise ToolError(f"shell not found: {shell}") from exc
    except OSError as exc:
        stdout_file.close()
        stderr_file.close()
        raise ToolError(str(exc)) from exc
    finally:
        # The child owns its duplicates; the parent's descriptors can close.
        stdout_file.close()
        stderr_file.close()

    if stdin_text is not None and process.stdin is not None:
        data = stdin_text.encode("utf-8")

        def _feed() -> None:
            try:
                process.stdin.write(data)  # type: ignore[union-attr]
                process.stdin.close()  # type: ignore[union-attr]
            except (BrokenPipeError, OSError, ValueError):
                pass

        threading.Thread(target=_feed, name=f"job-stdin-{job_id}", daemon=True).start()

    started_at_ms = int(time.time() * 1000)
    meta: dict[str, Any] = {
        "job_id": job_id,
        "command": command,
        "cwd": cwd,
        "shell": shell,
        "pid": process.pid,
        "started_at_ms": started_at_ms,
        "had_stdin": stdin_text is not None,
    }
    if isinstance(env_override, dict) and env_override:
        meta["env_keys"] = [str(key) for key in env_override]
    write_meta(meta)
    _live_jobs[job_id] = process
    threading.Thread(
        target=_watch, args=(job_id, process), name=f"job-watch-{job_id}", daemon=True
    ).start()

    return {
        "job_id": job_id,
        "pid": process.pid,
        "started_at_ms": started_at_ms,
        "command": command,
        "cwd": cwd,
        "shell": shell,
        "log_paths": {"stdout": str(stdout_path), "stderr": str(stderr_path)},
    }


# ── desktop_job_status ──────────────────────────────────────────────────────


async def job_status(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    job_id = _require_str(args, "job_id")
    meta = read_meta(job_id)
    if meta is None:
        raise ToolError(f"unknown job_id: {job_id}")
    directory = job_dir(job_id)
    result = dict(meta)
    result["state"] = _compute_state(job_id, meta)
    result["log_paths"] = {
        "stdout": str(directory / "stdout.log"),
        "stderr": str(directory / "stderr.log"),
    }
    return result


# ── desktop_job_logs ────────────────────────────────────────────────────────


def _read_tail(path: Path, offset: int | None, limit: int) -> dict[str, Any]:
    try:
        total = path.stat().st_size
    except OSError:
        return {"content": "", "total_bytes": 0, "truncated": False, "offset": 0}

    if offset is None:
        start = max(0, total - limit)
    elif offset < 0:
        start = max(0, total + offset)
    else:
        start = min(offset, total)
    end = min(total, start + limit)

    try:
        with path.open("rb") as handle:
            handle.seek(start)
            data = handle.read(end - start)
    except OSError:
        return {"content": "", "total_bytes": total, "truncated": False, "offset": 0}
    return {
        "content": data.decode("utf-8", errors="replace"),
        "total_bytes": total,
        "truncated": end < total,
        "offset": end,
    }


async def job_logs(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    job_id = _require_str(args, "job_id")
    stream = (_optional_str(args, "stream") or "both").lower()
    if stream not in {"stdout", "stderr", "both"}:
        raise ToolError('"stream" must be "stdout", "stderr", or "both"')
    limit = _arg_int(args, "limit", DEFAULT_LOG_LIMIT, minimum=1)
    raw_offset = args.get("offset")
    offset = (
        int(raw_offset)
        if isinstance(raw_offset, (int, float)) and not isinstance(raw_offset, bool)
        else None
    )

    meta = read_meta(job_id)
    if meta is None:
        raise ToolError(f"unknown job_id: {job_id}")
    directory = job_dir(job_id)
    result: dict[str, Any] = {
        "job_id": job_id,
        "state": _compute_state(job_id, meta),
    }
    if isinstance(meta.get("exit_code"), int):
        result["exit_code"] = meta["exit_code"]
    if stream in {"stdout", "both"}:
        result["stdout"] = _read_tail(directory / "stdout.log", offset, limit)
    if stream in {"stderr", "both"}:
        result["stderr"] = _read_tail(directory / "stderr.log", offset, limit)
    return result


# ── desktop_job_cancel ──────────────────────────────────────────────────────


def _signal_job(pid: int, force: bool) -> None:
    if sys.platform == "win32":
        argv = ["taskkill", "/T", "/PID", str(pid)]
        if force:
            argv.insert(1, "/F")
        subprocess.run(argv, capture_output=True, timeout=10, check=False)
        return
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.killpg(os.getpgid(pid), sig)
    except (ProcessLookupError, PermissionError):
        os.kill(pid, sig)


async def job_cancel(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    job_id = _require_str(args, "job_id")
    force = _arg_bool(args, "force", False)
    meta = read_meta(job_id)
    if meta is None:
        raise ToolError(f"unknown job_id: {job_id}")

    live = _live_jobs.get(job_id)
    pid = meta.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return {"job_id": job_id, "ok": False, "signaled": None, "already_exited": True}

    if live is None and not _pid_alive(pid):
        return {"job_id": job_id, "ok": False, "signaled": None, "already_exited": True}

    try:
        _signal_job(pid, force)
        meta["killed_by"] = "user"
        write_meta(meta)
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "job_id": job_id,
            "ok": False,
            "signaled": None,
            "already_exited": False,
            "error": str(exc),
        }
    return {
        "job_id": job_id,
        "ok": True,
        "signaled": "KILL" if force else "TERM",
        "already_exited": False,
    }


# ── desktop_job_list ────────────────────────────────────────────────────────


async def job_list(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    want_state = _optional_str(args, "state")
    since_ms = _arg_int(args, "since_ms", 0)
    limit = _arg_int(args, "limit", DEFAULT_LIST_LIMIT, minimum=1)

    root = jobs_root()
    if not root.is_dir():
        return {"jobs": [], "count": 0, "truncated": False}

    collected: list[dict[str, Any]] = []
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        return {"jobs": [], "count": 0, "truncated": False}

    for entry in entries:
        if not entry.is_dir() or not _JOB_ID_RE.match(entry.name):
            continue
        meta = read_meta(entry.name)
        if meta is None:
            continue
        started = meta.get("started_at_ms")
        if since_ms > 0 and isinstance(started, int) and started < since_ms:
            continue
        state = _compute_state(entry.name, meta)
        if want_state and state != want_state:
            continue
        row = dict(meta)
        row["state"] = state
        collected.append(row)

    collected.sort(key=lambda row: row.get("started_at_ms") or 0, reverse=True)
    selected = collected[:limit]
    return {
        "jobs": selected,
        "count": len(selected),
        "truncated": len(collected) > limit,
    }


def handlers() -> dict[str, Any]:
    return {
        "desktop_job_start": job_start,
        "desktop_job_status": job_status,
        "desktop_job_logs": job_logs,
        "desktop_job_cancel": job_cancel,
        "desktop_job_list": job_list,
    }


__all__ = [
    "handlers",
    "job_cancel",
    "job_dir",
    "job_list",
    "job_logs",
    "job_start",
    "job_status",
    "jobs_root",
    "read_meta",
    "write_meta",
]
