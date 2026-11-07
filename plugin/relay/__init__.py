"""Hermes-Relay server — unified WSS server embedded in the hermes-android plugin.

Exposes the chat, terminal, and bridge channels over a single WebSocket on
port 8767 (default). Replaces the standalone ``relay_server/`` package; the
top-level ``relay_server`` module is kept as a thin compat shim so existing
entrypoints like ``python -m relay_server`` keep working.

See ``plugin/relay/server.py`` for the aiohttp server,
``plugin/relay/channels/terminal.py`` for the PTY-backed terminal handler.

``create_app``/``main`` are re-exported lazily: importing this package must
stay cheap so the headless client (``plugin.relay.device``) can start on a
machine that only has the client's own dependencies.  The server module pulls
in requests, yaml, psutil, and the rest of the plugin stack.
"""

from __future__ import annotations

from typing import Any

# Canonical plugin version source is pyproject.toml's [project].version.
# Keep this runtime constant in sync with pyproject.toml for server-v*
# releases. Android releases use gradle/libs.versions.toml and android-v* tags;
# CLI+UI releases use desktop/package.json and desktop-v* tags. The /health endpoint
# reports this plugin version, and stale values make live diagnosis harder than
# it should be.
__version__ = "1.13.1"

__all__ = ["__version__", "create_app", "main"]

_LAZY_SERVER_ATTRIBUTES = frozenset({"create_app", "main"})


def __getattr__(name: str) -> Any:
    """Load ``create_app``/``main`` from ``plugin.relay.server`` on demand."""
    if name not in _LAZY_SERVER_ATTRIBUTES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from . import server as _server

    value = getattr(_server, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | _LAZY_SERVER_ATTRIBUTES)
