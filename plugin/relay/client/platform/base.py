"""Platform-adapter contract for Linux desktop observation and input.

The relay's ``desktop_ui_*`` tools are platform-neutral at the model boundary.
This module defines the small internal interface every Linux adapter implements
so the tool handlers never need to know whether a session is Hyprland or X11:

    handlers/desktop_ui.py  ->  platform.select_platform_adapter()  ->  adapter

Adapters report *why* a capability is missing (``UiCapability.reason``) instead
of throwing opaque errors: the agent needs to tell the user "drag needs the
wl-input helper", not "input failed".

Coordinate contract, shared by every capture::

    global_logical = origin + pixel / scale

``origin`` is the top-left pixel's logical desktop position and ``scale`` is the
output scale of the display the image came from.  A 2560x1440@1.25 output
captured as a full display therefore reports ``pixel_width=2560``,
``scale=1.25`` and covers 2048x1152 logical coordinates.
"""

from __future__ import annotations

import hashlib
import math
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC
from typing import Any

# ── Capability identifiers (kept in lock-step with desktop/src/tools/platform/types.ts)

CAPABILITY_IDS = (
    "screen.capture",
    "windows.list",
    "windows.focus",
    "pointer.move",
    "pointer.click",
    "pointer.drag",
    "pointer.scroll",
    "keyboard.type",
    "keyboard.keys",
)

UiPlatform = str
UiSessionKind = str
UiPointerAction = str


