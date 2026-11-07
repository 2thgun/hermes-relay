"""Diagnostics handler for the Python relay client.

``desktop_health`` exists on the relay as a relay-local tool (it answers from
the latest ``desktop.status`` heartbeat without a client round-trip).  The
headless client still implements and advertises its own handler so a direct
``desktop.command`` probe — or a future relay-side delegation — can ask the
client how it is doing.  The payload is intentionally close to the TS router's
heartbeat fields (``host``/``platform``/``pid``/``uptime_ms``/``last_error``).
"""

from __future__ import annotations

import os
import platform
import socket
import sys
import time
from typing import Any

from ..dispatch import ClientState, ToolContext

try:  # package is new; keep import failures non-fatal for older installs
    from .. import __version__
except Exception:  # noqa: BLE001
    __version__ = "unknown"


def make_health_handler(
    state: ClientState,
    *,
    advertised: list[str] | None = None,
    registry: Any = None,
):
    async def desktop_health(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        snapshot = state.snapshot()
        tools = advertised
        if tools is None and registry is not None:
            tools = registry.names()
        return {
            "ok": True,
            "connected": True,
            "client": "python",
            "version": __version__,
            "host": socket.gethostname(),
            "platform": sys.platform,
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "pid": os.getpid(),
            "cwd": ctx.cwd,
            "started_at_ms": snapshot["started_at_ms"],
            "uptime_ms": snapshot["uptime_ms"],
            "uptime_s": round(snapshot["uptime_ms"] / 1000.0, 3),
            "device_id": snapshot["device_id"],
            "device_name": snapshot["device_name"],
            "url": snapshot["url"],
            "handled": snapshot["handled"],
            "failed": snapshot["failed"],
            "last_tool": snapshot["last_tool"],
            "last_error": snapshot["last_error"],
            "advertised_tools": tools if tools is not None else [],
            "now_ms": int(time.time() * 1000),
        }

    return desktop_health


__all__ = ["make_health_handler"]
