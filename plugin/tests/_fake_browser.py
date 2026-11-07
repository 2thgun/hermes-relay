"""Fake CDP server and launcher for the phase-3 browser tests.

The fake server speaks the subset of HTTP/WebSocket CDP the controller uses:
``/json/version``, ``/json/list``, a browser websocket, and a page websocket.
It is a real loopback aiohttp server, so the tests exercise the actual
``aiohttp`` websocket client, request ids, event dispatch and timeouts without
launching Chromium or touching a third-party site.

DOM evaluation is deliberately not a JavaScript engine: expressions emitted by
:mod:`plugin.relay.client.browser.controller` are recognised by their stable
shape and answered from configuration.  The optional live Chromium smoke test
in ``test_python_client_browser_launcher.py`` validates those expressions
against a real browser.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from plugin.relay.client.browser.launcher import BrowserLaunchResult, BrowserStatus

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

_INSPECT_MAX = re.compile(r"const max = (\d+);")
_INSPECT_PREFIX = re.compile(r'const prefix = ("(?:[^"\\]|\\.)*");')
_QUERY_SELECTOR = re.compile(r'const el = document\.querySelector\(("(?:[^"\\]|\\.)*")\);')
_SET_VALUE_TEXT = re.compile(r"setter\.call\(el, (.+?)\); else el\.value")


class FakeCdpServer:
    """Loopback fake of Chrome's DevTools HTTP + WebSocket surface."""

    def __init__(self) -> None:
        self.url = "about:blank"
        self.title = "Fake page"
        self.titles: dict[str, str] = {}
        self.navigate_error: str | None = None
        self.inspect_elements: list[dict[str, Any]] = []
        self.inspect_text = "hello from the fake page"
        self.valid_refs: set[str] = set()
        self.selector_points: dict[str, tuple[int, int]] = {}
        self.invisible_selectors: set[str] = set()
        self.point: tuple[int, int] = (40, 60)
        self.focus_ok = True
        self.clear_ok = True
        self.set_value_ok = True
        self.set_value_text: str | None = None
        self.wait_selector_default = False
        self.wait_selector_sequence: list[bool] = []
        self.wait_text_default = False
        self.wait_text_sequence: list[bool] = []
        self.screenshot_bytes = TINY_PNG
        self.screenshot_params: list[dict[str, Any]] = []
        self.mouse_events: list[dict[str, Any]] = []
        self.key_events: list[dict[str, Any]] = []
        self.inserted_texts: list[str] = []
        self.uploaded_files: list[str] = []
        self.file_input_selectors: set[str] = {"input[type=file]", "#file"}
        self.download_behavior: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.connections: list[str] = []
        self.on_click: Any = None
        self.closed = False
        self._runner: web.AppRunner | None = None
        self._port = 0

    # ── Lifecycle ───────────────────────────────────────────────────────

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    @property
    def browser_ws(self) -> str:
        return f"ws://127.0.0.1:{self._port}/devtools/browser/fake"

    @property
    def page_ws(self) -> str:
        return f"ws://127.0.0.1:{self._port}/devtools/page/page-1"

    def targets(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "page-1",
                "type": "page",
                "url": self.url,
                "title": self.title,
                "webSocketDebuggerUrl": self.page_ws,
            }
        ]

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/json/version", self._version)
        app.router.add_get("/json/list", self._list)
        app.router.add_get("/devtools/browser/{name}", self._websocket)
        app.router.add_get("/devtools/page/{name}", self._websocket)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self._port = int(self._runner.addresses[0][1])

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ── HTTP ────────────────────────────────────────────────────────────

    async def _version(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"Browser": "FakeChrome/1.0", "webSocketDebuggerUrl": self.browser_ws}
        )

    async def _list(self, request: web.Request) -> web.Response:
        return web.json_response(self.targets())

    # ── WebSocket pump ──────────────────────────────────────────────────

    async def _websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=None)
        await ws.prepare(request)
        kind = "browser" if "/devtools/browser/" in request.path else "page"
        self.connections.append(kind)
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                continue
            try:
                payload = json.loads(message.data)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict) or not isinstance(payload.get("method"), str):
                continue
            method = payload["method"]
            params = payload.get("params")
            if not isinstance(params, dict):
                params = {}
            self.calls.append((kind, method, params))
            result, error, events = self._dispatch(kind, method, params)
            response: dict[str, Any] = {"id": payload.get("id")}
            if error is not None:
                response["error"] = error
            else:
                response["result"] = result
            await ws.send_json(response)
            for event in events:
                await ws.send_json(event)
        return ws

    # ── CDP methods ─────────────────────────────────────────────────────

    def _dispatch(
        self, kind: str, method: str, params: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None, list[dict[str, Any]]]:
        handler = getattr(self, f"_cdp_{method.replace('.', '_')}", None)
        if handler is None:
            return {}, {"code": -32601, "message": f"unknown method {method}"}, []
        return handler(kind, params)

    def _cdp_Page_enable(self, kind: str, params: dict[str, Any]):
        return {}, None, []

    def _cdp_Runtime_enable(self, kind: str, params: dict[str, Any]):
        return {}, None, []

    def _cdp_Page_navigate(self, kind: str, params: dict[str, Any]):
        url = str(params.get("url") or "")
        if self.navigate_error:
            return {"frameId": "F1", "errorText": self.navigate_error}, None, []
        self.url = url
        self.title = self.titles.get(url, self.title)
        self.valid_refs.clear()
        return {"frameId": "F1"}, None, [{"method": "Page.loadEventFired", "params": {}}]

    def _cdp_Page_getLayoutMetrics(self, kind: str, params: dict[str, Any]):
        return (
            {
                "cssLayoutViewport": {"clientWidth": 800, "clientHeight": 600},
                "cssContentSize": {"width": 1200, "height": 1600},
            },
            None,
            [],
        )

    def _cdp_Page_captureScreenshot(self, kind: str, params: dict[str, Any]):
        self.screenshot_params.append(dict(params))
        return {"data": base64.b64encode(self.screenshot_bytes).decode("ascii")}, None, []

    def _cdp_Target_getTargets(self, kind: str, params: dict[str, Any]):
        return (
            {
                "targetInfos": [
                    {"targetId": "page-1", "type": "page", "url": self.url, "title": self.title}
                ]
            },
            None,
            [],
        )

    def _cdp_Target_createTarget(self, kind: str, params: dict[str, Any]):
        return {"targetId": "page-1"}, None, []

    def _cdp_Browser_setDownloadBehavior(self, kind: str, params: dict[str, Any]):
        self.download_behavior.append(dict(params))
        return {}, None, []

    def _cdp_Page_setDownloadBehavior(self, kind: str, params: dict[str, Any]):
        self.download_behavior.append(dict(params))
        return {}, None, []

    def _cdp_Browser_close(self, kind: str, params: dict[str, Any]):
        self.closed = True
        return {}, None, []

    def _cdp_DOM_getDocument(self, kind: str, params: dict[str, Any]):
        return {"root": {"nodeId": 1}}, None, []

    def _cdp_DOM_querySelector(self, kind: str, params: dict[str, Any]):
        selector = str(params.get("selector") or "")
        return {"nodeId": 2 if selector in self.file_input_selectors else 0}, None, []

    def _cdp_DOM_setFileInputFiles(self, kind: str, params: dict[str, Any]):
        files = params.get("files")
        self.uploaded_files = [str(item) for item in files] if isinstance(files, list) else []
        return {}, None, []

    def _cdp_Input_dispatchMouseEvent(self, kind: str, params: dict[str, Any]):
        self.mouse_events.append(dict(params))
        if params.get("type") == "mouseReleased" and self.on_click is not None:
            self.on_click()
        return {}, None, []

    def _cdp_Input_dispatchKeyEvent(self, kind: str, params: dict[str, Any]):
        self.key_events.append(dict(params))
        return {}, None, []

    def _cdp_Input_insertText(self, kind: str, params: dict[str, Any]):
        self.inserted_texts.append(str(params.get("text") or ""))
        return {}, None, []

    def _cdp_Runtime_evaluate(self, kind: str, params: dict[str, Any]):
        expression = str(params.get("expression") or "")
        try:
            value = self._evaluate(expression)
        except Exception as exc:  # noqa: BLE001 - surfaced as a CDP exception
            return {"exceptionDetails": {"text": str(exc)}}, None, []
        return {"result": {"type": "object", "value": value}}, None, []

    # ── Expression interpreter ──────────────────────────────────────────

    def _evaluate(self, expression: str) -> Any:
        expr = expression.strip()
        if expr.startswith("({url: location.href"):
            return {"url": self.url, "title": self.title}
        if "data-hermes-ref" in expr and "querySelectorAll" in expr:
            return self._inspect(expr)
        if "scrollIntoView" in expr:
            return self._resolve_point(expr)
        if "execCommand('selectAll')" in expr:
            return self.clear_ok
        if "dispatchEvent(new Event('input'" in expr:
            match = _SET_VALUE_TEXT.search(expr)
            if match:
                try:
                    self.set_value_text = json.loads(match.group(1))
                except (TypeError, ValueError):
                    self.set_value_text = match.group(1)
            return self.set_value_ok
        if "el.focus()" in expr:
            return self.focus_ok
        if expr.startswith("!!document.querySelector("):
            if self.wait_selector_sequence:
                return self.wait_selector_sequence.pop(0)
            return self.wait_selector_default
        if ".includes(" in expr:
            if self.wait_text_sequence:
                return self.wait_text_sequence.pop(0)
            return self.wait_text_default
        raise AssertionError(f"fake CDP cannot interpret expression: {expr[:200]}")

    def _inspect(self, expression: str) -> dict[str, Any]:
        match = _INSPECT_MAX.search(expression)
        limit = int(match.group(1)) if match else 60
        prefix_match = _INSPECT_PREFIX.search(expression)
        prefix = json.loads(prefix_match.group(1)) if prefix_match else "hfake"
        elements: list[dict[str, Any]] = []
        for index, element in enumerate(self.inspect_elements[:limit]):
            ref = f"{prefix}-{index + 1}"
            elements.append({**element, "ref": ref})
        self.valid_refs = {element["ref"] for element in elements}
        payload: dict[str, Any] = {
            "url": self.url,
            "title": self.title,
            "elements": elements,
            "truncated": len(self.inspect_elements) >= limit,
        }
        if "text: document.body" in expression:
            payload["text"] = self.inspect_text
        return payload

    def _resolve_point(self, expression: str) -> dict[str, Any]:
        match = _QUERY_SELECTOR.search(expression)
        if not match:
            raise AssertionError("resolve expression has no selector")
        selector = json.loads(match.group(1))
        if selector.startswith('[data-hermes-ref="'):
            ref = selector[len('[data-hermes-ref="') : -2]
            if ref not in self.valid_refs:
                return {"error": "not_found"}
            return {"x": self.point[0], "y": self.point[1]}
        if selector in self.invisible_selectors:
            return {"error": "not_visible"}
        point = self.selector_points.get(selector)
        if point is None:
            return {"error": "not_found"}
        return {"x": point[0], "y": point[1]}