class UiError(Exception):
    """Adapter-level failure with a stable machine-readable ``code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def to_failure(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "message": str(self),
            "adapter_error": True,
        }


@dataclass(frozen=True)
class UiPoint:
    x: int
    y: int


@dataclass(frozen=True)
class UiRect:
    x: int
    y: int
    width: int
    height: int

    def contains(self, x: int, y: int) -> bool:
        return (
            x >= self.x
            and y >= self.y
            and x < self.x + self.width
            and y < self.y + self.height
        )

    def to_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass
class UiCapability:
    id: str
    available: bool
    backend: str | None = None
    reason: str | None = None


@dataclass
class UiDisplay:
    id: str
    name: str
    rect: UiRect
    scale: float = 1.0
    primary: bool = False
    focused: bool = False
    transform: int = 0


@dataclass
class UiWindow:
    id: str
    title: str
    app: str
    rect: UiRect
    pid: int | None = None
    display: str | None = None
    workspace: str | None = None
    focused: bool = False
    visible: bool = True
    native: dict[str, str] = field(default_factory=dict)


@dataclass
class UiEnvironment:
    platform: UiPlatform
    session: UiSessionKind
    graphical: bool
    compositor: str | None = None
    desktop: str | None = None
    reason: str | None = None
    dependencies: dict[str, dict[str, Any]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class UiCaptureRequest:
    target: str = "display"  # display | window | region | all
    display: str | None = None
    window_id: str | None = None
    region: UiRect | Mapping[str, Any] | None = None
    format: str = "png"
    quality: int | None = None
    save_to: str | None = None


@dataclass
class UiCapture:
    format: str
    size_bytes: int
    pixel_width: int
    pixel_height: int
    origin: UiPoint
    scale: float
    display: str
    target: str
    bytes_base64: str | None = None
    saved_path: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class UiPointerRequest:
    action: UiPointerAction
    x: int | None = None
    y: int | None = None
    to_x: int | None = None
    to_y: int | None = None
    button: str = "left"
    count: int = 1
    delta_x: int = 0
    delta_y: int = 0
    duration_ms: int | None = None
    verify: bool = False


@dataclass
class UiKeyboardRequest:
    text: str | None = None
    keys: str | None = None
    window_id: str | None = None
    delay_ms: int | None = None
    verify: bool = False


@dataclass
class UiVerification:
    method: str
    ok: bool | None
    detail: str | None = None
    cursor: UiPoint | None = None
    active_window: dict[str, Any] | None = None
    image_changed: bool | None = None


@dataclass
class UiActionResult:
    ok: bool
    backend: str
    action: str
    performed_at: str
    duration_ms: int
    verification: UiVerification | None = None
    warnings: list[str] = field(default_factory=list)


class PlatformAdapter(ABC):
    """Every Linux graphical backend implements this surface."""

    id: str = "unsupported"
    platform: str = "linux"

    @abstractmethod
    async def environment(self) -> UiEnvironment: ...

    @abstractmethod
    async def capabilities(self) -> list[UiCapability]: ...

    @abstractmethod
    async def displays(self) -> list[UiDisplay]: ...

    @abstractmethod
    async def windows(self) -> list[UiWindow]: ...

    @abstractmethod
    async def focus_window(self, window_id: str) -> UiActionResult: ...

    @abstractmethod
    async def capture(
        self, request: UiCaptureRequest | Mapping[str, Any]
    ) -> UiCapture: ...

    @abstractmethod
    async def pointer(
        self, request: UiPointerRequest | Mapping[str, Any]
    ) -> UiActionResult: ...

    @abstractmethod
    async def type_text(self, text: str, **kwargs: Any) -> UiActionResult: ...

    @abstractmethod
    async def keys(self, keys: str, **kwargs: Any) -> UiActionResult: ...


# ── Normalisation helpers ────────────────────────────────────────────────────


def _number(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return round(value)


def normalize_rect(value: Any) -> UiRect | None:
    if isinstance(value, UiRect):
        return value if value.width > 0 and value.height > 0 else None
    if not isinstance(value, Mapping):
        return None
    x = _number(value.get("x"))
    y = _number(value.get("y"))
    width = _number(value.get("width"))
    height = _number(value.get("height"))
    if x is None or y is None or width is None or height is None:
        return None
    if width <= 0 or height <= 0:
        return None
    return UiRect(x, y, width, height)


def layout_bounds(displays: list[UiDisplay]) -> UiRect | None:
    if not displays:
        return None
    min_x = min(display.rect.x for display in displays)
    min_y = min(display.rect.y for display in displays)
    max_x = max(display.rect.x + display.rect.width for display in displays)
    max_y = max(display.rect.y + display.rect.height for display in displays)
    return UiRect(min_x, min_y, max_x - min_x, max_y - min_y)


def display_at(displays: list[UiDisplay], x: int, y: int) -> UiDisplay | None:
    for display in displays:
        if display.rect.contains(x, y):
            return display
    return None


def coerce_capture_request(value: UiCaptureRequest | Mapping[str, Any]) -> UiCaptureRequest:
    if isinstance(value, UiCaptureRequest):
        return value
    if not isinstance(value, Mapping):
        raise UiError("invalid_request", "capture requires a request object.")
    region = value.get("region")
    return UiCaptureRequest(
        target=str(value.get("target") or "display"),
        display=str(value["display"]) if value.get("display") else None,
        window_id=str(value["window_id"]) if value.get("window_id") else None,
        region=region if region is not None else None,
        format=str(value.get("format") or "png"),
        quality=_number(value.get("quality")),
        save_to=str(value["save_to"]) if value.get("save_to") else None,
    )


def coerce_pointer_request(value: UiPointerRequest | Mapping[str, Any]) -> UiPointerRequest:
    if isinstance(value, UiPointerRequest):
        return value
    if not isinstance(value, Mapping):
        raise UiError("invalid_request", "pointer requires a request object.")
    return UiPointerRequest(
        action=str(value.get("action") or ""),
        x=_number(value.get("x")),
        y=_number(value.get("y")),
        to_x=_number(value.get("to_x")),
        to_y=_number(value.get("to_y")),
        button=str(value.get("button") or "left"),
        count=_number(value.get("count")) or 1,
        delta_x=_number(value.get("delta_x")) or 0,
        delta_y=_number(value.get("delta_y")) or 0,
        duration_ms=_number(value.get("duration_ms")),
        verify=value.get("verify") is True,
    )


def coerce_keyboard_request(
    value: UiKeyboardRequest | Mapping[str, Any],
) -> UiKeyboardRequest:
    if isinstance(value, UiKeyboardRequest):
        return value
    if not isinstance(value, Mapping):
        raise UiError("invalid_request", "keyboard requires a request object.")
    return UiKeyboardRequest(
        text=str(value["text"]) if isinstance(value.get("text"), str) else None,
        keys=str(value["keys"]) if isinstance(value.get("keys"), str) else None,
        window_id=str(value["window_id"]) if value.get("window_id") else None,
        delay_ms=_number(value.get("delay_ms")),
        verify=value.get("verify") is True,
    )


# ── Wire serialisation ───────────────────────────────────────────────────────


def wire(value: Any) -> Any:
    """Convert dataclasses (recursively) into JSON-safe structures."""

    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: wire(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): wire(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [wire(item) for item in value]
    return value


def iso_now() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# ── Image header parsing (no Pillow dependency) ──────────────────────────────

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_size(payload: bytes) -> tuple[int, int] | None:
    if len(payload) < 24:
        return None
    if payload[:8] != _PNG_SIGNATURE:
        return None
    if payload[12:16] != b"IHDR":
        return None
    width = int.from_bytes(payload[16:20], "big")
    height = int.from_bytes(payload[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return width, height


def jpeg_size(payload: bytes) -> tuple[int, int] | None:
    if len(payload) < 4 or payload[0] != 0xFF or payload[1] != 0xD8:
        return None
    offset = 2
    length_total = len(payload)
    while offset + 9 < length_total:
        if payload[offset] != 0xFF:
            offset += 1
            continue
        marker = payload[offset + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        if offset + 4 > length_total:
            return None
        length = int.from_bytes(payload[offset + 2 : offset + 4], "big")
        if length < 2 or offset + 2 + length > length_total:
            return None
        is_start_of_frame = (
            0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC)
        )
        if is_start_of_frame:
            if offset + 9 > length_total:
                return None
            height = int.from_bytes(payload[offset + 5 : offset + 7], "big")
            width = int.from_bytes(payload[offset + 7 : offset + 9], "big")
            if width <= 0 or height <= 0:
                return None
            return width, height
        offset += 2 + length
    return None


def image_size(fmt: str, payload: bytes) -> tuple[int, int] | None:
    return png_size(payload) if fmt == "png" else jpeg_size(payload)


__all__ = [
    "CAPABILITY_IDS",
    "PlatformAdapter",
    "UiActionResult",
    "UiCapability",
    "UiCapture",
    "UiCaptureRequest",
    "UiDisplay",
    "UiEnvironment",
    "UiError",
    "UiKeyboardRequest",
    "UiPoint",
    "UiPointerRequest",
    "UiRect",
    "UiVerification",
    "UiWindow",
    "coerce_capture_request",
    "coerce_keyboard_request",
    "coerce_pointer_request",
    "display_at",
    "image_size",
    "iso_now",
    "jpeg_size",
    "layout_bounds",
    "normalize_rect",
    "png_size",
    "sha256_bytes",
    "wire",
]
