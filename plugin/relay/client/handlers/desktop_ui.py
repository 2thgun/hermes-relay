"""GUI tool handlers for the Python relay client.

Implements the platform-neutral ``desktop_ui_*`` family from
``desktop/src/tools/handlers/desktopUi.ts`` plus the legacy
``desktop_screenshot`` shape:

* ``desktop_ui_status``      environment, displays, and per-capability reasons
* ``desktop_ui_windows``     list/filter windows, optionally focus one
* ``desktop_ui_screenshot``  display/window/region/all capture + coordinate contract
* ``desktop_ui_action``      pointer/keyboard/wait with verification
* ``desktop_screenshot``     legacy {format, bytes_base64|saved_path, size_bytes}

The relay applies its own tool policy and computer-use grants before forwarding
a command; this headless client only serves the operation and reports exactly
what the local adapter could verify.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from typing import Any

from ..dispatch import ToolContext, ToolError
from ..platform import select_platform_adapter
from ..platform.base import (
    UiCaptureRequest,
    UiError,
    UiKeyboardRequest,
    UiPointerRequest,
    iso_now,
    wire,
)

MAX_SCREENSHOT_BASE64 = 2_400_000
MAX_WINDOWS = 100
MAX_TEXT_CHARS = 2_000
POINTER_ACTIONS = frozenset(
    {"move", "click", "double_click", "right_click", "middle_click", "scroll", "drag"}
)
ACTION_NAMES = (
    "move",
    "click",
    "double_click",
    "right_click",
    "middle_click",
    "scroll",
    "drag",
    "type",
    "key",
    "keys",
    "wait",
)


def _failure(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "code": code, "message": message, **extra}


def _adapter_error(error: BaseException) -> dict[str, Any]:
    if isinstance(error, UiError):
        return error.to_failure()
    return _failure("adapter_failed", str(error) or error.__class__.__name__)


def _string(args: Mapping[str, Any], name: str) -> str | None:
    value = args.get(name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _integer(args: Mapping[str, Any], name: str) -> int | None:
    value = args.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(round(value))


def _boolean(args: Mapping[str, Any], name: str, fallback: bool) -> bool:
    value = args.get(name)
    return value if isinstance(value, bool) else fallback


def _save_path(args: Mapping[str, Any], ctx: ToolContext) -> str | None:
    value = _string(args, "save_to")
    if value and not os.path.isabs(value):
        return os.path.abspath(os.path.join(ctx.cwd, value))
    return value


async def _wait(ctx: ToolContext, duration_ms: int) -> None:
    seconds = min(10_000, max(0, duration_ms)) / 1000.0
    if seconds <= 0:
        ctx.check_cancelled()
        return
    try:
        await asyncio.wait_for(ctx.cancel.wait(), timeout=seconds)
    except TimeoutError:
        return
    raise ToolError("aborted")


# ── desktop_ui_status ───────────────────────────────────────────────────────


async def desktop_ui_status(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        selection = await select_platform_adapter()
        environment = await selection.adapter.environment()
        warnings = list(environment.warnings)
        displays: list[Any] = []
        if environment.graphical:
            try:
                displays = await selection.adapter.displays()
            except Exception as exc:  # noqa: BLE001 - status must still answer
                warnings.append(f"display enumeration failed: {exc}")
        capabilities = await selection.adapter.capabilities()
        payload: dict[str, Any] = {
            "ok": True,
            "adapter": selection.adapter.id,
            "selection_reason": selection.reason,
            "environment": environment,
            "capabilities": capabilities,
            "displays": displays,
        }
        if warnings:
            payload["warnings"] = warnings
        return wire(payload)
    except Exception as exc:  # noqa: BLE001 - report, never crash a status probe
        return _adapter_error(exc)


# ── desktop_ui_windows ──────────────────────────────────────────────────────


async def desktop_ui_windows(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        selection = await select_platform_adapter()
        focus = _string(args, "focus")
        if focus:
            result = await selection.adapter.focus_window(focus)
            payload: dict[str, Any] = {
                "adapter": selection.adapter.id,
                **wire(result),
            }
            if not result.ok:
                payload["code"] = "focus_not_verified"
            return payload

        environment = await selection.adapter.environment()
        if not environment.graphical:
            return _failure(
                "no_graphical_session",
                environment.reason or "No logged-in graphical session.",
            )
        app_filter = (_string(args, "app") or "").lower()
        include_hidden = _boolean(args, "include_hidden", False)
        windows = [
            window
            for window in await selection.adapter.windows()
            if (include_hidden or window.visible)
            and (
                not app_filter
                or app_filter in f"{window.app} {window.title}".lower()
            )
        ]
        windows.sort(key=lambda window: not window.focused)
        total = len(windows)
        visible = windows[:MAX_WINDOWS]
        return {
            "ok": True,
            "adapter": selection.adapter.id,
            "count": len(visible),
            "windows": wire(visible),
            "truncated": total > MAX_WINDOWS,
        }
    except Exception as exc:  # noqa: BLE001
        return _adapter_error(exc)


# ── desktop_ui_screenshot ───────────────────────────────────────────────────


async def desktop_ui_screenshot(
    args: dict[str, Any], ctx: ToolContext
) -> dict[str, Any]:
    try:
        selection = await select_platform_adapter()
        target = _string(args, "target") or "display"
        if target not in ("display", "window", "region", "all"):
            return _failure(
                "invalid_request",
                f"Unknown target {target!r}; use display, window, region, or all.",
            )
        window_id = _string(args, "window_id") or _string(args, "window")
        if target == "window" and not window_id:
            return _failure(
                "invalid_request", "target=window requires window_id."
            )
        region = args.get("region")
        if region is not None and not isinstance(region, Mapping):
            return _failure(
                "invalid_request",
                "region must be an object with x, y, width, and height.",
            )
        fmt = "jpeg" if args.get("format") == "jpeg" else "png"
        request = UiCaptureRequest(
            target=target,
            display=_string(args, "display"),
            window_id=window_id,
            region=region,
            format=fmt,
            quality=_integer(args, "quality"),
            save_to=_save_path(args, ctx),
        )
        capture = await selection.adapter.capture(request)
        encoded = capture.bytes_base64
        if (
            not capture.saved_path
            and encoded is not None
            and len(encoded) > MAX_SCREENSHOT_BASE64
        ):
            return _failure(
                "screenshot_too_large",
                f"The capture is {round(len(encoded) / 1024)} KiB encoded, over "
                "the relay response budget. Retry with target=window, "
                "target=region, format=jpeg, or save_to and read the file.",
                pixel_width=capture.pixel_width,
                pixel_height=capture.pixel_height,
            )
        payload: dict[str, Any] = {
            "ok": True,
            "adapter": selection.adapter.id,
            "mode": "observe",
        }
        payload.update(wire(capture))
        return payload
    except Exception as exc:  # noqa: BLE001
        return _adapter_error(exc)


# ── desktop_ui_action ───────────────────────────────────────────────────────


async def _screenshot_after(args: Mapping[str, Any], ctx: ToolContext) -> Any:
    return await desktop_ui_screenshot(
        {
            "display": args.get("display"),
            "format": "jpeg",
            "quality": 70,
            "target": "display",
        },
        ctx,
    )


async def desktop_ui_action(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        selection = await select_platform_adapter()
        action = (_string(args, "action") or "").lower()
        if not action:
            return _failure(
                "invalid_request", "desktop_ui_action requires an action name."
            )

        if action == "wait":
            duration = _integer(args, "duration_ms")
            await _wait(ctx, duration if duration is not None else 500)
            return {
                "ok": True,
                "adapter": selection.adapter.id,
                "action": "wait",
                "performed_at": iso_now(),
            }

        return_screenshot = _boolean(args, "return_screenshot", False)
        if action in ("type", "key", "keys"):
            text = _string(args, "text")
            keys = _string(args, "keys") or _string(args, "key")
            if not text and not keys:
                return _failure(
                    "invalid_request", "type/key requires text or keys."
                )
            request = UiKeyboardRequest(
                text=text,
                keys=keys,
                window_id=_string(args, "window_id"),
                delay_ms=_integer(args, "delay_ms"),
                verify=_boolean(args, "verify", False),
            )
            result = (
                await selection.adapter.type_text(
                    text,
                    window_id=request.window_id,
                    delay_ms=request.delay_ms,
                    verify=request.verify,
                )
                if text is not None
                else await selection.adapter.keys(
                    keys or "",
                    window_id=request.window_id,
                    verify=request.verify,
                )
            )
            response: dict[str, Any] = {
                "adapter": selection.adapter.id,
                **wire(result),
            }
            if return_screenshot:
                response["after_screenshot"] = await _screenshot_after(args, ctx)
            return response

        pointer_action = "click" if action == "left_click" else action
        if pointer_action not in POINTER_ACTIONS:
            return _failure(
                "invalid_request",
                f"Unknown action {action}. Use one of: "
                + ", ".join(ACTION_NAMES)
                + ".",
            )
        button = (
            "right"
            if action == "right_click"
            else (
                "middle"
                if action == "middle_click"
                else (_string(args, "button") or "left")
            )
        )
        x = _integer(args, "x")
        y = _integer(args, "y")
        if pointer_action not in ("scroll",) and (x is None or y is None):
            return _failure(
                "invalid_request",
                f"{action} requires integer x and y coordinates.",
            )
        if pointer_action == "scroll" and (x is None or y is None):
            return _failure(
                "invalid_request", "scroll requires integer x and y coordinates."
            )
        delta_y = _integer(args, "delta_y")
        if delta_y is None:
            delta_y = _integer(args, "scroll_y")
        request = UiPointerRequest(
            action=pointer_action,
            x=x,
            y=y,
            to_x=_integer(args, "to_x"),
            to_y=_integer(args, "to_y"),
            button=button,
            count=_integer(args, "count") or 1,
            delta_x=_integer(args, "delta_x") or 0,
            delta_y=delta_y or 0,
            duration_ms=_integer(args, "duration_ms"),
            verify=_boolean(args, "verify", True),
        )
        result = await selection.adapter.pointer(request)
        response = {"adapter": selection.adapter.id, **wire(result)}
        if return_screenshot:
            response["after_screenshot"] = await _screenshot_after(args, ctx)
        return response
    except Exception as exc:  # noqa: BLE001
        return _adapter_error(exc)


# ── desktop_screenshot (legacy shape) ───────────────────────────────────────


async def desktop_screenshot(
    args: dict[str, Any], ctx: ToolContext
) -> dict[str, Any]:
    try:
        selection = await select_platform_adapter()
        display = _string(args, "display")
        target = "display"
        if display:
            lowered = display.lower()
            if lowered in ("all", "virtual", "full"):
                target = "all"
                display = None
            elif lowered in ("primary", "main", "0"):
                display = None
        capture = await selection.adapter.capture(
            UiCaptureRequest(
                target=target,
                display=display,
                format="png",
                save_to=_save_path(args, ctx),
            )
        )
        if capture.saved_path:
            return {
                "format": "png",
                "saved_path": capture.saved_path,
                "size_bytes": capture.size_bytes,
            }
        return {
            "format": "png",
            "bytes_base64": capture.bytes_base64,
            "size_bytes": capture.size_bytes,
        }
    except Exception as exc:  # noqa: BLE001
        return _adapter_error(exc)


def handlers() -> dict[str, Any]:
    return {
        "desktop_ui_status": desktop_ui_status,
        "desktop_ui_windows": desktop_ui_windows,
        "desktop_ui_screenshot": desktop_ui_screenshot,
        "desktop_ui_action": desktop_ui_action,
        "desktop_screenshot": desktop_screenshot,
    }


__all__ = [
    "desktop_screenshot",
    "desktop_ui_action",
    "desktop_ui_screenshot",
    "desktop_ui_status",
    "desktop_ui_windows",
    "handlers",
]
