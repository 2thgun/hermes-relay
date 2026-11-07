"""WebSocket transport for the Python relay client.

This is the client half of the ``desktop`` channel described in
``plugin/relay/channels/desktop.py``.  It connects to a relay, performs the
``system/auth`` handshake with a pairing code or a stored session token, then
serves ``desktop.command`` envelopes until the socket closes.

Wire contract (frozen; matches ``desktop/src/transport/RelayTransport.ts`` and
docs/relay-protocol.md §3.7)::

    client → server   {channel:"system",  type:"auth",             payload:{...}}
    server → client   {channel:"system",  type:"auth.ok"|"auth.fail", payload:{...}}
    server → client   {channel:"desktop", type:"desktop.command",  payload:{request_id, tool, args, ...}}
    client → server   {channel:"desktop", type:"desktop.response", payload:{request_id, ok, result|error}}
    client → server   {channel:"desktop", type:"desktop.status",   payload:{advertised_tools, ...}}
    server → client   {channel:"system",  type:"ping",             payload:{ts}}

Reconnect policy mirrors the TypeScript client: exponential backoff of
1, 2, 4, 8, 16, 30, 30, … seconds, reset after a successful ``auth.ok``; a
close reason containing 429/rate-limited gets a five-minute delay.  Auth
failures are terminal because retrying the same rejected credential only
trips the relay's rate limiter.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import aiohttp

from .identity import ClientIdentity

logger = logging.getLogger(__name__)

AUTH_TIMEOUT = 15.0
HEARTBEAT_INTERVAL = 30.0
RECONNECT_BASE = 1.0
RECONNECT_MAX = 30.0
RECONNECT_BACKOFF_CEIL = 10
RATE_LIMIT_DELAY = 5 * 60.0
MAX_MESSAGE_BYTES = 8 * 1024 * 1024

EnvelopeHandler = Callable[[dict[str, Any]], Awaitable[None] | None]
StatusProvider = Callable[[], dict[str, Any] | Awaitable[dict[str, Any]]]


class TransportError(Exception):
    """Transient connection failure (retryable)."""


class RateLimitedError(TransportError):
    """The relay rejected the connection with HTTP 429."""


class AuthError(Exception):
    """The relay rejected our credentials. Retrying cannot help."""


@dataclass(frozen=True)
class AuthResult:
    """Result of a successful ``system/auth`` handshake."""

    session_token: str
    server_version: str | None = None
    expires_at: float | None = None
    grants: dict[str, float | None] | None = None
    transport_hint: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any], fallback_token: str) -> "AuthResult":
        token = payload.get("session_token")
        if not isinstance(token, str) or not token:
            token = fallback_token
        expires = payload.get("expires_at")
        if isinstance(expires, bool) or not isinstance(expires, (int, float)):
            expires = None
        grants_raw = payload.get("grants")
        grants: dict[str, float | None] | None = None
        if isinstance(grants_raw, dict):
            grants = {}
            for key, value in grants_raw.items():
                if not isinstance(key, str):
                    continue
                if value is None:
                    grants[key] = None
                elif isinstance(value, (int, float)) and not isinstance(value, bool):
                    grants[key] = float(value)
        hint = payload.get("transport_hint")
        version = payload.get("server_version")
        return cls(
            session_token=token,
            server_version=version if isinstance(version, str) else None,
            expires_at=float(expires) if expires is not None else None,
            grants=grants,
            transport_hint=hint if isinstance(hint, str) else None,
            payload=payload,
        )


class RelayTransport:
    """One relay connection with reconnect, heartbeat, and auth handling."""

    def __init__(
        self,
        url: str,
        *,
        identity: ClientIdentity,
        token: str | None = None,
        pairing_code: str | None = None,
        insecure: bool = False,
        auth_timeout: float = AUTH_TIMEOUT,
        heartbeat_interval: float = HEARTBEAT_INTERVAL,
        reconnect_base: float = RECONNECT_BASE,
        reconnect_max: float = RECONNECT_MAX,
        max_reconnect_attempts: int = 0,
        ttl_seconds: int | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        if not token and not pairing_code:
            raise ValueError("RelayTransport requires token= or pairing_code=")
        self.url = url
        self.identity = identity
        self.token = token
        self.pairing_code = pairing_code
        self.insecure = insecure
        self.auth_timeout = auth_timeout
        self.heartbeat_interval = heartbeat_interval
        self.reconnect_base = reconnect_base
        self.reconnect_max = reconnect_max
        self.max_reconnect_attempts = max_reconnect_attempts
        self.ttl_seconds = ttl_seconds

        self._session = session
        self._owns_session = session is None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._send_lock = asyncio.Lock()
        self._stopping = asyncio.Event()
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._status_provider: StatusProvider | None = None
        self._connected_at: float | None = None

    # ── Lifecycle ────────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    @property
    def connected_at(self) -> float | None:
        return self._connected_at

    async def close(self) -> None:
        """Close the current socket and the owned HTTP session."""
        self._stopping.set()
        await self._stop_heartbeat()
        ws, self._ws = self._ws, None
        if ws is not None and not ws.closed:
            try:
                await ws.close()
            except (aiohttp.ClientError, ConnectionError, RuntimeError):
                pass
        if self._owns_session and self._session is not None:
            session, self._session = self._session, None
            await session.close()

    def stop(self) -> None:
        """Request shutdown. Safe to call from a signal handler."""
        self._stopping.set()
        ws = self._ws
        if ws is not None and not ws.closed:
            asyncio.ensure_future(ws.close())

    # ── Single connection ────────────────────────────────────────────────

    async def connect_once(self) -> AuthResult:
        """Open one WebSocket and complete the auth handshake.

        Raises :class:`AuthError` for rejected credentials (terminal) and
        :class:`TransportError`/``OSError`` for transient failures.
        """
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, connect=15.0, sock_read=None)
            )
        ws: aiohttp.ClientWebSocketResponse | None = None
        try:
            kwargs: dict[str, Any] = {
                "heartbeat": self.heartbeat_interval,
                "max_msg_size": MAX_MESSAGE_BYTES,
                "headers": {"User-Agent": _user_agent()},
            }
            if self.insecure:
                kwargs["ssl"] = False
            try:
                ws = await self._session.ws_connect(self.url, **kwargs)
            except aiohttp.WSServerHandshakeError as exc:
                if exc.status == 429:
                    raise RateLimitedError(
                        f"relay rejected connection with HTTP 429: {exc.message}"
                    ) from exc
                raise TransportError(
                    f"relay handshake failed with HTTP {exc.status}: {exc.message}"
                ) from exc
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
                raise TransportError(f"cannot connect to relay: {exc}") from exc

            await self._send_auth(ws)
            auth = await self._await_auth(ws)
            self._ws = ws
            self._connected_at = time.time()
            logger.info(
                "connected to %s (server %s, transport %s)",
                self.url,
                auth.server_version or "?",
                auth.transport_hint or "?",
            )
            return auth
        except BaseException:
            if ws is not None and not ws.closed:
                try:
                    await ws.close()
                except (aiohttp.ClientError, ConnectionError, RuntimeError):
                    pass
            raise

    async def _send_auth(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        payload: dict[str, Any] = {"supports": {
            "typed_stream_events": True,
            "event_schema_version": 1,
        }}
        if self.pairing_code:
            payload["pairing_code"] = self.pairing_code
        elif self.token:
            payload["session_token"] = self.token
        payload.update(self.identity.device_metadata())
        if self.ttl_seconds is not None:
            payload["ttl_seconds"] = int(self.ttl_seconds)
        envelope = {
            "channel": "system",
            "type": "auth",
            "id": str(uuid.uuid4()),
            "payload": payload,
        }
        try:
            await ws.send_str(json.dumps(envelope))
        except (aiohttp.ClientError, ConnectionError) as exc:
            raise TransportError(f"failed to send auth envelope: {exc}") from exc

    async def _await_auth(self, ws: aiohttp.ClientWebSocketResponse) -> AuthResult:
        deadline = time.monotonic() + self.auth_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportError(
                    f"auth timed out after {self.auth_timeout:.0f}s"
                )
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise TransportError(
                    f"auth timed out after {self.auth_timeout:.0f}s"
                ) from exc
            except (aiohttp.ClientError, ConnectionError) as exc:
                raise TransportError(f"connection lost during auth: {exc}") from exc

            if msg.type == aiohttp.WSMsgType.TEXT:
                envelope = _parse_envelope(msg.data)
                if envelope is None:
                    continue
                channel = envelope.get("channel")
                msg_type = envelope.get("type")
                if channel == "system" and msg_type == "ping":
                    await _send_pong(ws, envelope.get("payload") or {})
                    continue
                if channel != "system":
                    logger.debug("ignoring %s/%s before auth", channel, msg_type)
                    continue
                payload = envelope.get("payload")
                if not isinstance(payload, dict):
                    payload = {}
                if msg_type == "auth.ok":
                    fallback = self.token or ""
                    auth = AuthResult.from_payload(payload, fallback)
                    if not auth.session_token:
                        raise AuthError("relay auth.ok did not include a session_token")
                    self.token = auth.session_token
                    # The pairing code is single-use; a reconnect must present
                    # the minted token instead of replaying the consumed code.
                    self.pairing_code = None
                    return auth
                if msg_type == "auth.fail":
                    reason = payload.get("reason")
                    raise AuthError(
                        str(reason) if isinstance(reason, str) and reason else "authentication failed"
                    )
                if msg_type == "error":
                    message = payload.get("message") or "relay returned an error during auth"
                    raise AuthError(str(message))
                continue

            if msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
            ):
                raise TransportError("relay closed the connection during auth")
            if msg.type == aiohttp.WSMsgType.ERROR:
                raise TransportError(f"relay websocket error during auth: {ws.exception()}")
            # BINARY / PING / PONG — aiohttp handles ping/pong internally.

    # ── Serving ──────────────────────────────────────────────────────────

    async def run(
        self,
        handler: EnvelopeHandler,
        *,
        status_provider: StatusProvider | None = None,
        on_connected: Callable[[AuthResult], Awaitable[None] | None] | None = None,
        on_disconnected: Callable[[str], Awaitable[None] | None] | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Connect, serve envelopes, and reconnect until stopped.

        ``handler`` receives every non-system envelope.  ``status_provider``
        supplies the ``desktop.status`` heartbeat payload (sent immediately
        after each auth and every ``heartbeat_interval`` seconds).
        """
        self._status_provider = status_provider
        stop = self._stopping
        watcher: asyncio.Task[None] | None = None
        if stop_event is not None:
            watcher = asyncio.create_task(_forward_stop(stop_event, stop), name="relay-stop")
        attempt = 0

        try:
            while not stop.is_set():
                try:
                    auth = await self.connect_once()
                except AuthError:
                    raise
                except RateLimitedError as exc:
                    attempt += 1
                    delay = RATE_LIMIT_DELAY
                    logger.warning("%s; retrying in %.0fs", exc, delay)
                    await _sleep_or_stop(delay, stop)
                    continue
                except (TransportError, OSError, asyncio.TimeoutError) as exc:
                    attempt += 1
                    if self.max_reconnect_attempts and attempt >= self.max_reconnect_attempts:
                        raise TransportError(
                            f"gave up after {attempt} connection attempts: {exc}"
                        ) from exc
                    delay = self.backoff_for(attempt)
                    logger.warning("connect failed (%s); retry %d in %.1fs", exc, attempt, delay)
                    await _sleep_or_stop(delay, stop)
                    continue

                attempt = 0
                if on_connected is not None:
                    try:
                        result = on_connected(auth)
                        if inspect.isawaitable(result):
                            await result
                    except Exception:  # noqa: BLE001 - logging must not kill the link
                        logger.exception("on_connected callback failed")

                reason = ""
                try:
                    reason = await self._serve(handler, stop)
                finally:
                    await self._stop_heartbeat()
                    ws, self._ws = self._ws, None
                    self._connected_at = None
                    if ws is not None and not ws.closed:
                        try:
                            await ws.close()
                        except (aiohttp.ClientError, ConnectionError, RuntimeError):
                            pass

                if stop.is_set():
                    break
                if reason == "__auth_failed__":
                    raise AuthError("relay rejected the session during an active connection")

                attempt += 1
                if self.max_reconnect_attempts and attempt > self.max_reconnect_attempts:
                    raise TransportError(
                        f"gave up after {attempt - 1} reconnect attempts ({reason or 'closed'})"
                    )
                delay = self.backoff_for(attempt, reason)
                logger.warning(
                    "disconnected (%s); reconnect %d in %.1fs",
                    reason or "closed",
                    attempt,
                    delay,
                )
                if on_disconnected is not None:
                    try:
                        result = on_disconnected(reason)
                        if inspect.isawaitable(result):
                            await result
                    except Exception:  # noqa: BLE001
                        logger.exception("on_disconnected callback failed")
                await _sleep_or_stop(delay, stop)
        finally:
            if watcher is not None and not watcher.done():
                watcher.cancel()
                try:
                    await watcher
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

        logger.info("transport stopped")

    async def _serve(self, handler: EnvelopeHandler, stop: asyncio.Event) -> str:
        ws = self._ws
        if ws is None:
            return "no socket"
        self._start_heartbeat()
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
            except asyncio.TimeoutError:
                if stop.is_set():
                    return "stopped"
                continue
            except (aiohttp.ClientError, ConnectionResetError) as exc:
                return str(exc)

            if msg.type == aiohttp.WSMsgType.TEXT:
                envelope = _parse_envelope(msg.data)
                if envelope is None:
                    continue
                channel = envelope.get("channel")
                msg_type = envelope.get("type")
                payload = envelope.get("payload")
                if not isinstance(payload, dict):
                    payload = {}
                if channel == "system":
                    if msg_type == "ping":
                        await _send_pong(ws, payload)
                        continue
                    if msg_type == "pong":
                        continue
                    if msg_type == "auth.fail":
                        logger.error("relay rejected session: %s", payload.get("reason"))
                        return "__auth_failed__"
                    if msg_type == "auth.ok":
                        # A relay may re-issue a token on reconnect. Keep the
                        # newest one for the next reconnect attempt.
                        token = payload.get("session_token")
                        if isinstance(token, str) and token:
                            self.token = token
                        continue
                    if msg_type == "error":
                        logger.warning("relay system error: %s", payload.get("message"))
                        continue
                    logger.debug("ignoring system/%s", msg_type)
                    continue

                try:
                    result = handler(envelope)
                    if inspect.isawaitable(result):
                        await result
                except Exception:  # noqa: BLE001 - one bad envelope must not kill the socket
                    logger.exception("envelope handler failed for %s/%s", channel, msg_type)
                continue

            if msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
            ):
                return msg.extra or "closed by relay"
            if msg.type == aiohttp.WSMsgType.ERROR:
                return f"websocket error: {ws.exception()}"
            # PING/PONG handled by aiohttp autoping; BINARY is unused.

    def backoff_for(self, attempt: int, reason: str = "") -> float:
        if _is_rate_limited(reason):
            return RATE_LIMIT_DELAY
        exponent = min(max(attempt - 1, 0), RECONNECT_BACKOFF_CEIL)
        return min(self.reconnect_base * (2**exponent), self.reconnect_max)

    # ── Sending ──────────────────────────────────────────────────────────

    async def send(
        self,
        channel: str,
        msg_type: str,
        payload: dict[str, Any],
        *,
        msg_id: str | None = None,
    ) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            raise TransportError("relay transport is not connected")
        envelope = {
            "channel": channel,
            "type": msg_type,
            "id": msg_id or str(uuid.uuid4()),
            "payload": payload,
        }
        async with self._send_lock:
            try:
                await ws.send_str(json.dumps(envelope, ensure_ascii=False))
            except (aiohttp.ClientError, ConnectionError, RuntimeError) as exc:
                raise TransportError(f"failed to send {channel}/{msg_type}: {exc}") from exc

    # ── Heartbeat ────────────────────────────────────────────────────────

    def _start_heartbeat(self) -> None:
        if self._heartbeat_task is not None or self._status_provider is None:
            return
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name="relay-heartbeat"
        )

    async def _stop_heartbeat(self) -> None:
        task, self._heartbeat_task = self._heartbeat_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _heartbeat_loop(self) -> None:
        provider = self._status_provider
        if provider is None:
            return
        while True:
            try:
                payload = provider()
                if inspect.isawaitable(payload):
                    payload = await payload
                await self.send("desktop", "desktop.status", dict(payload))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a failed heartbeat is retried next tick
                logger.debug("heartbeat send failed", exc_info=True)
            try:
                await asyncio.sleep(self.heartbeat_interval)
            except asyncio.CancelledError:
                raise