class FakeBrowserLauncher:
    """In-memory launcher that points the controller at :class:`FakeCdpServer`."""

    def __init__(self, server: FakeCdpServer, profile_dir: Path | None = None) -> None:
        self.server = server
        self.profile_dir = str(profile_dir or "/tmp/fake-browser-profile")
        self.download_dir = str((profile_dir or Path("/tmp/fake-browser-profile")) / "downloads")
        self.download: dict[str, Any] | None = None
        self.ensure_calls: list[bool | None] = []
        self.closed = False
        self.status_calls = 0

    async def ensure(self, *, headless: bool | None = None) -> BrowserLaunchResult:
        self.ensure_calls.append(headless)
        return BrowserLaunchResult(
            endpoint=self.server.endpoint,
            web_socket_url=self.server.browser_ws,
            profile_dir=self.profile_dir,
            binary_path="/usr/bin/chromium",
            launched=False,
            browser_version="FakeChrome/1.0",
        )

    async def status(self) -> BrowserStatus:
        self.status_calls += 1
        return BrowserStatus(
            available=True,
            running=True,
            profile_dir=self.profile_dir,
            binary_path="/usr/bin/chromium",
            endpoint=self.server.endpoint,
            web_socket_url=self.server.browser_ws,
            browser_version="FakeChrome/1.0",
        )

    async def list_targets(self, endpoint: str) -> list[dict[str, Any]]:
        return self.server.targets()

    async def downloads_path(self) -> str:
        Path(self.download_dir).mkdir(parents=True, exist_ok=True)
        return self.download_dir

    async def last_download(self) -> dict[str, Any] | None:
        return self.download

    async def close_gracefully(self) -> None:
        self.closed = True
        self.server.closed = True


async def wait_until(predicate: Any, timeout: float = 2.0) -> None:
    """Tiny polling helper for tests that need the server to catch up."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met before the deadline")
