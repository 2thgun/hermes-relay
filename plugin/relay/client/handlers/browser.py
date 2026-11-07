"""``desktop_browser_*`` handlers for the Python relay client.

Ported from ``desktop/src/tools/handlers/browser.ts``.  The browser runs as a
separate process with a dedicated automation profile
(``$HERMES_HOME/browser-profile``), so the user's signed-in browser is never
touched.  The relay applies its own tool policy and computer-use grants before
forwarding a command; this headless client serves the operation and reports
what the local browser controller could verify.

Every handler returns a JSON object.  Arg validation failures use
``{"ok": false, "code": "invalid_request", "message": ...}``; controller
failures use ``code: "browser_error"``.  Successful inspect/click/type/... keep
the exact TypeScript shapes: ``ref``, ``rect``, ``title``, ``url``,
``elements``, ``download {path,size,modified_ms}``, ``download_dir``, and
screenshot ``bytes_base64``/``format``/``size_bytes``.
"""

from __future__ import annotations

import asyncio
import base64
import os
from collections.abc import Mapping
from typing import Any

from ..browser.controller import BrowserController
from ..dispatch import ToolContext

MAX_SCREENSHOT_BASE64 = 2_400_000

# Browser tools may legitimately block on a page load or a download.  The
# desktop CLI gives every desktop_browser_* call 180s (router.ts); mirror that
# per-tool budget instead of the 30s default so download(timeout_ms=120000)
# can finish.
TOOL_TIMEOUT_S = 180.0

_controller: BrowserController | None = None


def get_browser_controller() -> BrowserController:
    global _controller
    if _controller is None:
        _controller = BrowserController()
    return _controller


def set_browser_controller_for_tests(controller: BrowserController | None) -> None:
    global _controller
    _controller = controller


def reset_browser_controller_for_tests() -> None:
    set_browser_controller_for_tests(None)


def _failure(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "code": code, "message": message, **extra}


