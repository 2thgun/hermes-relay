"""Minimal Chrome DevTools Protocol client over WebSocket.

Mirrors ``desktop/src/tools/browser/cdp.ts``: monotonically increasing request
ids, one receiver task per connection, per-request timeouts, and event
subscription.  ``aiohttp`` is already a plugin dependency, so the Python client
does not need a second websocket stack.

The controller never constructs a socket directly; it calls :func:`open_cdp`,
which makes the transport replaceable in tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_MS = 15_000
MAX_MESSAGE_BYTES = 64 * 1024 * 1024

ConnectHook = Callable[[str, int], Awaitable[Any]]


class CdpError(Exception):
    """A CDP command returned ``{error: {message, code}}``."""

    def __init__(self, method: str, message: str, code: int | None = None) -> None:
        super().__init__(f"{method}: {message} ({code if code is not None else 'unknown'})")
        self.method = method
        self.message = message
        self.code = code


class CdpTimeout(CdpError):
    """The command (or event) did not settle before its deadline."""

    def __init__(self, method: str, message: str) -> None:
        Exception.__init__(self, message)
        self.method = method
        self.message = message
        self.code = None


class CdpConnectionError(Exception):
    """The websocket could not be opened."""


class CdpConnectionClosed(Exception):
    """The connection is closed or the socket failed mid-request."""


@dataclass(frozen=True)
class CdpEvent:
    method: str
    params: dict[str, Any]
    session_id: str | None = None


class CdpConnection:
    """One CDP websocket (browser-level or page-level)."""

    def __init__(
        self,
        websocket: Any,
        *,
        session: aiohttp.ClientSession | None = None,
        default_timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> None:
        self._ws = websocket
        self._session = session
        self._default_timeout_ms = default_timeout_ms
        self._next_id = 1
        self._pending: dict[int, tuple[asyncio.Future[Any], str]] = {}
        self._listeners: dict[str, set[Callable[[CdpEvent], None]]] = {}
        self._close_handlers: set[Callable[[str], None]] = set()
        self._closed = False
        self._notified = False
        self._pump_task = asyncio.create_task(self._pump(), name="cdp-pump")

    # ── Properties ──────────────────────────────────────────────────────

    @property
    def is_closed(self) -> bool:
        return self._closed

    # ── Event subscription ──────────────────────────────────────────────

    def on(self, method: str, listener: Callable[[CdpEvent], None]) -> Callable[[], None]:
        """Subscribe to one CDP event; the returned callable unsubscribes."""
        listeners = self._listeners.setdefault(method, set())
        listeners.add(listener)

        def unsubscribe() -> None:
            listeners.discard(listener)

        return unsubscribe

    def on_close(self, listener: Callable[[str], None]) -> Callable[[], None]:
        self._close_handlers.add(listener)

        def unsubscribe() -> None:
            self._close_handlers.discard(listener)

        return unsubscribe

    async def wait_for_event(self, method: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> CdpEvent:
        future: asyncio.Future[CdpEvent] = asyncio.get_running_loop().create_future()

        def listener(event: CdpEvent) -> None:
            if not future.done():
                future.set_result(event)

        unsubscribe = self.on(method, listener)
        try:
            return await asyncio.wait_for(future, timeout=timeout_ms / 1000)
        except TimeoutError:
            raise CdpTimeout(method, f"Timed out waiting for {method}") from None
        finally:
            unsubscribe()

    # ── Commands ────────────────────────────────────────────────────────

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        if self._closed:
            raise CdpConnectionClosed("CDP connection is closed")
        message_id = self._next_id
        self._next_id += 1
        timeout = self._default_timeout_ms if timeout_ms is None else timeout_ms
        payload: dict[str, Any] = {
            "id": message_id,
            "method": method,
            "params": params or {},
        }
        if session_id:
            payload["sessionId"] = session_id

        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = (future, method)
        try:
            await self._ws.send_json(payload)
        except Exception as exc:  # noqa: BLE001 - socket failures are transport errors
            self._pending.pop(message_id, None)
            raise CdpConnectionClosed(f"CDP connection closed while sending {method}") from exc

        try:
            return await asyncio.wait_for(future, timeout=timeout / 1000)
        except TimeoutError:
            self._pending.pop(message_id, None)
            raise CdpTimeout(method, f"{method} timed out after {timeout}ms") from None

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def close(self) -> None:
        self._closed = True
        if not self._pump_task.done():
            self._pump_task.cancel()
        try:
            await self._pump_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        try:
            await self._ws.close()
        except Exception:  # noqa: BLE001 - already closed
            pass
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:  # noqa: BLE001
                pass
        self._fail_pending("CDP connection closed")
        self._notify_close("closed")

    async def _pump(self) -> None:
        try:
            async for message in self._ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    self._dispatch_text(message.data)
                elif message.type == aiohttp.WSMsgType.BINARY:
                    try:
                        self._dispatch_text(message.data.decode("utf-8", "replace"))
                    except Exception:  # noqa: BLE001 - ignore malformed frames
                        continue
                elif message.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - receiver failures close the connection
            logger.debug("CDP receiver failed", exc_info=True)
        finally:
            self._closed = True
            self._fail_pending("CDP connection closed")
            self._notify_close("closed")

    def _dispatch_text(self, text: Any) -> None:
        if not isinstance(text, str):
            return
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return
        if not isinstance(parsed, dict):
            return
        message_id = parsed.get("id")
        if isinstance(message_id, int) and not isinstance(message_id, bool):
            entry = self._pending.pop(message_id, None)
            if entry is None:
                return
            future, method = entry
            if future.done():
                return
            error = parsed.get("error")
            if error:
                message = error.get("message") if isinstance(error, dict) else None
                code = error.get("code") if isinstance(error, dict) else None
                future.set_exception(
                    CdpError(method, str(message or "CDP error"), code if isinstance(code, int) else None)
                )
            else:
                result = parsed.get("result")
                future.set_result(result if isinstance(result, dict) else {})
            return

        method = parsed.get("method")
        if not isinstance(method, str):
            return
        params = parsed.get("params")
        event = CdpEvent(
            method=method,
            params=params if isinstance(params, dict) else {},
            session_id=parsed.get("sessionId") if isinstance(parsed.get("sessionId"), str) else None,
        )
        for listener in list(self._listeners.get(method, ())):
            try:
                listener(event)
            except Exception:  # noqa: BLE001 - listener errors must not break the pump
                logger.debug("CDP event listener failed for %s", method, exc_info=True)

    def _fail_pending(self, message: str) -> None:
        for message_id, (future, _method) in list(self._pending.items()):
            if not future.done():
                future.set_exception(CdpConnectionClosed(message))
            self._pending.pop(message_id, None)

    def _notify_close(self, reason: str) -> None:
        if self._notified:
            return
        self._notified = True
        for handler in list(self._close_handlers):
            try:
                handler(reason)
            except Exception:  # noqa: BLE001 - close hooks are best effort
                logger.debug("CDP close handler failed", exc_info=True)


async def open_cdp(
    url: str,
    *,
    timeout_ms: int = 10_000,
    connect: ConnectHook | None = None,
) -> CdpConnection:
    """Connect to a CDP websocket endpoint.

    ``connect`` is a test seam returning an already-open websocket; the default
    uses ``aiohttp`` with a total connect deadline equal to ``timeout_ms``.
    """
    if connect is not None:
        websocket = await connect(url, timeout_ms)
        return CdpConnection(websocket, default_timeout_ms=DEFAULT_TIMEOUT_MS)

    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_ms / 1000))
    try:
        websocket = await session.ws_connect(
            url,
            heartbeat=30.0,
            max_msg_size=MAX_MESSAGE_BYTES,
        )
    except TimeoutError:
        await session.close()
        raise CdpConnectionError(f"Timed out connecting to CDP at {url}") from None
    except Exception as exc:  # noqa: BLE001 - normalize aiohttp/OS failures
        await session.close()
        raise CdpConnectionError(f"Could not connect to CDP at {url}") from exc
    return CdpConnection(websocket, session=session)


__all__ = [
    "CdpConnection",
    "CdpConnectionClosed",
    "CdpConnectionError",
    "CdpError",
    "CdpEvent",
    "CdpTimeout",
    "DEFAULT_TIMEOUT_MS",
    "open_cdp",
]
