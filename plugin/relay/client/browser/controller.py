"""High-level browser control over CDP for the Python relay client.

Ported from ``desktop/src/tools/browser/controller.ts``.  The controller works
on one dedicated automation page at a time.  Structured inspection assigns
``data-hermes-ref`` handles so the agent can click/type by reference instead of
guessing pixels; raw coordinates remain available.  Refs expire on navigation
and on every new inspection, and the error text says so.

All navigation is restricted to ``http(s)`` and ``about:blank``.  Downloads
land in the automation profile's ``downloads/`` directory.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from collections.abc import Callable, Sequence
from string import Template
from typing import Any

from ..platform.base import image_size
from .cdp import CdpConnection, open_cdp
from .launcher import BrowserLauncher

logger = logging.getLogger(__name__)

MAX_ELEMENTS_LIMIT = 200
MAX_TYPE_CHARS = 4_000
DOWNLOAD_TIMEOUT_MS = 30_000
MAX_DOWNLOAD_TIMEOUT_MS = 120_000
MIN_DOWNLOAD_TIMEOUT_MS = 1_000
WAIT_TIMEOUT_MS = 10_000
MAX_WAIT_TIMEOUT_MS = 60_000
MIN_WAIT_TIMEOUT_MS = 250

MODIFIER_BITS: dict[str, int] = {
    "alt": 1,
    "ctrl": 2,
    "control": 2,
    "meta": 4,
    "cmd": 4,
    "shift": 8,
}

NAMED_KEYS: dict[str, dict[str, Any]] = {
    "enter": {"key": "Enter", "code": "Enter", "vk": 13, "text": "\r"},
    "return": {"key": "Enter", "code": "Enter", "vk": 13, "text": "\r"},
    "tab": {"key": "Tab", "code": "Tab", "vk": 9},
    "escape": {"key": "Escape", "code": "Escape", "vk": 27},
    "esc": {"key": "Escape", "code": "Escape", "vk": 27},
    "backspace": {"key": "Backspace", "code": "Backspace", "vk": 8},
    "delete": {"key": "Delete", "code": "Delete", "vk": 46},
    "del": {"key": "Delete", "code": "Delete", "vk": 46},
    "space": {"key": " ", "code": "Space", "vk": 32, "text": " "},
    "arrowup": {"key": "ArrowUp", "code": "ArrowUp", "vk": 38},
    "up": {"key": "ArrowUp", "code": "ArrowUp", "vk": 38},
    "arrowdown": {"key": "ArrowDown", "code": "ArrowDown", "vk": 40},
    "down": {"key": "ArrowDown", "code": "ArrowDown", "vk": 40},
    "arrowleft": {"key": "ArrowLeft", "code": "ArrowLeft", "vk": 37},
    "left": {"key": "ArrowLeft", "code": "ArrowLeft", "vk": 37},
    "arrowright": {"key": "ArrowRight", "code": "ArrowRight", "vk": 39},
    "right": {"key": "ArrowRight", "code": "ArrowRight", "vk": 39},
    "home": {"key": "Home", "code": "Home", "vk": 36},
    "end": {"key": "End", "code": "End", "vk": 35},
    "pageup": {"key": "PageUp", "code": "PageUp", "vk": 33},
    "pagedown": {"key": "PageDown", "code": "PageDown", "vk": 34},
    "insert": {"key": "Insert", "code": "Insert", "vk": 45},
}

_ALLOWED_URL = re.compile(r"^https?://", re.IGNORECASE)
_REF_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

STATE_JS = "({url: location.href, title: document.title})"

_INSPECT_JS = Template(
    r"""(() => {
  const max = ${max};
  const prefix = ${prefix};
  const selector = 'a[href],button,input:not([type=hidden]),textarea,select,[role=button],[role=link],[role=checkbox],[role=tab],[role=menuitem],summary,[contenteditable=true]';
  const nodes = Array.from(document.querySelectorAll(selector));
  const elements = [];
  for (const el of nodes) {
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || Number(style.opacity) === 0) continue;
    const ref = prefix + '-' + (elements.length + 1);
    el.setAttribute('data-hermes-ref', ref);
    const tag = el.tagName.toLowerCase();
    const rawName = el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('placeholder') ||
      (el.innerText || '').trim() || (el.value || '');
    elements.push({
      ref,
      tag,
      role: el.getAttribute('role') || undefined,
      type: el.getAttribute('type') || undefined,
      name: String(rawName).replace(/\s+/g, ' ').trim().slice(0, 160),
      value: 'value' in el && typeof el.value === 'string' ? el.value.slice(0, 300) : undefined,
      disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
      checked: 'checked' in el ? Boolean(el.checked) : undefined,
      rect: { x: Math.round(rect.x + scrollX), y: Math.round(rect.y + scrollY), width: Math.round(rect.width), height: Math.round(rect.height) }
    });
    if (elements.length >= max) break;
  }
  return {
    url: location.href,
    title: document.title,
    elements,
    truncated: elements.length >= max,
    text: ${text}
  };
})()"""
)

_RESOLVE_JS = Template(
    r"""(() => {
  const el = document.querySelector(${selector});
  if (!el) return {error: 'not_found'};
  el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
  const rect = el.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return {error: 'not_visible'};
  return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
})()"""
)

_CLEAR_JS = """(() => {
  const el = document.activeElement;
  if (!el) return false;
  if (el.isContentEditable) { document.execCommand('selectAll'); return true; }
  if (typeof el.select === 'function') { el.select(); return true; }
  return false;
})()"""

_FOCUS_JS = Template(
    """(() => { const el = document.querySelector(${selector}); if (!el) return false; el.focus(); return true; })()"""
)

_SET_VALUE_JS = Template(
    r"""(() => {
  const el = document.querySelector(${selector});
  if (!el) return false;
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
  const setter = descriptor && descriptor.set;
  if (setter) setter.call(el, ${text}); else el.value = ${text};
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return true;
})()"""
)

_WAIT_SELECTOR_JS = Template("!!document.querySelector(${selector})")
_WAIT_TEXT_JS = Template("(document.body ? document.body.innerText : '').includes(${text})")


class BrowserError(Exception):
    """A browser operation failed with an agent-readable message."""


def ensure_allowed_url(url: str) -> str:
    """Return a trimmed http(s)/about:blank URL or raise ``BrowserError``."""
    value = (url or "").strip()
    if not value:
        raise BrowserError("url is required")
    if not _ALLOWED_URL.match(value) and value.lower() != "about:blank":
        raise BrowserError("Only http(s) URLs (or about:blank) are allowed.")
    return value


def ref_selector(ref: str) -> str:
    if not _REF_PATTERN.fullmatch(ref or ""):
        raise BrowserError("Invalid element reference.")
    return f'[data-hermes-ref="{ref}"]'


def modifier_mask(parts: Sequence[str]) -> int:
    mask = 0
    for part in parts:
        mask |= MODIFIER_BITS.get(part.lower(), 0)
    return mask


def key_descriptor(part: str) -> dict[str, Any]:
    lower = part.lower()
    named = NAMED_KEYS.get(lower)
    if named is not None:
        return dict(named)
    match = re.fullmatch(r"f([1-9]|1[0-2])", lower)
    if match:
        number = int(match.group(1))
        return {"key": part.upper(), "code": part.upper(), "vk": 111 + number}
    if re.fullmatch(r"[a-z]", lower):
        return {
            "key": lower,
            "code": f"Key{lower.upper()}",
            "vk": ord(lower.upper()),
            "text": lower,
        }
    if re.fullmatch(r"[0-9]", lower):
        return {"key": lower, "code": f"Digit{lower}", "vk": ord(lower), "text": lower}
    raise BrowserError(
        f"Unsupported key {part}. Use letters, digits, f1-f12, or named keys "
        "(enter, tab, escape, arrows, pageup, pagedown, home, end, backspace, "
        "delete, space)."
    )


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


class BrowserController:
    """One dedicated automation page at a time, over two CDP connections."""

    def __init__(
        self,
        launcher: BrowserLauncher | None = None,
        *,
        timeout_ms: int = 20_000,
        open_cdp_fn: Callable[..., Any] = open_cdp,
        wait_poll_s: float = 0.25,
        download_poll_s: float = 0.3,
        download_stable_ms: int = 800,
    ) -> None:
        self.launcher = launcher or BrowserLauncher()
        self.timeout_ms = timeout_ms
        self._open_cdp = open_cdp_fn
        self._wait_poll_s = wait_poll_s
        self._download_poll_s = download_poll_s
        self._download_stable_ms = download_stable_ms
        self._browser: CdpConnection | None = None
        self._browser_ws: str | None = None
        self._page: CdpConnection | None = None
        self._page_target_id: str | None = None
        self._inspect_counter = 0

    # ── Connections ─────────────────────────────────────────────────────

    async def connect_browser(self) -> CdpConnection:
        launch = await self.launcher.ensure()
        if (
            self._browser is not None
            and self._browser_ws == launch.web_socket_url
            and not self._browser.is_closed
        ):
            return self._browser
        if self._browser is not None:
            await self._browser.close()
        self._browser = await self._open_cdp(launch.web_socket_url)
        self._browser_ws = launch.web_socket_url
        return self._browser

    async def connect_page(self) -> tuple[CdpConnection, dict[str, str]]:
        browser = await self.connect_browser()
        status = await self.launcher.status()
        if not status.endpoint:
            raise BrowserError("Browser endpoint disappeared.")
        targets = await self.launcher.list_targets(status.endpoint)
        page_target = next(
            (
                target
                for target in targets
                if target.get("type") == "page"
                and isinstance(target.get("webSocketDebuggerUrl"), str)
                and target.get("webSocketDebuggerUrl")
            ),
            None,
        )
        if page_target is None:
            await browser.send("Target.createTarget", {"url": "about:blank"})
            await asyncio.sleep(0.3)
            targets = await self.launcher.list_targets(status.endpoint)
            page_target = next(
                (
                    target
                    for target in targets
                    if target.get("type") == "page"
                    and isinstance(target.get("webSocketDebuggerUrl"), str)
                    and target.get("webSocketDebuggerUrl")
                ),
                None,
            )
        if page_target is None or not isinstance(page_target.get("webSocketDebuggerUrl"), str):
            raise BrowserError("The browser has no inspectable page target.")

        target_id = str(page_target.get("id"))
        page = self._page
        if page is None or page.is_closed or self._page_target_id != target_id:
            if self._page is not None:
                await self._page.close()
            page = await self._open_cdp(page_target["webSocketDebuggerUrl"])
            self._page = page
            self._page_target_id = target_id
            for method in ("Page.enable", "Runtime.enable"):
                try:
                    await page.send(method)
                except Exception:  # noqa: BLE001 - a page without domains is still usable
                    logger.debug("%s failed during page setup", method, exc_info=True)
        return page, {
            "id": target_id,
            "url": str(page_target.get("url") or ""),
            "title": str(page_target.get("title") or ""),
        }

    # ── Status ──────────────────────────────────────────────────────────

    async def status(self) -> dict[str, Any]:
        status = await self.launcher.status()
        pages: list[dict[str, Any]] = []
        if status.running and status.endpoint:
            try:
                targets = await self.launcher.list_targets(status.endpoint)
            except Exception:  # noqa: BLE001 - status must still answer
                targets = []
            pages = [
                {
                    "id": target.get("id"),
                    "url": target.get("url", ""),
                    "title": target.get("title", ""),
                }
                for target in targets
                if target.get("type") == "page"
            ]
        payload = status.to_dict()
        payload["pages"] = pages
        return payload

    async def pages(self) -> list[dict[str, Any]]:
        browser = await self.connect_browser()
        result = await browser.send("Target.getTargets")
        infos = result.get("targetInfos") if isinstance(result, dict) else None
        if not isinstance(infos, list):
            return []
        return [
            {"id": info.get("targetId"), "url": info.get("url", ""), "title": info.get("title", "")}
            for info in infos
            if isinstance(info, dict) and info.get("type") == "page"
        ]

    # ── Navigation ──────────────────────────────────────────────────────

    async def open(self, url: str, *, headless: bool | None = None) -> dict[str, Any]:
        target = ensure_allowed_url(url)
        if headless is not None:
            await self.launcher.ensure(headless=headless)
        page, _info = await self.connect_page()
        await self._navigate(page, target)
        return await self._page_state(page, "navigated")

    async def navigate(self, url: str) -> dict[str, Any]:
        target = ensure_allowed_url(url)
        page, _info = await self.connect_page()
        await self._navigate(page, target)
        return await self._page_state(page, "navigated")

    async def _navigate(self, page: CdpConnection, url: str) -> None:
        loaded = asyncio.create_task(
            page.wait_for_event("Page.loadEventFired", self.timeout_ms), name="cdp-page-load"
        )
        try:
            navigation = await page.send("Page.navigate", {"url": url}, timeout_ms=self.timeout_ms)
        except Exception:
            loaded.cancel()
            await _reap(loaded)
            raise
        error_text = navigation.get("errorText") if isinstance(navigation, dict) else None
        if error_text:
            loaded.cancel()
            await _reap(loaded)
            raise BrowserError(f"Navigation failed: {error_text}")
        try:
            await loaded
        except Exception:  # noqa: BLE001 - a load event can be missed for same-page URLs
            logger.debug("no Page.loadEventFired for %s", url, exc_info=True)

    async def _page_state(self, page: CdpConnection, detail: str) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": True, "detail": detail}
        try:
            value = await self._evaluate(page, STATE_JS)
        except Exception:  # noqa: BLE001 - a closing page still yields a usable detail
            return payload
        if isinstance(value, dict):
            if isinstance(value.get("url"), str):
                payload["url"] = value["url"]
            if isinstance(value.get("title"), str):
                payload["title"] = value["title"]
        return payload

    # ── Evaluation ──────────────────────────────────────────────────────

    async def _evaluate(
        self,
        page: CdpConnection,
        expression: str,
        *,
        timeout_ms: int | None = None,
    ) -> Any:
        result = await page.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": False},
            timeout_ms=timeout_ms or self.timeout_ms,
        )
        details = result.get("exceptionDetails") if isinstance(result, dict) else None
        if details:
            exception = details.get("exception") if isinstance(details, dict) else None
            description = exception.get("description") if isinstance(exception, dict) else None
            text = details.get("text") if isinstance(details, dict) else None
            raise BrowserError(str(description or text or "JavaScript evaluation failed."))
        inner = result.get("result") if isinstance(result, dict) else None
        if not isinstance(inner, dict):
            return None
        return inner.get("value")

    # ── Inspect ─────────────────────────────────────────────────────────

    async def inspect(
        self,
        *,
        max_elements: int | None = None,
        include_text: bool = False,
    ) -> dict[str, Any]:
        page, _info = await self.connect_page()
        limit = _clamp(int(max_elements if max_elements is not None else 60), 1, MAX_ELEMENTS_LIMIT)
        prefix = f"h{int(time.time() * 1000):x}{self._inspect_counter}"
        self._inspect_counter += 1
        expression = _INSPECT_JS.substitute(
            max=limit,
            prefix=json.dumps(prefix),
            text=(
                "document.body ? document.body.innerText.slice(0, 6000) : ''"
                if include_text
                else "undefined"
            ),
        )
        value = await self._evaluate(page, expression)
        if not isinstance(value, dict):
            raise BrowserError("Inspection returned no value.")
        return value

    async def _resolve_point(
        self,
        *,
        ref: str | None = None,
        selector: str | None = None,
        x: int | None = None,
        y: int | None = None,
    ) -> tuple[int, int]:
        if x is not None and y is not None:
            return int(round(x)), int(round(y))
        query = ref_selector(ref) if ref else selector
        if not query:
            raise BrowserError("Provide ref, selector, or x and y.")
        page, _info = await self.connect_page()
        value = await self._evaluate(page, _RESOLVE_JS.substitute(selector=json.dumps(query)))
        if not isinstance(value, dict) or value.get("error"):
            if isinstance(value, dict) and value.get("error") == "not_found":
                raise BrowserError(
                    f"No element matches {query}. Re-run desktop_browser_inspect; "
                    "references expire after navigation."
                )
            raise BrowserError("Element is not visible.")
        return int(round(value.get("x") or 0)), int(round(value.get("y") or 0))

    # ── Pointer / keyboard ──────────────────────────────────────────────

    async def _mouse(
        self,
        page: CdpConnection,
        event_type: str,
        x: int,
        y: int,
        extras: dict[str, Any] | None = None,
    ) -> None:
        await page.send(
            "Input.dispatchMouseEvent",
            {"type": event_type, "x": x, "y": y, **(extras or {})},
            timeout_ms=self.timeout_ms,
        )

    async def _click_point(
        self, page: CdpConnection, x: int, y: int, button: str, count: int
    ) -> None:
        await self._mouse(page, "mouseMoved", x, y)
        buttons = 1 if button == "left" else 2 if button == "right" else 4
        for index in range(1, count + 1):
            await self._mouse(
                page,
                "mousePressed",
                x,
                y,
                {"button": button, "clickCount": index, "buttons": buttons},
            )
            await self._mouse(
                page,
                "mouseReleased",
                x,
                y,
                {"button": button, "clickCount": index, "buttons": 0},
            )

    async def click(
        self,
        *,
        ref: str | None = None,
        selector: str | None = None,
        x: int | None = None,
        y: int | None = None,
        button: str = "left",
        count: int = 1,
    ) -> dict[str, Any]:
        page, _info = await self.connect_page()
        point_x, point_y = await self._resolve_point(
            ref=ref, selector=selector, x=x, y=y
        )
        chosen_button = button if button in ("left", "right", "middle") else "left"
        click_count = _clamp(int(count), 1, 3)
        await self._click_point(page, point_x, point_y, chosen_button, click_count)
        state = await self._page_state(page, f"clicked {chosen_button}")
        return {**state, "x": point_x, "y": point_y, "click_count": click_count}

    async def press_key(self, page: CdpConnection, combo: str) -> None:
        parts = [part.strip() for part in (combo or "").split("+") if part.strip()]
        if not parts:
            raise BrowserError("Empty key combination.")
        descriptor = key_descriptor(parts[-1])
        modifiers = modifier_mask(parts[:-1])
        base = {
            "key": descriptor["key"],
            "code": descriptor["code"],
            "windowsVirtualKeyCode": descriptor["vk"],
            "nativeVirtualKeyCode": descriptor["vk"],
            "modifiers": modifiers,
        }
        event_type = "keyDown" if descriptor.get("text") and modifiers == 0 else "rawKeyDown"
        payload = {"type": event_type, **base}
        if descriptor.get("text") and modifiers == 0:
            payload["text"] = descriptor["text"]
            payload["unmodifiedText"] = descriptor["text"]
        await page.send("Input.dispatchKeyEvent", payload, timeout_ms=self.timeout_ms)
        await page.send(
            "Input.dispatchKeyEvent", {"type": "keyUp", **base}, timeout_ms=self.timeout_ms
        )

    async def type(
        self,
        *,
        text: str,
        ref: str | None = None,
        selector: str | None = None,
        clear: bool = False,
        submit: bool = False,
        mode: str = "insert",
    ) -> dict[str, Any]:
        page, _info = await self.connect_page()
        if len(text) > MAX_TYPE_CHARS:
            raise BrowserError(f"text is limited to {MAX_TYPE_CHARS} characters per call.")
        query: str | None = ref_selector(ref) if ref else selector
        if ref or selector:
            point_x, point_y = await self._resolve_point(ref=ref, selector=selector)
            await self._click_point(page, point_x, point_y, "left", 1)
            await self._evaluate(page, _FOCUS_JS.substitute(selector=json.dumps(query)))
        if clear:
            await self._evaluate(page, _CLEAR_JS)
        if mode == "value":
            if not query:
                raise BrowserError("mode=value requires ref or selector.")
            changed = await self._evaluate(
                page,
                _SET_VALUE_JS.substitute(selector=json.dumps(query), text=json.dumps(text)),
            )
            if not changed:
                raise BrowserError("Could not set the field value.")
        else:
            await page.send("Input.insertText", {"text": text}, timeout_ms=self.timeout_ms)
        if submit:
            await self.press_key(page, "enter")
        state = await self._page_state(page, "typed")
        return {**state, "characters": len(text)}

    async def key(self, *, keys: str) -> dict[str, Any]:
        page, _info = await self.connect_page()
        await self.press_key(page, keys)
        return await self._page_state(page, f"pressed {keys}")

    async def scroll(
        self,
        *,
        x: int | None = None,
        y: int | None = None,
        delta_x: int | None = None,
        delta_y: int | None = None,
    ) -> dict[str, Any]:
        page, _info = await self.connect_page()
        metrics: dict[str, Any] = {}
        try:
            metrics = await page.send("Page.getLayoutMetrics")
        except Exception:  # noqa: BLE001 - fall back to a nominal viewport
            metrics = {}
        viewport = metrics.get("cssLayoutViewport") if isinstance(metrics, dict) else None
        width = viewport.get("clientWidth") if isinstance(viewport, dict) else 800
        height = viewport.get("clientHeight") if isinstance(viewport, dict) else 600
        point_x = int(round(x if x is not None else (width or 800) / 2))
        point_y = int(round(y if y is not None else (height or 600) / 2))
        scroll_x = int(round(delta_x or 0))
        scroll_y = int(round(delta_y or 0))
        if not scroll_x and not scroll_y:
            raise BrowserError("scroll needs delta_x or delta_y.")
        await page.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseWheel",
                "x": point_x,
                "y": point_y,
                "deltaX": scroll_x,
                "deltaY": scroll_y,
            },
            timeout_ms=self.timeout_ms,
        )
        return await self._page_state(page, "scrolled")

    # ── Screenshot ──────────────────────────────────────────────────────

    async def screenshot(
        self,
        *,
        format: str = "jpeg",
        quality: int | None = None,
        full_page: bool = False,
    ) -> dict[str, Any]:
        page, _info = await self.connect_page()
        fmt = "png" if format == "png" else "jpeg"
        params: dict[str, Any] = {
            "format": fmt,
            "captureBeyondViewport": full_page is True,
        }
        if fmt == "jpeg":
            params["quality"] = _clamp(int(quality if quality is not None else 75), 1, 100)
        if full_page:
            metrics = await page.send("Page.getLayoutMetrics")
            content = metrics.get("cssContentSize") if isinstance(metrics, dict) else None
            if isinstance(content, dict):
                width = content.get("width")
                height = content.get("height")
                if (
                    isinstance(width, (int, float))
                    and isinstance(height, (int, float))
                    and width > 0
                    and height > 0
                ):
                    params["clip"] = {
                        "x": 0,
                        "y": 0,
                        "width": int(-(-float(width) // 1)),
                        "height": int(-(-float(height) // 1)),
                        "scale": 1,
                    }
        result = await page.send("Page.captureScreenshot", params, timeout_ms=30_000)
        encoded = result.get("data") if isinstance(result, dict) else None
        if not isinstance(encoded, str) or not encoded:
            raise BrowserError("Screenshot returned no data.")
        payload = base64.b64decode(encoded)
        size = image_size(fmt, payload)
        return {
            "ok": True,
            "format": fmt,
            "bytes_base64": encoded,
            "size_bytes": len(payload),
            "pixel_width": size[0] if size else None,
            "pixel_height": size[1] if size else None,
            "full_page": full_page is True,
        }

    # ── Upload / download ───────────────────────────────────────────────

    async def upload(
        self,
        *,
        files: Sequence[str],
        ref: str | None = None,
        selector: str | None = None,
    ) -> dict[str, Any]:
        page, _info = await self.connect_page()
        query = ref_selector(ref) if ref else selector
        if not query:
            raise BrowserError("upload requires ref or selector.")
        paths = list(files)
        if not 1 <= len(paths) <= 10:
            raise BrowserError("files must contain 1..10 absolute paths.")
        if any(not os.path.isabs(path) for path in paths):
            raise BrowserError("files must contain 1..10 absolute paths.")
        document = await page.send("DOM.getDocument", {"depth": 1})
        root = document.get("root") if isinstance(document, dict) else None
        root_id = root.get("nodeId") if isinstance(root, dict) else None
        if not root_id:
            raise BrowserError("Could not read the DOM root.")
        query_result = await page.send("DOM.querySelector", {"nodeId": root_id, "selector": query})
        node_id = query_result.get("nodeId") if isinstance(query_result, dict) else None
        if not node_id:
            raise BrowserError(f"No element matches {query}.")
        await page.send("DOM.setFileInputFiles", {"nodeId": node_id, "files": paths})
        return await self._page_state(page, "uploaded")

    async def download(
        self,
        *,
        ref: str | None = None,
        selector: str | None = None,
        x: int | None = None,
        y: int | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        browser = await self.connect_browser()
        download_dir = await self.launcher.downloads_path()
        try:
            await browser.send(
                "Browser.setDownloadBehavior",
                {"behavior": "allow", "downloadPath": download_dir, "eventsEnabled": True},
            )
        except Exception:  # noqa: BLE001 - older builds only expose the page command
            page, _info = await self.connect_page()
            await page.send(
                "Page.setDownloadBehavior",
                {"behavior": "allow", "downloadPath": download_dir},
            )
        before = await self.launcher.last_download()
        click = await self.click(ref=ref, selector=selector, x=x, y=y)
        timeout = _clamp(
            int(timeout_ms if timeout_ms is not None else DOWNLOAD_TIMEOUT_MS),
            MIN_DOWNLOAD_TIMEOUT_MS,
            MAX_DOWNLOAD_TIMEOUT_MS,
        )
        deadline = asyncio.get_running_loop().time() + timeout / 1000
        stable_since = 0.0
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(self._download_poll_s)
            latest = await self.launcher.last_download()
            if not latest:
                continue
            changed = (
                before is None
                or latest.get("path") != before.get("path")
                or latest.get("modified_ms") != before.get("modified_ms")
            )
            if not changed:
                continue
            now = asyncio.get_running_loop().time()
            if stable_since == 0.0:
                stable_since = now
            if (now - stable_since) * 1000 > self._download_stable_ms and latest.get("size", 0) > 0:
                return {**click, "ok": True, "download": latest, "download_dir": download_dir}
        return {
            **click,
            "ok": False,
            "detail": "No completed download appeared before the timeout.",
            "download_dir": download_dir,
            "timeout_ms": timeout,
        }

    # ── Wait ────────────────────────────────────────────────────────────

    async def wait_for(
        self,
        *,
        selector: str | None = None,
        text: str | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        page, _info = await self.connect_page()
        if not selector and not text:
            raise BrowserError("wait_for requires selector or text.")
        timeout = _clamp(
            int(timeout_ms if timeout_ms is not None else WAIT_TIMEOUT_MS),
            MIN_WAIT_TIMEOUT_MS,
            MAX_WAIT_TIMEOUT_MS,
        )
        expression = (
            _WAIT_SELECTOR_JS.substitute(selector=json.dumps(selector))
            if selector
            else _WAIT_TEXT_JS.substitute(text=json.dumps(text or ""))
        )
        deadline = asyncio.get_running_loop().time() + timeout / 1000
        last_error: str | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                if await self._evaluate(page, expression) is True:
                    state = await self._page_state(page, "condition met")
                    return {**state, "ok": True}
            except Exception as exc:  # noqa: BLE001 - retry until the deadline
                last_error = str(exc) or exc.__class__.__name__
            await asyncio.sleep(self._wait_poll_s)
        payload: dict[str, Any] = {
            "ok": False,
            "detail": f"Condition not met within {timeout}ms.",
        }
        if last_error:
            payload["error"] = last_error
        return payload

    # ── Close ───────────────────────────────────────────────────────────

    async def close(self) -> None:
        if self._page is not None:
            await self._page.close()
        self._page = None
        self._page_target_id = None
        if self._browser is not None:
            await self._browser.close()
        self._browser = None
        self._browser_ws = None
        try:
            await self.launcher.close_gracefully()
        except Exception:  # noqa: BLE001 - closing is best effort
            logger.debug("graceful browser close failed", exc_info=True)

    async def close_page(self) -> None:
        if self._page is not None:
            await self._page.close()
        self._page = None
        self._page_target_id = None


async def _reap(task: asyncio.Task[Any]) -> None:
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


__all__ = [
    "BrowserController",
    "BrowserError",
    "ensure_allowed_url",
    "key_descriptor",
    "modifier_mask",
    "ref_selector",
]
