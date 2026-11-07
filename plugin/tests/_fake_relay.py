"""In-process fake relay used by the Python client tests.

Speaks just enough of the real relay protocol to drive ``pair`` and ``run``:
``system/auth`` → ``system/auth.ok``, then ``desktop.command`` /
``desktop.response`` / ``desktop.status`` envelopes.  It deliberately uses the
same frozen field names as ``plugin/relay/channels/desktop.py`` so a mismatch
in the client would fail here rather than only against a live relay.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Callable

from aiohttp import WSMsgType, web


class FakeRelay:
    def __init__(
        self,
        *,
        pairing_code: str = "ABC123",
        token: str = "session-token-1",
        server_version: str = "test-relay-1.0",
        reject_auth_reason: str | None = None,
        close_after_status_count: int = 0,
    ) -> None:
        self.pairing_code = pairing_code
        self.token = token
        self.server_version = server_version
        self.reject_auth_reason = reject_auth_reason
        self.close_after_status_count = close_after_status_count

        self.app = web.Application()
        self.app.router.add_get("/ws", self._ws_handler)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self.port: int | None = None

        self.ws: web.WebSocketResponse | None = None
        self.connection_count = 0
        self.auth_requests: list[dict[str, Any]] = []
        self.received: list[dict[str, Any]] = []
        self.status_updates: list[dict[str, Any]] = []
        self.responses: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._connected = asyncio.Event()
        self._closed = asyncio.Event()

    @property
    def url(self) -> str:
        if self.port is None:
            raise RuntimeError("fake relay is not started")
        return f"ws://127.0.0.1:{self.port}/ws"

    async def start(self) -> "FakeRelay":
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        sockets = self._site._server.sockets  # type: ignore[union-attr]
        self.port = sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        ws = self.ws
        if ws is not None and not ws.closed:
            await ws.close()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ── Test helpers ─────────────────────────────────────────────────────

    async def wait_connected(self, timeout: float = 5.0) -> None:
        await asyncio.wait_for(self._connected.wait(), timeout)

    async def wait_connections(self, count: int, timeout: float = 8.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while self.connection_count < count:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError(
                    f"expected {count} connections, saw {self.connection_count}"
                )
            await asyncio.sleep(0.02)

    async def wait_status(
        self,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            for status in reversed(self.status_updates):
                if predicate is None or predicate(status):
                    return status
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError(
                    f"no matching desktop.status heartbeat; saw {len(self.status_updates)}"
                )
            await asyncio.sleep(0.02)

    async def send_command(
        self,
        tool: str,
        args: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
        timeout: float = 6.0,
    ) -> dict[str, Any]:
        await self.wait_connected()
        if self.ws is None or self.ws.closed:
            raise AssertionError("fake relay has no open client socket")
        rid = request_id or str(uuid.uuid4())
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        envelope = {
            "channel": "desktop",
            "type": "desktop.command",
            "id": rid,
            "payload": {
                "request_id": rid,
                "tool": tool,
                "args": args or {},
                "target_device_id": "fake-device",
            },
        }
        await self.ws.send_str(json.dumps(envelope))
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(rid, None)

    async def close_current(self) -> None:
        ws = self.ws
        if ws is not None and not ws.closed:
            await ws.close()

    # ── Protocol handler ─────────────────────────────────────────────────

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=None, max_msg_size=8 * 1024 * 1024)
        await ws.prepare(request)
        self.connection_count += 1

        try:
            first = await asyncio.wait_for(ws.receive(), timeout=10.0)
            if first.type != WSMsgType.TEXT:
                await ws.close()
                return ws
            try:
                envelope = json.loads(first.data)
            except ValueError:
                await self._send_system(ws, "auth.fail", {"reason": "Invalid message format"})
                await ws.close()
                return ws

            if envelope.get("channel") != "system" or envelope.get("type") != "auth":
                await self._send_system(
                    ws, "auth.fail", {"reason": "First message must be system/auth"}
                )
                await ws.close()
                return ws

            payload = envelope.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            self.auth_requests.append(payload)
            self.received.append(envelope)

            if self.reject_auth_reason:
                await self._send_system(ws, "auth.fail", {"reason": self.reject_auth_reason})
                await ws.close()
                return ws

            pairing_ok = bool(self.pairing_code) and payload.get("pairing_code") == self.pairing_code
            token_ok = payload.get("session_token") == self.token
            if not (pairing_ok or token_ok):
                await self._send_system(
                    ws,
                    "auth.fail",
                    {"reason": "Invalid pairing code or session token"},
                )
                await ws.close()
                return ws

            await self._send_system(
                ws,
                "auth.ok",
                {
                    "session_token": self.token,
                    "server_version": self.server_version,
                    "expires_at": None,
                    "grants": {"desktop": None, "chat": None},
                    "transport_hint": "ws",
                },
            )
            self.ws = ws
            self._connected.set()
            self._closed.clear()

            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                try:
                    incoming = json.loads(message.data)
                except ValueError:
                    continue
                self.received.append(incoming)
                await self._handle_envelope(ws, incoming)

            return ws
        finally:
            if self.ws is ws:
                self.ws = None
                self._connected.clear()
            self._closed.set()

    async def _handle_envelope(
        self, ws: web.WebSocketResponse, envelope: dict[str, Any]
    ) -> None:
        msg_type = envelope.get("type", "")
        if isinstance(msg_type, str) and msg_type.startswith("desktop."):
            msg_type = msg_type[len("desktop."):]
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            return

        if msg_type == "status" and envelope.get("channel") == "desktop":
            self.status_updates.append(payload)
            if self.close_after_status_count > 0:
                self.close_after_status_count -= 1
                await ws.close()
            return

        if msg_type == "response" and envelope.get("channel") == "desktop":
            request_id = payload.get("request_id")
            if isinstance(request_id, str):
                self.responses[request_id] = payload
                future = self._pending.get(request_id)
                if future is not None and not future.done():
                    future.set_result(payload)
            return

    async def _send_system(
        self, ws: web.WebSocketResponse, msg_type: str, payload: dict[str, Any]
    ) -> None:
        if ws.closed:
            return
        await ws.send_str(
            json.dumps(
                {
                    "channel": "system",
                    "type": msg_type,
                    "id": str(uuid.uuid4()),
                    "payload": payload,
                }
            )
        )
