"""High-level pair/serve orchestration for the Python relay client.

The CLI in :mod:`plugin.relay.client.__main__` is a thin wrapper around these
two coroutines, which keeps the network lifecycle testable without spawning a
subprocess.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import socket
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import __version__
from .dispatch import ClientState, DesktopDispatcher
from .handlers import browser as browser_handlers
from .handlers import build_registry
from .identity import (
    ClientIdentity,
    IdentityError,
    normalize_relay_url,
)
from .transport import AuthError, AuthResult, RelayTransport

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PairResult:
    url: str
    device_id: str
    device_name: str
    server_version: str | None
    stored_path: str
    expires_at: float | None = None
    transport_hint: str | None = None


def clean_pairing_code(code: str) -> str:
    """Normalize a 6-character pairing code, raising on malformed input."""
    cleaned = (code or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{6}", cleaned):
        raise IdentityError(
            "pairing code must be exactly 6 letters/digits (as shown by the relay)"
        )
    return cleaned


async def pair(
    url: str,
    code: str,
    *,
    name: str | None = None,
    store_path: str | os.PathLike[str] | None = None,
    insecure: bool = False,
    auth_timeout: float = 15.0,
) -> PairResult:
    """Exchange a one-time pairing code for a stored session token."""
    normalized = normalize_relay_url(url)
    pairing_code = clean_pairing_code(code)
    identity = ClientIdentity.load(store_path)
    if name:
        identity.set_device_name(name)

    transport = RelayTransport(
        normalized,
        identity=identity,
        pairing_code=pairing_code,
        insecure=insecure,
        auth_timeout=auth_timeout,
    )
    try:
        auth = await transport.connect_once()
    finally:
        await transport.close()

    identity.remember_pairing(
        normalized,
        session_token=auth.session_token,
        server_version=auth.server_version,
        expires_at=auth.expires_at,
        transport_hint=auth.transport_hint,
        grants=auth.grants,
    )
    stored = identity.save()
    logger.info("paired %s as %s (%s)", normalized, identity.device_name, identity.device_id)
    return PairResult(
        url=normalized,
        device_id=identity.device_id,
        device_name=identity.device_name,
        server_version=auth.server_version,
        stored_path=str(stored),
        expires_at=auth.expires_at,
        transport_hint=auth.transport_hint,
    )


@dataclass
class ServeOptions:
    url: str
    token: str | None = None
    code: str | None = None
    cwd: str = field(default_factory=os.getcwd)
    allow: set[str] | None = None
    deny: set[str] | None = None
    store_path: str | os.PathLike[str] | None = None
    insecure: bool = False
    heartbeat_interval: float = 30.0
    handler_timeout: float = 30.0
    reconnect_base: float = 1.0
    reconnect_max: float = 30.0
    max_reconnect_attempts: int = 0
    once: bool = False
    interactive: bool = False
    on_ready: Callable[[AuthResult], None] | None = None
    on_disconnected: Callable[[str], None] | None = None
    on_result: Callable[[str, dict[str, Any], bool], None] | None = None


@dataclass
class ServeHandle:
    """Observable state for an in-flight serve loop (used by CLI/tests)."""

    transport: RelayTransport
    dispatcher: DesktopDispatcher
    state: ClientState
    stop_event: asyncio.Event
    first_result: asyncio.Event
    task: asyncio.Task[None] | None = None


def build_status_payload(
    state: ClientState,
    registry: Any,
    *,
    capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``desktop.status`` heartbeat body (shape mirrors router.ts)."""
    now_ms = int(time.time() * 1000)
    return {
        "advertised_tools": registry.names(),
        "host": socket.gethostname(),
        "platform": sys.platform,
        "arch": platform.machine(),
        "python": platform.python_version(),
        "version": __version__,
        "pid": os.getpid(),
        "started_at_ms": state.started_at_ms,
        "uptime_ms": now_ms - state.started_at_ms,
        "interactive": False,
        "device_id": state.device_id,
        "device_name": state.device_name,
        "capabilities": capabilities
        or {
            "adapter": "python",
            "platform": platform.platform(),
            "graphical": False,
            "capabilities": [],
        },
    }


async def build_live_status_payload(state: ClientState, registry: Any) -> dict[str, Any]:
    """Heartbeat body enriched with the selected desktop adapter.

    The relay's ``desktop_devices`` tool reads this block, so a client that can
    actually drive Hyprland/X11 must not keep reporting ``graphical: false``.
    Probe failures degrade to a descriptive no-session block rather than
    suppressing the heartbeat.
    """

    from .platform import select_platform_adapter
    from .platform.base import wire

    try:
        selection = await select_platform_adapter()
        environment = await selection.adapter.environment()
        capabilities = await selection.adapter.capabilities()
        block: dict[str, Any] = {
            "adapter": selection.adapter.id,
            "platform": environment.platform,
            "session": environment.session,
            "compositor": environment.compositor,
            "graphical": environment.graphical,
            "reason": environment.reason,
            "capabilities": wire(capabilities),
            "dependencies": wire(environment.dependencies),
            "warnings": environment.warnings,
        }
    except Exception as exc:  # noqa: BLE001 - a probe must not break status
        block = {
            "adapter": "python",
            "platform": platform.platform(),
            "graphical": False,
            "reason": str(exc) or exc.__class__.__name__,
            "capabilities": [],
        }
    return build_status_payload(state, registry, capabilities=block)


