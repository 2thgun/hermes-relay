"""X11 platform adapter -- xdotool + ImageMagick/scrot.

This is the compatibility path for non-Hyprland Linux sessions (i3, XFCE,
GNOME on Xorg, ...).  It uses the standard XTEST-backed ``xdotool`` surface and
therefore inherits X11's limits: no awareness of Wayland surfaces, and
coordinates are pixels at the X server's scale (always 1).

Hyprland sessions should use :class:`HyprlandAdapter` even though ``DISPLAY``
may also be set there; selection prefers the Hyprland IPC probe.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import time
from collections.abc import Callable, Mapping
from typing import Any

from .base import (
    PlatformAdapter,
    UiActionResult,
    UiCapability,
    UiCapture,
    UiCaptureRequest,
    UiDisplay,
    UiEnvironment,
    UiError,
    UiKeyboardRequest,
    UiPoint,
    UiPointerRequest,
    UiRect,
    UiVerification,
    UiWindow,
    coerce_capture_request,
    coerce_keyboard_request,
    coerce_pointer_request,
    display_at,
    image_size,
    iso_now,
    layout_bounds,
    normalize_rect,
)
from .command import CommandRunner, SubprocessRunner, which_binary

BUTTONS: dict[str, int] = {"left": 1, "middle": 2, "right": 3}
POINTER_ACTIONS = frozenset(
    {"move", "click", "double_click", "right_click", "middle_click", "scroll", "drag"}
)
MAX_TEXT_CHARS = 2_000


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _int_or(value: Any, fallback: int = 0) -> int:
    if _is_number(value):
        return int(round(value))
    if isinstance(value, str):
        try:
            return int(round(float(value.strip())))
        except (TypeError, ValueError):
            return fallback
    return fallback


def _shell_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (text or "").splitlines():
        key, _, value = line.partition("=")
        if key and value:
            values[key.strip()] = value.strip()
    return values


class X11Adapter(PlatformAdapter):
    id = "linux-x11"
    platform = "linux"

    def __init__(
        self,
        runner: CommandRunner | None = None,
        env: Mapping[str, str] | None = None,
        platform: str = "linux",
        which: Callable[[str], str | None] | None = None,
        home: str | None = None,
        sleep: Callable[[float], Any] | None = None,
    ) -> None:
        self.runner = runner or SubprocessRunner()
        self.env = dict(env if env is not None else os.environ)
        self.platform = platform
        self.home = home or self.env.get("HOME") or os.path.expanduser("~")
        self._which = which or (
            lambda name: which_binary(
                name, self.env.get("PATH"), self.home, self.env
            )
        )
        self._sleep = sleep or asyncio.sleep
        self._binary_cache: dict[str, str | None] = {}

    # ── Discovery ────────────────────────────────────────────────────────

    def _binary(self, name: str) -> str | None:
        if name not in self._binary_cache:
            self._binary_cache[name] = self._which(name)
        return self._binary_cache[name]

    async def _xdotool(self, args: list[str], *, timeout_ms: int = 10_000):
        binary = self._binary("xdotool")
        if not binary:
            raise UiError("missing_dependency", "xdotool is not installed.")
        result = await self.runner.text(
            binary, args, timeout_ms=timeout_ms, env=self.env
        )
        if not result.ran:
            raise UiError(
                "no_graphical_session",
                "xdotool could not run; the X11 display is unavailable.",
            )
        return result

    async def environment(self) -> UiEnvironment:
        xdotool = self._binary("xdotool")
        imagemagick = self._binary("import")
        scrot = self._binary("scrot")
        xrandr = self._binary("xrandr")
        display = (self.env.get("DISPLAY") or "").strip()
        graphical = False
        reason: str | None = None

        if self.platform != "linux":
            reason = f"X11 adapter requires Linux (platform={self.platform})."
        elif not display:
            reason = "DISPLAY is not set; this process is not inside an X11 session."
        elif not xdotool:
            reason = "xdotool is not installed."
        else:
            try:
                probe = await self._xdotool(["getdisplaygeometry"])
                graphical = probe.exit_code == 0
                if not graphical:
                    reason = probe.stderr.strip() or "X display is not reachable."
            except UiError as exc:
                reason = str(exc)

        return UiEnvironment(
            platform=self.platform,
            session="x11" if display else "none",
            compositor="x11" if graphical else None,
            desktop=self.env.get("XDG_CURRENT_DESKTOP"),
            graphical=graphical,
            reason=reason,
            dependencies={
                "xdotool": {"available": bool(xdotool), "path": xdotool},
                "import": {"available": bool(imagemagick), "path": imagemagick},
                "scrot": {"available": bool(scrot), "path": scrot},
                "xrandr": {"available": bool(xrandr), "path": xrandr},
            },
            warnings=[],
        )

    async def capabilities(self) -> list[UiCapability]:
        environment = await self.environment()
        graphical = environment.graphical
        reason = None if graphical else environment.reason

        def has(name: str) -> bool:
            return bool(environment.dependencies.get(name, {}).get("available"))

        capture_backend = (
            "imagemagick" if has("import") else ("scrot" if has("scrot") else None)
        )
        capture_ok = graphical and bool(capture_backend)
        return [
            UiCapability(
                "screen.capture",
                capture_ok,
                capture_backend,
                None
                if capture_ok
                else (
                    environment.reason
                    if not graphical
                    else "needs imagemagick or scrot"
                ),
            ),
            UiCapability("windows.list", graphical, "xdotool", reason),
            UiCapability("windows.focus", graphical, "xdotool", reason),
            UiCapability("pointer.move", graphical, "xdotool", reason),
            UiCapability("pointer.click", graphical, "xdotool", reason),
            UiCapability("pointer.drag", graphical, "xdotool", reason),
            UiCapability("pointer.scroll", graphical, "xdotool", reason),
            UiCapability("keyboard.type", graphical, "xdotool", reason),
            UiCapability("keyboard.keys", graphical, "xdotool", reason),
        ]

    async def displays(self) -> list[UiDisplay]:
        xrandr = self._binary("xrandr")
        if xrandr:
            result = await self.runner.text(
                xrandr, ["--listmonitors"], timeout_ms=5_000, env=self.env
            )
            if result.exit_code == 0:
                displays: list[UiDisplay] = []
                pattern = re.compile(
                    r"^\s*\d+:\s+\+?\*?([^\s]+)\s+"
                    r"(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)"
                )
                for index, line in enumerate(result.text().splitlines()):
                    match = pattern.match(line)
                    if match is None:
                        continue
                    displays.append(
                        UiDisplay(
                            id=match.group(1),
                            name=match.group(1),
                            rect=UiRect(
                                int(match.group(4)),
                                int(match.group(5)),
                                int(match.group(2)),
                                int(match.group(3)),
                            ),
                            scale=1.0,
                            primary=index == 0,
                            focused=index == 0,
                        )
                    )
                if displays:
                    return displays

        geometry = await self._xdotool(["getdisplaygeometry"])
        match = re.match(r"^\s*(\d+)\s+(\d+)", geometry.text())
        if match is None:
            raise UiError(
                "no_graphical_session", "Could not read the X11 display geometry."
            )
        return [
            UiDisplay(
                id="root",
                name="X11",
                rect=UiRect(0, 0, int(match.group(1)), int(match.group(2))),
                scale=1.0,
                primary=True,
                focused=True,
            )
        ]

    async def windows(self) -> list[UiWindow]:
        search = await self._xdotool(
            ["search", "--onlyvisible", "--name", ".*"]
        )
        if search.exit_code != 0:
            return []
        ids = [
            line.strip()
            for line in search.text().splitlines()
            if re.fullmatch(r"\d+", line.strip())
        ][:80]
        active = await self._active_window_id()
        displays = []
        try:
            displays = await self.displays()
        except UiError:
            pass

        windows: list[UiWindow] = []
        for window_id in ids:
            try:
                name = await self._xdotool(["getwindowname", window_id])
            except UiError:
                name = None
            try:
                geometry = await self._xdotool(
                    ["getwindowgeometry", "--shell", window_id]
                )
            except UiError:
                continue
            if geometry.exit_code != 0:
                continue
            values = _shell_values(geometry.text())
            width = _int_or(values.get("WIDTH"))
            height = _int_or(values.get("HEIGHT"))
            if not width or not height:
                continue
            rect = UiRect(
                _int_or(values.get("X")),
                _int_or(values.get("Y")),
                width,
                height,
            )
            try:
                pid_result = await self._xdotool(["getwindowpid", window_id])
            except UiError:
                pid_result = None
            title = (name.text().strip() if name else "") or "unknown"
            display = display_at(
                displays,
                rect.x + rect.width // 2,
                rect.y + rect.height // 2,
            )
            windows.append(
                UiWindow(
                    id=window_id,
                    title=title,
                    app=title,
                    pid=(
                        int(pid_result.text().strip())
                        if pid_result
                        and re.fullmatch(r"\d+", pid_result.text().strip())
                        else None
                    ),
                    rect=rect,
                    display=display.name if display else None,
                    focused=active == window_id,
                    visible=True,
                )
            )
        return windows

    async def _active_window_id(self) -> str | None:
        try:
            result = await self._xdotool(["getactivewindow"])
        except UiError:
            return None
        value = result.text().strip()
        return value if re.fullmatch(r"\d+", value) else None

    async def _active_window(self) -> dict[str, Any] | None:
        window_id = await self._active_window_id()
        if not window_id:
            return None
        try:
            name = await self._xdotool(["getwindowname", window_id])
            title = name.text().strip()
        except UiError:
            title = ""
        return {"id": window_id, "app": title or "unknown", "title": title}

    async def _cursor(self) -> UiPoint | None:
        try:
            result = await self._xdotool(["getmouselocation", "--shell"])
        except UiError:
            return None
        if result.exit_code != 0:
            return None
        values = _shell_values(result.text())
        if "X" not in values or "Y" not in values:
            return None
        return UiPoint(_int_or(values.get("X")), _int_or(values.get("Y")))

    # ── Actions ──────────────────────────────────────────────────────────

    async def focus_window(self, window_id: str) -> UiActionResult:
        started = time.monotonic()
        normalized_id = str(window_id or "")
        if not re.fullmatch(r"\d+", normalized_id):
            raise UiError(
                "invalid_request",
                "X11 window ids are decimal integers from desktop_ui_windows.",
            )
        window_id = normalized_id
        result = await self._xdotool(["windowactivate", "--sync", window_id])
        if result.exit_code != 0:
            raise UiError(
                "input_failed",
                f"windowactivate failed: {result.stderr.strip() or result.exit_code}",
            )
        active = await self._active_window()
        verified = active is not None and active.get("id") == window_id
        return UiActionResult(
            ok=verified,
            backend="xdotool",
            action="focus_window",
            performed_at=iso_now(),
            duration_ms=int((time.monotonic() - started) * 1000),
            verification=UiVerification(
                method="active_window",
                ok=verified,
                active_window=active,
                detail=(
                    f"focused {window_id}"
                    if verified
                    else f"focus did not settle on {window_id}"
                ),
            ),
        )

    async def capture(
        self, request: UiCaptureRequest | Mapping[str, Any]
    ) -> UiCapture:
        request = coerce_capture_request(request)
        displays = await self.displays()
        target = request.target or "display"
        if target not in ("display", "window", "region", "all"):
            raise UiError(
                "invalid_request",
                f"Unknown capture target {target!r}; use display, window, "
                "region, or all.",
            )
        fmt = request.format if request.format in ("png", "jpeg") else None
        if fmt is None:
            raise UiError(
                "invalid_request", f"Unknown capture format {request.format!r}."
            )

        display_name: str
        if target == "window":
            if not request.window_id:
                raise UiError(
                    "invalid_request", "target=window requires window_id."
                )
            window = next(
                (
                    item
                    for item in await self.windows()
                    if item.id == request.window_id
                ),
                None,
            )
            if window is None:
                raise UiError(
                    "window_not_found", f"No X11 window {request.window_id}."
                )
            region = window.rect
            display_name = (
                window.display or (displays[0].name if displays else "X11")
            )
        elif target == "region":
            parsed = normalize_rect(request.region)
            if parsed is None:
                raise UiError(
                    "invalid_request",
                    "target=region requires {x,y,width,height} with positive "
                    "width and height.",
                )
            region = parsed
            display = display_at(displays, region.x, region.y)
            display_name = display.name if display else "X11"
        elif target == "all":
            bounds = layout_bounds(displays)
            if bounds is None:
                raise UiError(
                    "no_graphical_session", "No X11 displays are available."
                )
            region = bounds
            display_name = "all"
        else:
            if request.display:
                display = next(
                    (
                        item
                        for item in displays
                        if item.name == request.display
                        or item.id == request.display
                    ),
                    None,
                )
                if display is None:
                    raise UiError(
                        "invalid_request", f"Unknown display {request.display}."
                    )
            else:
                display = next(
                    (item for item in displays if item.primary), displays[0]
                )
            region = display.rect
            display_name = display.name

        imagemagick = self._binary("import")
        scrot = self._binary("scrot")
        payload: bytes | None = None
        if imagemagick:
            args = [
                "-window",
                "root",
                "-crop",
                f"{region.width}x{region.height}+{region.x}+{region.y}",
                f"{fmt}:-",
            ]
            result = await self.runner.buffer(
                imagemagick, args, timeout_ms=15_000, env=self.env
            )
            if result.exit_code == 0 and result.stdout:
                payload = result.stdout
        if payload is None and scrot:
            cache_dir = os.path.join(self.home, ".hermes", "cache")
            path = os.path.join(
                cache_dir,
                f"x11-capture-{int(time.time() * 1000)}-{os.getpid()}.{fmt}",
            )
            try:
                os.makedirs(cache_dir, exist_ok=True)
            except OSError:
                pass
            result = await self.runner.text(
                scrot,
                [
                    "-o",
                    "-a",
                    f"{region.x},{region.y},{region.width},{region.height}",
                    path,
                ],
                timeout_ms=15_000,
                env=self.env,
            )
            if result.exit_code == 0:
                try:
                    with open(path, "rb") as handle:
                        payload = handle.read()
                except OSError:
                    payload = None
                try:
                    os.unlink(path)
                except OSError:
                    pass
        if not payload:
            raise UiError(
                "capture_failed",
                "No X11 capture backend succeeded (tried import, scrot).",
            )

        saved_path: str | None = None
        if request.save_to:
            saved_path = request.save_to
            try:
                parent = os.path.dirname(saved_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(saved_path, "wb") as handle:
                    handle.write(payload)
            except OSError as exc:
                raise UiError(
                    "capture_failed", f"could not write {saved_path}: {exc}"
                ) from exc

        size = image_size(fmt, payload)
        if size is None:
            raise UiError(
                "capture_failed",
                "Capture did not contain a readable image header.",
            )
        return UiCapture(
            format=fmt,
            bytes_base64=(
                None
                if saved_path
                else base64.b64encode(payload).decode("ascii")
            ),
            saved_path=saved_path,
            size_bytes=len(payload),
            pixel_width=size[0],
            pixel_height=size[1],
            origin=UiPoint(region.x, region.y),
            scale=1.0,
            display=display_name,
            target=target,
            warnings=[],
        )

    @staticmethod
    def _require_point(request: UiPointerRequest) -> tuple[int, int]:
        if request.x is None or request.y is None:
            raise UiError(
                "invalid_request",
                "Pointer actions require integer x and y coordinates.",
            )
        return int(request.x), int(request.y)

    @staticmethod
    def _button_for(request: UiPointerRequest) -> int:
        if request.action == "right_click":
            name = "right"
        elif request.action == "middle_click":
            name = "middle"
        else:
            name = request.button or "left"
        if name not in BUTTONS:
            raise UiError(
                "invalid_request",
                f"Unknown mouse button {name!r}; use left, right, or middle.",
            )
        return BUTTONS[name]

    async def pointer(
        self, request: UiPointerRequest | Mapping[str, Any]
    ) -> UiActionResult:
        request = coerce_pointer_request(request)
        started = time.monotonic()
        if request.action not in POINTER_ACTIONS:
            raise UiError(
                "invalid_request",
                f"Unknown pointer action {request.action!r}.",
            )
        x, y = self._require_point(request)
        displays = await self.displays()
        bounds = layout_bounds(displays)
        if bounds is None:
            raise UiError(
                "no_graphical_session", "No X11 displays are available."
            )
        if not bounds.contains(x, y):
            raise UiError(
                "invalid_request",
                f"Point {x},{y} is outside the desktop layout "
                f"(x {bounds.x}..{bounds.x + bounds.width - 1}, "
                f"y {bounds.y}..{bounds.y + bounds.height - 1}).",
            )

        button = self._button_for(request)
        if request.action == "move":
            await self._xdotool(["mousemove", "--sync", str(x), str(y)])
        elif request.action == "scroll":
            delta_y = _int_or(request.delta_y)
            delta_x = _int_or(request.delta_x)
            if not delta_x and not delta_y:
                raise UiError(
                    "invalid_request",
                    "scroll needs delta_x or delta_y (wheel notches).",
                )
            await self._xdotool(["mousemove", "--sync", str(x), str(y)])
            vertical = 5 if delta_y > 0 else 4
            horizontal = 7 if delta_x > 0 else 6
            if delta_y:
                await self._xdotool(
                    ["click", "--repeat", str(abs(delta_y)), str(vertical)]
                )
            if delta_x:
                await self._xdotool(
                    ["click", "--repeat", str(abs(delta_x)), str(horizontal)]
                )
        elif request.action == "drag":
            if request.to_x is None or request.to_y is None:
                raise UiError(
                    "invalid_request", "drag requires to_x and to_y."
                )
            await self._xdotool(["mousemove", "--sync", str(x), str(y)])
            await self._xdotool(["mousedown", str(button)])
            await self._sleep(0.08)
            await self._xdotool(
                [
                    "mousemove",
                    "--sync",
                    str(int(request.to_x)),
                    str(int(request.to_y)),
                ]
            )
            await self._sleep(0.08)
            await self._xdotool(["mouseup", str(button)])
        else:
            count = (
                2
                if request.action == "double_click"
                else min(3, max(1, _int_or(request.count, 1)))
            )
            await self._xdotool(["mousemove", "--sync", str(x), str(y)])
            await self._xdotool(
                ["click", "--repeat", str(count), str(button)]
            )

        expected = (
            (int(request.to_x), int(request.to_y))
            if request.action == "drag"
            and request.to_x is not None
            and request.to_y is not None
            else (x, y)
        )
        cursor = await self._cursor()
        active = await self._active_window()
        cursor_ok: bool | None = None
        detail: str | None = None
        if cursor is not None:
            cursor_ok = (
                abs(cursor.x - expected[0]) <= 2
                and abs(cursor.y - expected[1]) <= 2
            )
            detail = (
                f"cursor at {cursor.x},{cursor.y}"
                if cursor_ok
                else f"cursor at {cursor.x},{cursor.y}, expected "
                f"{expected[0]},{expected[1]}"
            )
        return UiActionResult(
            ok=cursor_ok is True,
            backend="xdotool",
            action=request.action,
            performed_at=iso_now(),
            duration_ms=int((time.monotonic() - started) * 1000),
            verification=UiVerification(
                method="cursor+active_window",
                ok=cursor_ok,
                detail=detail,
                cursor=cursor,
                active_window=active,
            ),
        )

    async def keyboard(
        self, request: UiKeyboardRequest | Mapping[str, Any]
    ) -> UiActionResult:
        request = coerce_keyboard_request(request)
        started = time.monotonic()
        warnings: list[str] = []
        if request.window_id:
            focus = await self.focus_window(request.window_id)
            if not focus.ok:
                warnings.append(
                    focus.verification.detail
                    if focus.verification and focus.verification.detail
                    else "focus did not settle before typing"
                )
            await self._sleep(0.12)
        if not request.text and not request.keys:
            raise UiError("invalid_request", "keyboard requires text or keys.")
        if request.text and len(request.text) > MAX_TEXT_CHARS:
            raise UiError(
                "invalid_request",
                f"text is limited to {MAX_TEXT_CHARS} characters per call.",
            )
        if request.text:
            delay = min(200, max(0, _int_or(request.delay_ms, 12)))
            result = await self._xdotool(
                [
                    "type",
                    "--clearmodifiers",
                    "--delay",
                    str(delay),
                    "--",
                    request.text,
                ]
            )
            if result.exit_code != 0:
                raise UiError(
                    "input_failed",
                    f"xdotool type failed: "
                    f"{result.stderr.strip() or result.exit_code}",
                )
        if request.keys:
            result = await self._xdotool(
                ["key", "--clearmodifiers", request.keys]
            )
            if result.exit_code != 0:
                raise UiError(
                    "input_failed",
                    f"xdotool key failed: "
                    f"{result.stderr.strip() or result.exit_code}",
                )
        active = await self._active_window()
        return UiActionResult(
            ok=True,
            backend="xdotool",
            action="type" if request.text else "keys",
            performed_at=iso_now(),
            duration_ms=int((time.monotonic() - started) * 1000),
            verification=UiVerification(
                method="active_window",
                ok=None,
                active_window=active,
            ),
            warnings=warnings,
        )

    async def type_text(self, text: str, **kwargs: Any) -> UiActionResult:
        return await self.keyboard(
            UiKeyboardRequest(
                text=text,
                window_id=kwargs.get("window_id"),
                delay_ms=kwargs.get("delay_ms"),
                verify=kwargs.get("verify") is True,
            )
        )

    async def keys(self, keys: str, **kwargs: Any) -> UiActionResult:
        return await self.keyboard(
            UiKeyboardRequest(
                keys=keys,
                window_id=kwargs.get("window_id"),
                verify=kwargs.get("verify") is True,
            )
        )


__all__ = ["X11Adapter"]