def _user_agent() -> str:
    from . import __version__

    return f"hermes-relay-python-client/{__version__}"


def _parse_envelope(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("ignoring malformed relay frame (not JSON)")
        return None
    if not isinstance(value, dict):
        return None
    return value


async def _send_pong(ws: aiohttp.ClientWebSocketResponse, payload: dict[str, Any]) -> None:
    envelope = {
        "channel": "system",
        "type": "pong",
        "id": str(uuid.uuid4()),
        "payload": {"ts": payload.get("ts", time.time())},
    }
    try:
        await ws.send_str(json.dumps(envelope))
    except (aiohttp.ClientError, ConnectionError):
        pass


def _is_rate_limited(reason: str) -> bool:
    lowered = (reason or "").lower()
    return "429" in lowered or "rate-limited" in lowered or "rate limited" in lowered


async def _sleep_or_stop(delay: float, stop: asyncio.Event) -> None:
    if delay <= 0:
        return
    try:
        await asyncio.wait_for(stop.wait(), timeout=delay)
    except asyncio.TimeoutError:
        pass


async def _forward_stop(external: asyncio.Event, internal: asyncio.Event) -> None:
    """Bridge an external stop event onto the transport's internal one."""
    try:
        await external.wait()
    except asyncio.CancelledError:
        return
    internal.set()