def prepare_serve(options: ServeOptions) -> tuple[ServeHandle, ClientIdentity]:
    """Construct the transport/dispatcher without starting the network loop."""
    url = normalize_relay_url(options.url)
    identity = ClientIdentity.load(options.store_path)
    token = options.token or identity.token_for(url)
    code = clean_pairing_code(options.code) if options.code else None
    if not token and not code:
        raise IdentityError(
            f"no stored session for {url}; run `python -m plugin.relay.client pair` first"
        )

    state = ClientState(
        url=url,
        device_id=identity.device_id,
        device_name=identity.device_name,
        cwd=options.cwd,
    )
    registry = build_registry(state=state, allow=options.allow, deny=options.deny)
    transport = RelayTransport(
        url,
        identity=identity,
        token=token,
        pairing_code=code,
        insecure=options.insecure,
        heartbeat_interval=options.heartbeat_interval,
        reconnect_base=options.reconnect_base,
        reconnect_max=options.reconnect_max,
        max_reconnect_attempts=options.max_reconnect_attempts,
    )

    async def send_response(payload: dict[str, Any]) -> None:
        await transport.send("desktop", "desktop.response", payload)

    dispatcher = DesktopDispatcher(
        registry,
        send_response,
        state=state,
        cwd=options.cwd,
        timeout=options.handler_timeout,
        tool_timeouts=browser_handlers.timeouts(),
        interactive=options.interactive,
        on_result=options.on_result,
    )
    handle = ServeHandle(
        transport=transport,
        dispatcher=dispatcher,
        state=state,
        stop_event=asyncio.Event(),
        first_result=asyncio.Event(),
    )

    original_on_result = options.on_result
    if options.once:
        def _once_hook(tool: str, payload: dict[str, Any], ok: bool) -> None:
            if original_on_result is not None:
                original_on_result(tool, payload, ok)
            if ok:
                handle.first_result.set()

        dispatcher.on_result = _once_hook

    return handle, identity


async def serve(handle: ServeHandle, identity: ClientIdentity, options: ServeOptions) -> None:
    """Run the serve loop until ``handle.stop_event`` is set."""
    transport = handle.transport
    state = handle.state
    registry = handle.dispatcher.registry
    url = handle.state.url or options.url

    async def on_connected(auth: AuthResult) -> None:
        state.connected = True
        # A relay may rotate the token; persist it so the next start works.
        stored = identity.token_for(url)
        if auth.session_token and auth.session_token != stored:
            try:
                identity.remember_pairing(
                    url,
                    session_token=auth.session_token,
                    server_version=auth.server_version,
                    expires_at=auth.expires_at,
                    transport_hint=auth.transport_hint,
                    grants=auth.grants,
                )
                identity.save()
            except IdentityError:
                logger.warning("could not persist rotated session token", exc_info=True)
        if options.on_ready is not None:
            options.on_ready(auth)

    async def on_disconnected(reason: str) -> None:
        state.connected = False
        logger.info("disconnected: %s", reason or "closed")
        # The server fails every pending command when a desktop drops; do the
        # same locally so no handler keeps running against a dead socket.
        await handle.dispatcher.cancel_all(reason or "transport closed")
        if options.on_disconnected is not None:
            try:
                options.on_disconnected(reason)
            except Exception:  # noqa: BLE001
                logger.exception("on_disconnected callback failed")

    run_task = asyncio.create_task(
        transport.run(
            handle.dispatcher.handle_envelope,
            status_provider=lambda: build_live_status_payload(state, registry),
            on_connected=on_connected,
            on_disconnected=on_disconnected,
            stop_event=handle.stop_event,
        ),
        name="relay-serve",
    )
    handle.task = run_task
    wait_task = asyncio.create_task(handle.first_result.wait(), name="relay-first-result")

    try:
        done, _pending = await asyncio.wait(
            {run_task, wait_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if wait_task in done and options.once and not run_task.done():
            handle.stop_event.set()
            transport.stop()
        # Surface transport failures (auth denial, exhausted retries).
        await run_task
    except asyncio.CancelledError:
        handle.stop_event.set()
        transport.stop()
        try:
            await run_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        raise
    finally:
        wait_task.cancel()
        try:
            await wait_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await handle.dispatcher.close()
        await transport.close()


__all__ = [
    "PairResult",
    "ServeHandle",
    "ServeOptions",
    "build_live_status_payload",
    "build_status_payload",
    "clean_pairing_code",
    "pair",
    "prepare_serve",
    "serve",
    "AuthError",
]
