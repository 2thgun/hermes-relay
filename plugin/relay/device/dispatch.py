"""Tool registry and request dispatch for the Python relay client.

The relay sends ``desktop.command`` envelopes with a ``request_id``, the tool
name, and an argument object.  The dispatcher runs the matching handler under
a per-request timeout and always answers with a ``desktop.response`` envelope
so the server's pending future never hangs — including for unknown tools,
handler exceptions, and timeouts.

Response shape (matches ``desktop/src/tools/router.ts``)::

    {"request_id": "...", "ok": true,  "result": <json>}
    {"request_id": "...", "ok": false, "error": "<message>"}

Unknown tool calls carry a machine-readable ``code: "unsupported_tool"`` in
addition to the human-readable error string.  The relay passes the payload
through to the agent verbatim, so extra keys are harmless.

Cancellation is local: the protocol has no cancel frame.  A request is aborted
by cancelling its ``asyncio`` task (timeout, socket teardown, or explicit
``cancel_all``); handlers are expected to terminate child processes in their
``CancelledError`` path.  ``ToolContext.cancel`` lets cooperative handlers
notice the abort at await points.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

HANDLER_TIMEOUT = 30.0
RESPONSE_WIRE_BUDGET_BYTES = 3 * 1024 * 1024
_ENVELOPE_ID_PLACEHOLDER = "00000000-0000-0000-0000-000000000000"


class ToolError(Exception):
    """Handler-level failure serialized as ``{ok: false, error}``."""


class UnsupportedTool(ToolError):
    """The requested tool is not implemented by this client."""

    def __init__(self, tool: str) -> None:
        super().__init__(f"unsupported_tool: {tool}")
        self.tool = tool


class ToolCancelled(Exception):
    """The request was aborted (timeout or transport teardown)."""


@dataclass
class ToolContext:
    """Per-call execution context handed to every tool handler."""

    cwd: str
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    interactive: bool = False
    request_id: str = ""
    started_at: float = field(default_factory=time.monotonic)

    @property
    def cancelled(self) -> bool:
        return self.cancel.is_set()

    def check_cancelled(self) -> None:
        if self.cancel.is_set():
            raise ToolCancelled("aborted")


ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[Any]]


class ToolRegistry:
    """Name → async handler map with an optional allowlist.

    Only names actually present in the registry are advertised, so the relay's
    advertised-tool gating never routes a tool this client cannot serve.
    """

    def __init__(self, *, allow: set[str] | None = None, deny: set[str] | None = None) -> None:
        self._handlers: dict[str, ToolHandler] = {}
        self._allow = allow
        self._deny = deny or set()

    def add(self, name: str, handler: ToolHandler) -> None:
        self._handlers[name] = handler

    def extend(self, handlers: dict[str, ToolHandler]) -> None:
        self._handlers.update(handlers)

    def has(self, name: str) -> bool:
        return name in self.names()

    def names(self) -> list[str]:
        names = set(self._handlers)
        if self._allow is not None:
            names &= set(self._allow)
        names -= set(self._deny)
        return sorted(names)

    async def invoke(self, name: str, args: dict[str, Any], ctx: ToolContext) -> Any:
        handler = self._handlers.get(name)
        if handler is None or name not in self.names():
            raise UnsupportedTool(name)
        result = handler(args, ctx)
        if not hasattr(result, "__await__"):
            raise ToolError(f"handler for {name} is not async")
        return await result


@dataclass
class ClientState:
    """Mutable diagnostics surfaced by ``desktop_health`` and the CLI."""

    started_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    url: str | None = None
    device_id: str | None = None
    device_name: str | None = None
    cwd: str = field(default_factory=os.getcwd)
    connected: bool = False
    handled: int = 0
    failed: int = 0
    last_error: dict[str, Any] | None = None
    last_tool: str | None = None

    def record_success(self, tool: str) -> None:
        self.handled += 1
        self.last_tool = tool

    def record_failure(self, tool: str, message: str) -> None:
        self.failed += 1
        self.last_tool = tool
        self.last_error = {"message": message, "tool": tool, "ts": int(time.time() * 1000)}

    def snapshot(self) -> dict[str, Any]:
        return {
            "started_at_ms": self.started_at_ms,
            "uptime_ms": int(time.time() * 1000) - self.started_at_ms,
            "url": self.url,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "cwd": self.cwd,
            "connected": self.connected,
            "handled": self.handled,
            "failed": self.failed,
            "last_tool": self.last_tool,
            "last_error": self.last_error,
        }


SendResponse = Callable[[dict[str, Any]], Awaitable[None]]


class DesktopDispatcher:
    """Runs ``desktop.command`` payloads and emits ``desktop.response``."""

    def __init__(
        self,
        registry: ToolRegistry,
        send: SendResponse,
        *,
        state: ClientState | None = None,
        cwd: str | None = None,
        timeout: float = HANDLER_TIMEOUT,
        tool_timeouts: Mapping[str, float] | None = None,
        max_wire_bytes: int = RESPONSE_WIRE_BUDGET_BYTES,
        interactive: bool = False,
        on_result: Callable[[str, dict[str, Any], bool], None] | None = None,
    ) -> None:
        self.registry = registry
        self._send = send
        self.state = state or ClientState()
        self.cwd = cwd or os.getcwd()
        self.timeout = timeout
        self.tool_timeouts = dict(tool_timeouts or {})
        self.max_wire_bytes = max_wire_bytes
        self.interactive = interactive
        self.on_result = on_result
        self._inflight: dict[str, asyncio.Task[None]] = {}
        self._cancels: dict[str, asyncio.Event] = {}
        self._closed = False

    # ── Envelope entry point ─────────────────────────────────────────────

    async def handle_envelope(self, envelope: dict[str, Any]) -> None:
        """Handle one relay envelope. Non-desktop channels are ignored."""
        channel = envelope.get("channel")
        if channel != "desktop":
            logger.debug("dispatcher ignoring channel %r", channel)
            return
        msg_type = envelope.get("type")
        if isinstance(msg_type, str) and msg_type.startswith("desktop."):
            msg_type = msg_type[len("desktop."):]
        if msg_type != "command":
            # desktop.status/workspace/active_editor are client→server; the
            # server's desktop.control_session_end is for computer-use, which
            # is out of phase 1 scope.
            logger.debug("dispatcher ignoring desktop/%s", msg_type)
            return

        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            logger.warning("desktop.command payload is not an object; dropping")
            return
        request_id = payload.get("request_id")
        tool = payload.get("tool")
        args = payload.get("args")
        if (
            not isinstance(request_id, str)
            or not request_id
            or not isinstance(tool, str)
            or not tool
            or not isinstance(args, dict)
        ):
            # Without a request_id there is nothing to correlate a response
            # with, so a malformed command can only be logged and dropped.
            logger.warning("malformed desktop.command (request_id=%r tool=%r)", request_id, tool)
            return

        if self._closed:
            await self._respond(
                {"request_id": request_id, "ok": False, "error": "client is shutting down"}
            )
            return

        if request_id in self._inflight:
            await self._respond(
                {
                    "request_id": request_id,
                    "ok": False,
                    "error": "duplicate request_id",
                    "code": "duplicate_request_id",
                }
            )
            return

        task = asyncio.create_task(
            self._dispatch(request_id, tool, args), name=f"desktop-{tool}-{request_id[:8]}"
        )
        self._inflight[request_id] = task

        def _done(_: asyncio.Task[None]) -> None:
            self._inflight.pop(request_id, None)
            self._cancels.pop(request_id, None)

        task.add_done_callback(_done)

    # ── Dispatch ─────────────────────────────────────────────────────────

    async def _dispatch(self, request_id: str, tool: str, args: dict[str, Any]) -> None:
        started = time.monotonic()
        cancel = asyncio.Event()
        self._cancels[request_id] = cancel
        ctx = ToolContext(
            cwd=self.cwd,
            cancel=cancel,
            interactive=self.interactive,
            request_id=request_id,
        )

        if not self.registry.has(tool):
            message = f"unsupported_tool: {tool}"
            await self._respond(
                {
                    "request_id": request_id,
                    "ok": False,
                    "error": message,
                    "code": "unsupported_tool",
                    "tool": tool,
                }
            )
            self.state.record_failure(tool, message)
            self._emit_result(tool, {"error": message}, False)
            return

        handler_task = asyncio.create_task(
            self.registry.invoke(tool, args, ctx), name=f"invoke-{tool}"
        )
        timeout = self.tool_timeouts.get(tool, self.timeout)
        try:
            done, _pending = await asyncio.wait({handler_task}, timeout=timeout)
        except asyncio.CancelledError:
            # The dispatcher task itself was cancelled (socket teardown).
            # ``asyncio.wait`` does not propagate cancellation into its
            # children, so cancel the handler explicitly before unwinding.
            cancel.set()
            handler_task.cancel()
            await _reap(handler_task)
            raise
        if not done:
            cancel.set()
            handler_task.cancel()
            message = f"timed out after {int(timeout * 1000)}ms"
            await self._respond({"request_id": request_id, "ok": False, "error": message})
            self.state.record_failure(tool, message)
            self._emit_result(tool, {"error": message}, False)
            await _reap(handler_task)
            return

        try:
            result = handler_task.result()
        except ToolCancelled:
            await self._respond({"request_id": request_id, "ok": False, "error": "aborted"})
            self.state.record_failure(tool, "aborted")
            self._emit_result(tool, {"error": "aborted"}, False)
            return
        except asyncio.CancelledError:
            await self._respond({"request_id": request_id, "ok": False, "error": "aborted"})
            self.state.record_failure(tool, "aborted")
            self._emit_result(tool, {"error": "aborted"}, False)
            return
        except Exception as exc:  # noqa: BLE001 - every handler error is a tool result
            message = str(exc) or exc.__class__.__name__
            await self._respond({"request_id": request_id, "ok": False, "error": message})
            self.state.record_failure(tool, message)
            self._emit_result(tool, {"error": message}, False)
            return

        payload = {"request_id": request_id, "ok": True, "result": result}
        await self._respond(payload)
        self.state.record_success(tool)
        self._emit_result(tool, {"result": result}, True)
        logger.debug(
            "served %s request_id=%s in %.1fms",
            tool,
            request_id,
            (time.monotonic() - started) * 1000,
        )

    async def _respond(self, payload: dict[str, Any]) -> None:
        fitted = self._fit_to_wire(payload)
        try:
            await self._send(fitted)
        except Exception:  # noqa: BLE001 - transport teardown races are expected
            logger.warning("failed to send desktop.response for %s", payload.get("request_id"))

    def _fit_to_wire(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Keep one oversized result from tripping the relay's 4 MiB frame cap."""
        try:
            size = len(
                json.dumps(
                    {
                        "channel": "desktop",
                        "type": "desktop.response",
                        "id": _ENVELOPE_ID_PLACEHOLDER,
                        "payload": payload,
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            )
        except (TypeError, ValueError):
            return {
                "request_id": payload.get("request_id"),
                "ok": False,
                "error": (
                    "Desktop result could not be serialized. Retry with bounded text "
                    "or save the output to a file."
                ),
            }
        if size <= self.max_wire_bytes:
            return payload
        return {
            "request_id": payload.get("request_id"),
            "ok": False,
            "error": (
                f"Desktop result exceeded the {self.max_wire_bytes}-byte relay response "
                "budget. Retry with bounded output or save the result to a file."
            ),
        }

    def _emit_result(self, tool: str, payload: dict[str, Any], ok: bool) -> None:
        if self.on_result is None:
            return
        try:
            self.on_result(tool, payload, ok)
        except Exception:  # noqa: BLE001 - local hooks are best-effort
            logger.debug("on_result hook failed", exc_info=True)

    # ── Shutdown ─────────────────────────────────────────────────────────

    async def cancel_all(self, reason: str = "transport closed") -> None:
        """Abort every in-flight handler (socket teardown path)."""
        for cancel in list(self._cancels.values()):
            cancel.set()
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), timeout=5.0
                )
            except asyncio.TimeoutError:
                logger.warning("some desktop handlers ignored cancellation")
        self._inflight.clear()
        self._cancels.clear()
        logger.info("cancelled %d in-flight desktop request(s): %s", len(tasks), reason)

    async def close(self) -> None:
        self._closed = True
        await self.cancel_all("client shutting down")


async def _reap(task: asyncio.Task[None]) -> None:
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


def envelope_error(message: str, request_id: str | None = None) -> dict[str, Any]:
    """Build a minimal error payload (used by the CLI's fatal paths)."""
    payload: dict[str, Any] = {"ok": False, "error": message}
    if request_id:
        payload["request_id"] = request_id
    return payload
