"""Run the Relay WSS server as a plugin-managed sidecar.

The Hermes gateway forks after loading plugins, so a server thread started in
``register()`` only lives in the parent and never reaches the worker that owns
the port. A small child process survives that fork and needs no systemd unit:
installing the plugin is enough.

Health is checked before spawning, so repeated registration, gateway reloads,
and a manually started relay do not create duplicates. The sidecar is detached,
logs under ``$HERMES_HOME/logs/hermes-relay-sidecar.log``, and is terminated on
a clean plugin-process exit.
"""

from __future__ import annotations

import atexit
import logging
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PORT = 18767
_process: subprocess.Popen[bytes] | None = None


def embedded_enabled() -> bool:
    return os.getenv("HERMES_RELAY_EMBEDDED", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def relay_port() -> int:
    try:
        return int(os.getenv("RELAY_PORT", str(DEFAULT_PORT)))
    except ValueError:
        return DEFAULT_PORT


def relay_host() -> str:
    return os.getenv("RELAY_HOST", "0.0.0.0")


def _healthy() -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{relay_port()}/health", timeout=0.7
        ) as response:
            return response.status == 200
    except Exception:
        return False


def _log_path() -> Path:
    home = Path(os.getenv("HERMES_HOME") or (Path.home() / ".hermes"))
    logs = home / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs / "hermes-relay-sidecar.log"


def start_embedded_relay() -> subprocess.Popen[bytes] | None:
    """Start the relay sidecar once. Idempotent and non-blocking."""
    global _process

    if not embedded_enabled():
        logger.info("Relay sidecar disabled via HERMES_RELAY_EMBEDDED")
        return None

    port = relay_port()
    os.environ.setdefault("DESKTOP_RELAY_URL", f"http://127.0.0.1:{port}")

    if _healthy():
        return None
    if _process is not None and _process.poll() is None:
        return _process

    plugin_dir = Path(__file__).resolve().parent.parent
    log = _log_path()
    env = os.environ.copy()
    env.setdefault("HERMES_HOME", str(Path.home() / ".hermes"))
    try:
        handle = open(log, "ab", buffering=0)
        _process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "relay",
                "--host",
                relay_host(),
                "--port",
                str(port),
                "--no-ssl",
            ],
            cwd=str(plugin_dir),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    except OSError:
        logger.exception("Could not start the Relay sidecar")
        return None

    atexit.register(stop_embedded_relay)
    logger.info("Started Relay sidecar pid=%s on port %s (log %s)", _process.pid, port, log)
    return _process


def stop_embedded_relay() -> None:
    global _process
    if _process is None:
        return
    if _process.poll() is None:
        _process.terminate()
        try:
            _process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _process.kill()
    _process = None