def _string(args: Mapping[str, Any], name: str) -> str | None:
    value = args.get(name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _number(args: Mapping[str, Any], name: str) -> int | None:
    value = args.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(round(value))


def _boolean(args: Mapping[str, Any], name: str, fallback: bool) -> bool:
    value = args.get(name)
    return value if isinstance(value, bool) else fallback


def _error_result(error: BaseException) -> dict[str, Any]:
    return _failure("browser_error", str(error) or error.__class__.__name__)


async def desktop_browser_status(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        status = await get_browser_controller().status()
    except Exception as exc:  # noqa: BLE001 - status must always answer
        return _error_result(exc)
    payload = {**status, "ok": True}
    if not status.get("available"):
        payload["note"] = "Browser automation is unavailable on this computer."
    return payload


async def desktop_browser_open(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    url = _string(args, "url")
    if not url:
        return _failure("invalid_request", "desktop_browser_open requires a url.")
    if not url.lower().startswith(("http://", "https://")) and url.lower() != "about:blank":
        return _failure("invalid_request", "Only http(s) URLs (or about:blank) are allowed.")
    try:
        headless = args.get("headless") if isinstance(args.get("headless"), bool) else None
        result = await get_browser_controller().open(
            url, headless=headless
        )
        return {**result, "ok": True, "profile": "dedicated automation profile"}
    except Exception as exc:  # noqa: BLE001
        return _error_result(exc)


async def desktop_browser_inspect(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        result = await get_browser_controller().inspect(
            max_elements=_number(args, "max_elements"),
            include_text=_boolean(args, "include_text", False),
        )
        return {"ok": True, **result}
    except Exception as exc:  # noqa: BLE001
        return _error_result(exc)


async def desktop_browser_click(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    ref = _string(args, "ref")
    selector = _string(args, "selector")
    x = _number(args, "x")
    y = _number(args, "y")
    if not ref and not selector and (x is None or y is None):
        return _failure("invalid_request", "Provide ref, selector, or x and y.")
    try:
        return await get_browser_controller().click(
            ref=ref,
            selector=selector,
            x=x,
            y=y,
            button=_string(args, "button") or "left",
            count=_number(args, "count") or 1,
        )
    except Exception as exc:  # noqa: BLE001
        return _error_result(exc)


async def desktop_browser_type(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    text = args.get("text") if isinstance(args.get("text"), str) else ""
    if not text:
        return _failure("invalid_request", "desktop_browser_type requires text.")
    try:
        return await get_browser_controller().type(
            text=text,
            ref=_string(args, "ref"),
            selector=_string(args, "selector"),
            clear=_boolean(args, "clear", False),
            submit=_boolean(args, "submit", False),
            mode="value" if args.get("mode") == "value" else "insert",
        )
    except Exception as exc:  # noqa: BLE001
        return _error_result(exc)


async def desktop_browser_key(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    keys = _string(args, "keys")
    if not keys:
        return _failure(
            "invalid_request",
            'desktop_browser_key requires keys (e.g. "ctrl+l" or "enter").',
        )
    try:
        return await get_browser_controller().key(keys=keys)
    except Exception as exc:  # noqa: BLE001
        return _error_result(exc)


async def desktop_browser_scroll(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    delta_y = _number(args, "delta_y")
    if delta_y is None:
        delta_y = _number(args, "scroll_y")
    try:
        return await get_browser_controller().scroll(
            x=_number(args, "x"),
            y=_number(args, "y"),
            delta_x=_number(args, "delta_x"),
            delta_y=delta_y,
        )
    except Exception as exc:  # noqa: BLE001
        return _error_result(exc)


def _save_path(args: Mapping[str, Any], ctx: ToolContext) -> str | None:
    value = _string(args, "save_to")
    if value and not os.path.isabs(value):
        return os.path.abspath(os.path.join(ctx.cwd, value))
    return value


def _write_bytes(path: str, payload: bytes) -> None:
    with open(path, "wb") as handle:
        handle.write(payload)


async def desktop_browser_screenshot(
    args: dict[str, Any], ctx: ToolContext
) -> dict[str, Any]:
    try:
        result = await get_browser_controller().screenshot(
            format="png" if args.get("format") == "png" else "jpeg",
            quality=_number(args, "quality"),
            full_page=_boolean(args, "full_page", False),
        )
        save_to = _save_path(args, ctx)
        encoded = result.get("bytes_base64")
        if save_to and isinstance(encoded, str):
            try:
                payload = base64.b64decode(encoded)
                await asyncio.to_thread(_write_bytes, save_to, payload)
            except OSError as exc:
                return _failure(
                    "save_failed", f"Could not write the screenshot to {save_to}: {exc}"
                )
            result.pop("bytes_base64", None)
            result["saved_path"] = save_to
            return result
        if isinstance(encoded, str) and len(encoded) > MAX_SCREENSHOT_BASE64:
            return _failure(
                "screenshot_too_large",
                f"The screenshot is {round(len(encoded) / 1024)} KiB encoded, over "
                "the relay response budget. Retry with full_page=false, "
                "format=jpeg, a smaller quality, or save_to.",
                pixel_width=result.get("pixel_width"),
                pixel_height=result.get("pixel_height"),
            )
        return result
    except Exception as exc:  # noqa: BLE001
        return _error_result(exc)


async def desktop_browser_upload(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    raw = args.get("files")
    files = [
        item for item in raw if isinstance(item, str) and item
    ] if isinstance(raw, list) else []
    if not files:
        return _failure(
            "invalid_request",
            "desktop_browser_upload requires files (absolute paths on the "
            "selected computer).",
        )
    try:
        return await get_browser_controller().upload(
            ref=_string(args, "ref"),
            selector=_string(args, "selector"),
            files=files,
        )
    except Exception as exc:  # noqa: BLE001
        return _error_result(exc)


async def desktop_browser_download(
    args: dict[str, Any], ctx: ToolContext
) -> dict[str, Any]:
    try:
        return await get_browser_controller().download(
            ref=_string(args, "ref"),
            selector=_string(args, "selector"),
            x=_number(args, "x"),
            y=_number(args, "y"),
            timeout_ms=_number(args, "timeout_ms"),
        )
    except Exception as exc:  # noqa: BLE001
        return _error_result(exc)


async def desktop_browser_wait(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        return await get_browser_controller().wait_for(
            selector=_string(args, "selector"),
            text=_string(args, "text"),
            timeout_ms=_number(args, "timeout_ms"),
        )
    except Exception as exc:  # noqa: BLE001
        return _error_result(exc)


async def desktop_browser_close(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        if _boolean(args, "page_only", False):
            await get_browser_controller().close_page()
            return {
                "ok": True,
                "detail": "Closed the automation page; the browser stays running.",
            }
        await get_browser_controller().close()
        return {"ok": True, "detail": "Closed the automation browser."}
    except Exception as exc:  # noqa: BLE001
        return _error_result(exc)


def handlers() -> dict[str, Any]:
    return {
        "desktop_browser_status": desktop_browser_status,
        "desktop_browser_open": desktop_browser_open,
        "desktop_browser_inspect": desktop_browser_inspect,
        "desktop_browser_click": desktop_browser_click,
        "desktop_browser_type": desktop_browser_type,
        "desktop_browser_key": desktop_browser_key,
        "desktop_browser_scroll": desktop_browser_scroll,
        "desktop_browser_screenshot": desktop_browser_screenshot,
        "desktop_browser_upload": desktop_browser_upload,
        "desktop_browser_download": desktop_browser_download,
        "desktop_browser_wait": desktop_browser_wait,
        "desktop_browser_close": desktop_browser_close,
    }


def timeouts() -> dict[str, float]:
    """Per-tool dispatcher budgets for every browser tool."""
    return {name: TOOL_TIMEOUT_S for name in handlers()}


__all__ = [
    "TOOL_TIMEOUT_S",
    "desktop_browser_click",
    "desktop_browser_close",
    "desktop_browser_download",
    "desktop_browser_inspect",
    "desktop_browser_key",
    "desktop_browser_open",
    "desktop_browser_screenshot",
    "desktop_browser_scroll",
    "desktop_browser_status",
    "desktop_browser_type",
    "desktop_browser_upload",
    "desktop_browser_wait",
    "get_browser_controller",
    "handlers",
    "reset_browser_controller_for_tests",
    "set_browser_controller_for_tests",
    "timeouts",
]
