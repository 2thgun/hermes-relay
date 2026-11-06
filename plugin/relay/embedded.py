"""Run the Relay WSS server inside the Hermes plugin process.

Historically the relay ran as a separate ``python -m plugin.relay`` systemd
unit. That works, but it means a plugin install is not self-contained: users
have to remember a unit, a port, and a working directory. This module owns the
server instead: one daemon thread, one asyncio loop, started from
``register(ctx)``. A bind failure (for example, an older systemd unit still
listening) is logged and leaves the external server in place.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading

from aiohttp import web

from .config import RelayConfig
from .server import create_app

logger = logging.getLogger(__name__)

DEFAULT_PORT = 18767

_lock = threading.Lock()
_thread: threading.Thread | None = None
_loop: asyncio.AbstractEventLoop | None = None
_runner: web.AppRunner | None = None


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


def start_embedded_relay() -> threading.Thread | None:
    """Start the relay once, in a daemon thread. Idempotent and non-blocking."""
    if not embedded_enabled():
        logger.info("Embedded relay disabled via HERMES_RELAY_EMBEDDED")
        return None

    # Keep the tool layer pointed at this instance even when the host chose a
    # non-default port. ``desktop_tool`` reads this on every request.
    os.environ.setdefault(
        "DESKTOP_RELAY_URL", f"http://127.0.0.1:{relay_port()}"
    )

    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return _thread
        _thread = threading.Thread(
            target=_serve,
            name="hermes-relay-embedded",
            daemon=True,
        )
        _thread.start()
        return _thread


def _serve() -> None:
    global _loop, _runner
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _loop = loop

    try:
        config = RelayConfig.from_env()
        config.host = relay_host()
        config.port = relay_port()
        # Nginx Proxy Manager terminates TLS for the public URL. An embedded
        # listener stays plain HTTP on the LAN/loopback side.
        config.ssl_cert = None
        config.ssl_key = None

        app = create_app(config)
        runner = web.AppRunner(app, access_log=None)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, config.host, config.port)
        loop.run_until_complete(site.start())
        _runner = runner
        logger.info(
            "Embedded Hermes Relay listening on %s:%s",
            config.host,
            config.port,
        )
        loop.run_forever()
    except OSError as exc:
        logger.warning(
            "Embedded relay could not bind %s:%s (%s); an external relay may "
            "already be running. The plugin tools will use that listener.",
            relay_host(),
            relay_port(),
            exc,
        )
    except Exception:
        logger.exception("Embedded relay failed to start")
    finally:
        try:
            if _runner is not None:
                loop.run_until_complete(_runner.cleanup())
        except Exception:
            logger.debug("Embedded relay cleanup failed", exc_info=True)
        loop.close()
