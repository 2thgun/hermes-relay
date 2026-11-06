"""Platform-neutral desktop UI, browser, and device-directory tools.

These tools extend the existing ``desktop_*`` family without changing its wire
contract. The desktop client owns the platform adapter (Hyprland, X11, or
Windows) and the browser controller; this module only defines the model-facing
schema and forwards calls through the relay's loopback dispatch route.

Screenshots are returned as multimodal content (text metadata + image) exactly
like ``desktop_computer_screenshot``, so the agent can actually look at the
result instead of trusting a base64 blob.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any, Optional

_MAX_IMAGE_BYTES = 8 * 1024 * 1024

# Tools in this module that need no desktop target resolution.
_NO_TARGET_TOOLS: frozenset[str] = frozenset({"desktop_devices"})


def _transport():
    # Imported lazily: desktop_tool imports this module while defining its
    # registry, so a module-level import would be circular.
    from . import desktop_tool

    return desktop_tool


def _post(tool: str, payload: dict[str, Any]) -> dict[str, Any]:
    return _transport()._post(f"/desktop/{tool}", payload)


def _get(path: str) -> dict[str, Any]:
    return _transport()._get(path)


def _json_result(data: dict[str, Any]) -> str:
    if data.get("ok") is False and "result" not in data:
        return json.dumps(data)
    return json.dumps(data)


def _screenshot_result(data: dict[str, Any], summary: str) -> str | dict[str, Any]:
    """Attach a validated image from a client screenshot response."""
    if data.get("ok") is False or "error" in data:
        return json.dumps(data)
    result = data.get("result") if isinstance(data.get("result"), dict) else data
    encoded = result.get("bytes_base64") if isinstance(result, dict) else None
    if encoded is None:
        return json.dumps(data)
    if not isinstance(encoded, str) or len(encoded) > ((_MAX_IMAGE_BYTES + 2) // 3) * 4:
        return json.dumps({"error": "Screenshot exceeds the host image limit (8 MiB)"})
    try:
        image = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return json.dumps({"error": "Invalid screenshot image data"})
    if image.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif image.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    else:
        return json.dumps({"error": "Unsupported screenshot image format"})
    if len(image) > _MAX_IMAGE_BYTES:
        return json.dumps({"error": "Screenshot exceeds the host image limit (8 MiB)"})
    metadata_source = dict(result) if isinstance(result, dict) else {}
    metadata_source.pop("bytes_base64", None)
    metadata = {**data, "result": metadata_source} if isinstance(data.get("result"), dict) else metadata_source
    return {
        "_multimodal": True,
        "content": [
            {"type": "text", "text": f"{summary}\n{json.dumps(metadata)}"},
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{encoded}"},
            },
        ],
        "text_summary": summary,
    }


# ── Handlers ─────────────────────────────────────────────────────────────────


def desktop_devices() -> str:
    """Stable directory of paired computers: online, offline, and capabilities."""
    return json.dumps(_get("/desktop/devices"))


def desktop_ui_status() -> str:
    return _json_result(_post("desktop_ui_status", {}))


def desktop_ui_windows(
    focus: Optional[str] = None,
    app: Optional[str] = None,
    include_hidden: bool = False,
) -> str:
    payload: dict[str, Any] = {"include_hidden": bool(include_hidden)}
    if focus:
        payload["focus"] = focus
    if app:
        payload["app"] = app
    return _json_result(_post("desktop_ui_windows", payload))


def desktop_ui_screenshot(
    target: str = "display",
    display: Optional[str] = None,
    window_id: Optional[str] = None,
    region: Optional[dict[str, Any]] = None,
    format: str = "png",
    quality: Optional[int] = None,
    save_to: Optional[str] = None,
) -> str | dict[str, Any]:
    payload: dict[str, Any] = {"target": target, "format": format}
    for key, value in {
        "display": display,
        "window_id": window_id,
        "region": region,
        "quality": quality,
        "save_to": save_to,
    }.items():
        if value is not None:
            payload[key] = value
    data = _post("desktop_ui_screenshot", payload)
    return _screenshot_result(data, "Desktop capture; image attached for inspection.")


def desktop_ui_action(action: str, **kwargs: Any) -> str | dict[str, Any]:
    payload: dict[str, Any] = {"action": action}
    payload.update({key: value for key, value in kwargs.items() if value is not None})
    data = _post("desktop_ui_action", payload)
    if not isinstance(data.get("result"), dict):
        return json.dumps(data)
    result = data["result"]
    if kwargs.get("return_screenshot") and isinstance(result, dict):
        after = result.get("after_screenshot")
        if isinstance(after, dict):
            encoded = after.get("bytes_base64")
            if isinstance(encoded, str):
                return _screenshot_result(
                    {"ok": True, "result": after},
                    f"{action} executed; verification screenshot attached.",
                )
    return json.dumps(data)


def desktop_browser_status() -> str:
    return _json_result(_post("desktop_browser_status", {}))


def desktop_browser_open(url: str, headless: Optional[bool] = None) -> str:
    payload: dict[str, Any] = {"url": url}
    if headless is not None:
        payload["headless"] = bool(headless)
    return _json_result(_post("desktop_browser_open", payload))


def desktop_browser_inspect(max_elements: int = 60, include_text: bool = False) -> str:
    return _json_result(
        _post(
            "desktop_browser_inspect",
            {"max_elements": int(max_elements), "include_text": bool(include_text)},
        )
    )


def desktop_browser_click(
    ref: Optional[str] = None,
    selector: Optional[str] = None,
    x: Optional[int] = None,
    y: Optional[int] = None,
    button: str = "left",
    count: int = 1,
) -> str:
    payload: dict[str, Any] = {"button": button, "count": int(count)}
    for key, value in {"ref": ref, "selector": selector, "x": x, "y": y}.items():
        if value is not None:
            payload[key] = value
    return _json_result(_post("desktop_browser_click", payload))


def desktop_browser_type(
    text: str,
    ref: Optional[str] = None,
    selector: Optional[str] = None,
    clear: bool = False,
    submit: bool = False,
    mode: str = "insert",
) -> str:
    payload: dict[str, Any] = {
        "text": text,
        "clear": bool(clear),
        "submit": bool(submit),
        "mode": mode,
    }
    for key, value in {"ref": ref, "selector": selector}.items():
        if value is not None:
            payload[key] = value
    return _json_result(_post("desktop_browser_type", payload))


def desktop_browser_key(keys: str) -> str:
    return _json_result(_post("desktop_browser_key", {"keys": keys}))


def desktop_browser_scroll(
    delta_y: Optional[int] = None,
    delta_x: Optional[int] = None,
    x: Optional[int] = None,
    y: Optional[int] = None,
) -> str:
    payload: dict[str, Any] = {}
    for key, value in {"delta_y": delta_y, "delta_x": delta_x, "x": x, "y": y}.items():
        if value is not None:
            payload[key] = value
    return _json_result(_post("desktop_browser_scroll", payload))


def desktop_browser_screenshot(
    format: str = "jpeg",
    quality: int = 75,
    full_page: bool = False,
    save_to: Optional[str] = None,
) -> str | dict[str, Any]:
    payload: dict[str, Any] = {
        "format": format,
        "quality": int(quality),
        "full_page": bool(full_page),
    }
    if save_to:
        payload["save_to"] = save_to
    data = _post("desktop_browser_screenshot", payload)
    return _screenshot_result(data, "Browser screenshot; image attached for inspection.")


def desktop_browser_upload(
    files: list[str],
    ref: Optional[str] = None,
    selector: Optional[str] = None,
) -> str:
    payload: dict[str, Any] = {"files": files}
    for key, value in {"ref": ref, "selector": selector}.items():
        if value is not None:
            payload[key] = value
    return _json_result(_post("desktop_browser_upload", payload))


def desktop_browser_download(
    ref: Optional[str] = None,
    selector: Optional[str] = None,
    x: Optional[int] = None,
    y: Optional[int] = None,
    timeout_ms: int = 30_000,
) -> str:
    payload: dict[str, Any] = {"timeout_ms": int(timeout_ms)}
    for key, value in {"ref": ref, "selector": selector, "x": x, "y": y}.items():
        if value is not None:
            payload[key] = value
    return _json_result(_post("desktop_browser_download", payload))


def desktop_browser_wait(
    selector: Optional[str] = None,
    text: Optional[str] = None,
    timeout_ms: int = 10_000,
) -> str:
    payload: dict[str, Any] = {"timeout_ms": int(timeout_ms)}
    if selector:
        payload["selector"] = selector
    if text:
        payload["text"] = text
    return _json_result(_post("desktop_browser_wait", payload))


def desktop_browser_close(page_only: bool = False) -> str:
    return _json_result(_post("desktop_browser_close", {"page_only": bool(page_only)}))


# ── Schemas ──────────────────────────────────────────────────────────────────

_DEVICE_SELECTOR_NOTE = (
    "Pass device_id from desktop_devices for an exact unsupported-name match. "
    "Without a selector this call uses the device pinned by this conversation; "
    "it never falls back to another computer."
)

_SCHEMAS: dict[str, dict[str, Any]] = {
    "desktop_devices": {
        "name": "desktop_devices",
        "description": (
            "List paired computers with stable device_id, readable name, platform, "
            "online/offline state, and capability details (adapter, screen/input/browser "
            "support and why something is unavailable). Call this before multi-device "
            "work. Offline devices are listed for diagnosis only; a call to one fails "
            "instead of switching to another machine. " + _DEVICE_SELECTOR_NOTE
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    "desktop_ui_status": {
        "name": "desktop_ui_status",
        "description": (
            "Report this computer's desktop-control environment: OS session type, "
            "compositor, graphical-session availability, displays, and per-capability "
            "status with reasons. Use it to choose tools and to explain platform limits."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    "desktop_ui_windows": {
        "name": "desktop_ui_windows",
        "description": (
            "List visible windows with stable ids, titles, app/class, bounds, workspace, "
            "and focus state. Set focus=<id> to focus one and verify the result. Prefer "
            "this over screenshot-squinting when the app exposes window state."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "focus": {"type": "string", "description": "Window id from a previous listing; focuses and verifies it."},
                "app": {"type": "string", "description": "Case-insensitive filter over app and title."},
                "include_hidden": {"type": "boolean", "default": False},
            },
        },
    },
    "desktop_ui_screenshot": {
        "name": "desktop_ui_screenshot",
        "description": (
            "Capture the screen, a window, or a region. Returns the image plus the exact "
            "coordinate contract: global = origin + pixel / scale. Require an observe grant. "
            "Prefer target=window or region for precise clicking; a full 4K PNG may exceed "
            "the response budget (use jpeg or save_to)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "enum": ["display", "window", "region", "all"], "default": "display"},
                "display": {"type": "string", "description": "Display name from desktop_ui_status."},
                "window_id": {"type": "string", "description": "Window id for target=window."},
                "region": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                    },
                    "required": ["x", "y", "width", "height"],
                },
                "format": {"type": "string", "enum": ["png", "jpeg"], "default": "png"},
                "quality": {"type": "integer", "minimum": 1, "maximum": 100},
                "save_to": {"type": "string", "description": "Absolute path on the selected computer."},
            },
        },
    },
    "desktop_ui_action": {
        "name": "desktop_ui_action",
        "description": (
            "Perform one pointer or keyboard action on the selected computer and return "
            "verification (cursor position, active window, optional pixel change). "
            "Requires an assist/control grant. Coordinates are global logical desktop "
            "coordinates. Actions: move, click, double_click, right_click, middle_click, "
            "scroll, drag, type, key, wait. Verify the effect before claiming success."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["move", "click", "double_click", "right_click", "middle_click", "scroll", "drag", "type", "key", "wait"],
                },
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "to_x": {"type": "integer", "description": "drag destination."},
                "to_y": {"type": "integer"},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                "count": {"type": "integer", "minimum": 1, "maximum": 3},
                "delta_x": {"type": "integer", "description": "scroll notches; positive = right."},
                "delta_y": {"type": "integer", "description": "scroll notches; positive = down."},
                "duration_ms": {"type": "integer", "description": "drag duration (40-5000)."},
                "text": {"type": "string", "description": "Literal text for type."},
                "keys": {"type": "string", "description": "Key combo for key, e.g. ctrl+shift+t or enter."},
                "window_id": {"type": "string", "description": "Focus this window first (type/key)."},
                "delay_ms": {"type": "integer"},
                "verify": {"type": "boolean", "default": True, "description": "Capture before/after and compare pixels."},
                "return_screenshot": {"type": "boolean", "default": False},
                "display": {"type": "string"},
            },
            "required": ["action"],
        },
    },
    "desktop_browser_status": {
        "name": "desktop_browser_status",
        "description": (
            "Report browser automation availability: binary, dedicated automation profile, "
            "running state, DevTools endpoint, and pages. Read-only."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    "desktop_browser_open": {
        "name": "desktop_browser_open",
        "description": (
            "Launch (if needed) the dedicated automation browser and navigate to an http(s) "
            "URL. Never attaches to the user's signed-in profile. Requires an assist/control grant."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "headless": {"type": "boolean", "description": "Only effective on first launch."},
            },
            "required": ["url"],
        },
    },
    "desktop_browser_inspect": {
        "name": "desktop_browser_inspect",
        "description": (
            "Structured page inspection: URL, title, visible interactive elements with "
            "stable refs, role/name/value/checked/disabled, and bounds. Refs expire after "
            "navigation or a new inspection. Use include_text for a bounded text snapshot."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "max_elements": {"type": "integer", "default": 60, "maximum": 200},
                "include_text": {"type": "boolean", "default": False},
            },
        },
    },
    "desktop_browser_click": {
        "name": "desktop_browser_click",
        "description": (
            "Click an element by inspect ref, CSS selector, or viewport coordinates. "
            "Dispatches real mouse events; scrolls the element into view first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "selector": {"type": "string"},
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                "count": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1},
            },
        },
    },
    "desktop_browser_type": {
        "name": "desktop_browser_type",
        "description": (
            "Focus an element (ref/selector) and type text. clear=true selects existing "
            "content first; submit=true presses Enter after typing. mode=value uses the "
            "native value setter plus input/change events for stubborn controlled inputs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "maxLength": 4000},
                "ref": {"type": "string"},
                "selector": {"type": "string"},
                "clear": {"type": "boolean", "default": False},
                "submit": {"type": "boolean", "default": False},
                "mode": {"type": "string", "enum": ["insert", "value"], "default": "insert"},
            },
            "required": ["text"],
        },
    },
    "desktop_browser_key": {
        "name": "desktop_browser_key",
        "description": "Press a key combo in the page, e.g. ctrl+l, shift+tab, enter, pagedown, f5.",
        "parameters": {
            "type": "object",
            "properties": {"keys": {"type": "string"}},
            "required": ["keys"],
        },
    },
    "desktop_browser_scroll": {
        "name": "desktop_browser_scroll",
        "description": "Scroll the page with a real wheel event at the viewport centre or x,y.",
        "parameters": {
            "type": "object",
            "properties": {
                "delta_y": {"type": "integer", "description": "Positive scrolls down."},
                "delta_x": {"type": "integer"},
                "x": {"type": "integer"},
                "y": {"type": "integer"},
            },
        },
    },
    "desktop_browser_screenshot": {
        "name": "desktop_browser_screenshot",
        "description": "Capture the page (viewport or full page) as an attached image with pixel dimensions.",
        "parameters": {
            "type": "object",
            "properties": {
                "format": {"type": "string", "enum": ["png", "jpeg"], "default": "jpeg"},
                "quality": {"type": "integer", "default": 75},
                "full_page": {"type": "boolean", "default": False},
                "save_to": {"type": "string"},
            },
        },
    },
    "desktop_browser_upload": {
        "name": "desktop_browser_upload",
        "description": (
            "Set files on a file input. Paths are absolute on the selected computer. "
            "The browser's dedicated download directory receives downloads."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "files": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 10},
                "ref": {"type": "string"},
                "selector": {"type": "string"},
            },
            "required": ["files"],
        },
    },
    "desktop_browser_download": {
        "name": "desktop_browser_download",
        "description": (
            "Click a download link/button, wait for the file to settle in the automation "
            "download directory, and return its path and size."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "selector": {"type": "string"},
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "timeout_ms": {"type": "integer", "default": 30000},
            },
        },
    },
    "desktop_browser_wait": {
        "name": "desktop_browser_wait",
        "description": "Wait until a selector exists or the page text contains a string.",
        "parameters": {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
                "text": {"type": "string"},
                "timeout_ms": {"type": "integer", "default": 10000},
            },
        },
    },
    "desktop_browser_close": {
        "name": "desktop_browser_close",
        "description": "Close the automation page (page_only=true) or the whole automation browser.",
        "parameters": {
            "type": "object",
            "properties": {"page_only": {"type": "boolean", "default": False}},
        },
    },
}

_HANDLERS: dict[str, Any] = {
    "desktop_devices": lambda args, **kw: desktop_devices(),
    "desktop_ui_status": lambda args, **kw: desktop_ui_status(),
    "desktop_ui_windows": lambda args, **kw: desktop_ui_windows(**args),
    "desktop_ui_screenshot": lambda args, **kw: desktop_ui_screenshot(**args),
    "desktop_ui_action": lambda args, **kw: desktop_ui_action(**args),
    "desktop_browser_status": lambda args, **kw: desktop_browser_status(),
    "desktop_browser_open": lambda args, **kw: desktop_browser_open(**args),
    "desktop_browser_inspect": lambda args, **kw: desktop_browser_inspect(**args),
    "desktop_browser_click": lambda args, **kw: desktop_browser_click(**args),
    "desktop_browser_type": lambda args, **kw: desktop_browser_type(**args),
    "desktop_browser_key": lambda args, **kw: desktop_browser_key(**args),
    "desktop_browser_scroll": lambda args, **kw: desktop_browser_scroll(**args),
    "desktop_browser_screenshot": lambda args, **kw: desktop_browser_screenshot(**args),
    "desktop_browser_upload": lambda args, **kw: desktop_browser_upload(**args),
    "desktop_browser_download": lambda args, **kw: desktop_browser_download(**args),
    "desktop_browser_wait": lambda args, **kw: desktop_browser_wait(**args),
    "desktop_browser_close": lambda args, **kw: desktop_browser_close(**args),
}
