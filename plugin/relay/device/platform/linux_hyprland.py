"""Hyprland platform adapter.

Division of labour (matching ``desktop/src/tools/platform/hyprland.ts``):

  state / focus      ``hyprctl -j`` (monitors, clients, activewindow, cursorpos)
  absolute pointer   ``hyprctl dispatch movecursor`` (wlrctl fallback path)
  pointer buttons    ``wl-input`` (zwlr_virtual_pointer_v1) -- absolute move,
                     button, discrete scroll and real drag in one connection
  keyboard           ``wtype`` (zwp_virtual_keyboard_v1, XKB layouts, unicode)
  capture            ``grim`` (wlr-screencopy)

Hyprland 0.56 replaced string dispatchers with Lua expressions.  The adapter
probes ``hyprctl -j version`` once and chooses between
``hl.dsp.focus({window="address:0x..."})`` /
``hl.dsp.cursor.move({x=..., y=...})`` and the older
``focuswindow address:0x...`` / ``movecursor x y`` forms.

Known compositor quirks are surfaced as warnings, never hidden:

* hyprwm/Hyprland#6749 -- absolute virtual-pointer motion on multi-monitor
  layouts.  Coordinates stay global and the cursor is re-read to verify.
* hyprwm/Hyprland#4167 -- a button sent right after an external cursor warp can
  land at the previous position.  ``wl-input`` moves and clicks in one
  connection; the ``wlrctl`` fallback is verified and reported.
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
    sha256_bytes,
)
from .command import (
    CommandRunner,
    SubprocessRunner,
    parse_cursor_pos,
    parse_json,
    which_binary,
)

PROBE_TTL_SECONDS = 20.0
MAX_TEXT_CHARS = 2_000

KEY_ALIASES: dict[str, str] = {
    "enter": "Return",
    "return": "Return",
    "tab": "Tab",
    "esc": "Escape",
    "escape": "Escape",
    "space": "space",
    "backspace": "BackSpace",
    "delete": "Delete",
    "del": "Delete",
    "insert": "Insert",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "home": "Home",
    "end": "End",
    "pageup": "Prior",
    "pagedown": "Next",
    "printscreen": "Print",
    "menu": "Menu",
}

MODIFIER_ALIASES: dict[str, str] = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "shift": "shift",
    "alt": "alt",
    "option": "alt",
    "super": "logo",
    "meta": "logo",
    "win": "logo",
    "logo": "logo",
    "altgr": "altgr",
}

BUTTONS = frozenset({"left", "right", "middle"})

POINTER_ACTIONS = frozenset(
    {"move", "click", "double_click", "right_click", "middle_click", "scroll", "drag"}
)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _int_or(value: Any, fallback: int = 0) -> int:
    return int(round(value)) if _is_number(value) else fallback


def _pair(value: Any) -> tuple[int, int]:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return _int_or(value[0]), _int_or(value[1])
    return 0, 0


class HyprlandAdapter(PlatformAdapter):
    id = "linux-hyprland"
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
        self._probe: tuple[float, UiEnvironment, list[UiCapability]] | None = None
        self._hyprland_version: str | None = None

    # ── Discovery ────────────────────────────────────────────────────────

    def _resolve_binary(self, name: str) -> str | None:
        if name in self._binary_cache:
            return self._binary_cache[name]
        if name == "wl-input":
            explicit = (self.env.get("HERMES_RELAY_WL_INPUT") or "").strip()
            if explicit:
                resolved = self._which(explicit) or explicit
            else:
                bundled = os.path.join(self.home, ".hermes", "bin", "wl-input")
                resolved = self._which(bundled) or self._which("wl-input")
        else:
            resolved = self._which(name)
        self._binary_cache[name] = resolved
        return resolved

    async def _hyprctl(
        self,
        args: list[str],
        *,
        timeout_ms: int = 8_000,
    ):
        binary = self._resolve_binary("hyprctl")
        if not binary:
            raise UiError(
                "missing_dependency", "hyprctl is not installed or not on PATH."
            )
        return await self.runner.text(
            binary, args, timeout_ms=timeout_ms, env=self.env
        )

    async def _hyprctl_json(self, args: list[str]) -> Any:
        result = await self._hyprctl(args)
        if not result.ran:
            raise UiError(
                "no_graphical_session",
                "hyprctl could not run; the compositor session is unavailable.",
            )
        if result.exit_code != 0:
            detail = result.stderr.strip()[:400] or f"exit {result.exit_code}"
            raise UiError(
                "compositor_error", f"hyprctl {' '.join(args)} failed: {detail}"
            )
        parsed = parse_json(result.text())
        if parsed is None:
            raise UiError(
                "compositor_error",
                f"hyprctl {' '.join(args)} returned non-JSON output.",
            )
        return parsed

    async def environment(self) -> UiEnvironment:
        now = time.monotonic()
        if self._probe is not None and now - self._probe[0] < PROBE_TTL_SECONDS:
            return self._probe[1]

        warnings: list[str] = []
        hyprctl = self._resolve_binary("hyprctl")
        grim = self._resolve_binary("grim")
        wtype = self._resolve_binary("wtype")
        wl_input = self._resolve_binary("wl-input")
        wlrctl = self._resolve_binary("wlrctl")

        wayland_display = (self.env.get("WAYLAND_DISPLAY") or "").strip()
        signature = (self.env.get("HYPRLAND_INSTANCE_SIGNATURE") or "").strip()
        display_var = (self.env.get("DISPLAY") or "").strip()
        session = "wayland" if wayland_display else ("x11" if display_var else "none")

        graphical = False
        reason: str | None = None
        version: str | None = None

        if self.platform != "linux":
            reason = f"Hyprland adapter requires Linux (platform={self.platform})."
        elif not wayland_display:
            reason = (
                "WAYLAND_DISPLAY is not set; this process is not inside a "
                "Wayland session."
            )
        elif not hyprctl:
            reason = "hyprctl is not installed."
        else:
            try:
                probe = await self._hyprctl_json(["version", "-j"])
                if not isinstance(probe, Mapping):
                    raise UiError(
                        "compositor_error",
                        "hyprctl -j version returned an unexpected payload.",
                    )
                graphical = True
                raw_version = probe.get("version") or probe.get("tag")
                version = raw_version if isinstance(raw_version, str) else None
                self._hyprland_version = version
            except UiError as exc:
                reason = str(exc)
            except Exception as exc:  # noqa: BLE001 - a probe failure is a status
                reason = str(exc)

        if graphical and not signature:
            warnings.append(
                "HYPRLAND_INSTANCE_SIGNATURE is not set; using the only reachable "
                "Hyprland instance. Start the client inside the Hyprland session "
                "when several instances are possible."
            )
        if graphical and not wl_input:
            warnings.append(
                "wl-input helper not found (~/.hermes/bin/wl-input). Click/scroll "
                "fall back to wlrctl; drag is unavailable. Run "
                "`hermes-relay setup-input` on this machine."
            )
        if graphical and not grim:
            warnings.append(
                "grim is not installed; screenshots and visual verification are "
                "unavailable."
            )
        if graphical and not wtype:
            warnings.append(
                "wtype is not installed; keyboard input is unavailable."
            )

        environment = UiEnvironment(
            platform=self.platform,
            session=session,
            compositor="hyprland" if graphical else None,
            desktop=self.env.get("XDG_CURRENT_DESKTOP"),
            graphical=graphical,
            reason=reason,
            dependencies={
                "hyprctl": {
                    "available": bool(hyprctl),
                    "path": hyprctl,
                    **({"version": version} if version else {}),
                },
                "grim": {"available": bool(grim), "path": grim},
                "wtype": {"available": bool(wtype), "path": wtype},
                "wl-input": {"available": bool(wl_input), "path": wl_input},
                "wlrctl": {"available": bool(wlrctl), "path": wlrctl},
            },
            warnings=warnings,
        )
        capabilities = self._build_capabilities(environment)
        self._probe = (now, environment, capabilities)
        return environment

    def _build_capabilities(self, environment: UiEnvironment) -> list[UiCapability]:
        def has(name: str) -> bool:
            return bool(environment.dependencies.get(name, {}).get("available"))

        graphical = environment.graphical
        reason = environment.reason
        pointer_backend = (
            "wl-input"
            if has("wl-input")
            else ("wlrctl" if has("wlrctl") else None)
        )

        return [
            UiCapability(
                "screen.capture",
                graphical and has("grim"),
                "grim",
                None if (graphical and has("grim")) else (
                    environment.reason if not graphical else "grim not installed"
                ),
            ),
            UiCapability(
                "windows.list",
                graphical,
                "hyprctl",
                None if graphical else reason,
            ),
            UiCapability(
                "windows.focus",
                graphical,
                "hyprctl",
                None if graphical else reason,
            ),
            UiCapability(
                "pointer.move",
                graphical,
                "hyprctl movecursor",
                None if graphical else reason,
            ),
            UiCapability(
                "pointer.click",
                graphical and bool(pointer_backend),
                pointer_backend,
                None if (graphical and pointer_backend) else (
                    environment.reason
                    if not graphical
                    else "needs wl-input or wlrctl"
                ),
            ),
            UiCapability(
                "pointer.drag",
                graphical and has("wl-input"),
                "wl-input" if has("wl-input") else None,
                None if (graphical and has("wl-input")) else (
                    environment.reason
                    if not graphical
                    else "drag needs the wl-input helper"
                ),
            ),
            UiCapability(
                "pointer.scroll",
                graphical and bool(pointer_backend),
                pointer_backend,
                None if (graphical and pointer_backend) else (
                    environment.reason
                    if not graphical
                    else "needs wl-input or wlrctl"
                ),
            ),
            UiCapability(
                "keyboard.type",
                graphical and has("wtype"),
                "wtype",
                None if (graphical and has("wtype")) else (
                    environment.reason if not graphical else "wtype not installed"
                ),
            ),
            UiCapability(
                "keyboard.keys",
                graphical and has("wtype"),
                "wtype",
                None if (graphical and has("wtype")) else (
                    environment.reason if not graphical else "wtype not installed"
                ),
            ),
        ]

    async def capabilities(self) -> list[UiCapability]:
        await self.environment()
        return self._probe[2] if self._probe is not None else []

    # ── State ────────────────────────────────────────────────────────────

    async def displays(self) -> list[UiDisplay]:
        monitors = await self._hyprctl_json(["monitors", "-j"])
        if not isinstance(monitors, list):
            raise UiError(
                "compositor_error", "hyprctl -j monitors returned a non-list payload."
            )
        displays: list[UiDisplay] = []
        for monitor in monitors:
            if not isinstance(monitor, Mapping) or monitor.get("disabled") is True:
                continue
            scale = (
                float(monitor["scale"])
                if _is_number(monitor.get("scale")) and float(monitor["scale"]) > 0
                else 1.0
            )
            raw_width = _int_or(monitor.get("width"))
            raw_height = _int_or(monitor.get("height"))
            transform = _int_or(monitor.get("transform"))
            rotated = transform in (1, 3, 5, 7)
            logical_width = round((raw_height if rotated else raw_width) / scale)
            logical_height = round((raw_width if rotated else raw_height) / scale)
            monitor_id = monitor.get("id")
            name = monitor.get("name")
            displays.append(
                UiDisplay(
                    id=str(monitor_id if monitor_id is not None else (name or "unknown")),
                    name=str(name or f"monitor-{monitor_id or 0}"),
                    rect=UiRect(
                        _int_or(monitor.get("x")),
                        _int_or(monitor.get("y")),
                        int(logical_width),
                        int(logical_height),
                    ),
                    scale=scale,
                    primary=monitor_id == 0,
                    focused=monitor.get("focused") is True,
                    transform=transform,
                )
            )
        return displays

    async def windows(self) -> list[UiWindow]:
        clients = await self._hyprctl_json(["clients", "-j"])
        if not isinstance(clients, list):
            raise UiError(
                "compositor_error", "hyprctl -j clients returned a non-list payload."
            )
        try:
            displays = await self.displays()
        except UiError:
            displays = []

        windows: list[UiWindow] = []
        for client in clients:
            if not isinstance(client, Mapping):
                continue
            address = client.get("address")
            if not isinstance(address, str) or not address.startswith("0x"):
                continue
            at = _pair(client.get("at"))
            size = _pair(client.get("size"))
            monitor_id = client.get("monitor")
            monitor = next(
                (item for item in displays if item.id == str(monitor_id)), None
            )
            workspace = client.get("workspace")
            workspace_name: str | None = None
            if isinstance(workspace, Mapping):
                raw_name = workspace.get("name")
                if isinstance(raw_name, str) and raw_name:
                    workspace_name = raw_name
                elif workspace.get("id") is not None:
                    workspace_name = str(workspace.get("id"))
            pid_value = client.get("pid")
            windows.append(
                UiWindow(
                    id=address,
                    title=str(client.get("title") or ""),
                    app=str(
                        client.get("class")
                        or client.get("initialClass")
                        or "unknown"
                    ),
                    pid=int(pid_value) if _is_number(pid_value) else None,
                    rect=UiRect(at[0], at[1], size[0], size[1]),
                    display=monitor.name if monitor else None,
                    workspace=workspace_name,
                    focused=client.get("focusHistoryID") == 0,
                    visible=(
                        client.get("mapped") is not False
                        and client.get("hidden") is not True
                    ),
                    native={
                        "floating": "true" if client.get("floating") is True else "false",
                        "xwayland": "true" if client.get("xwayland") is True else "false",
                    },
                )
            )
        return windows

    async def _active_window(self) -> dict[str, Any] | None:
        try:
            active = await self._hyprctl_json(["activewindow", "-j"])
        except UiError:
            return None
        if not isinstance(active, Mapping) or not active.get("address"):
            return None
        return {
            "id": str(active.get("address")),
            "app": str(
                active.get("class") or active.get("initialClass") or "unknown"
            ),
            "title": str(active.get("title") or ""),
        }

    async def _cursor_position(self) -> UiPoint | None:
        binary = self._resolve_binary("hyprctl")
        if not binary:
            return None
        try:
            result = await self.runner.text(
                binary, ["cursorpos"], timeout_ms=4_000, env=self.env
            )
        except Exception:  # noqa: BLE001 - verification is best-effort
            return None
        if result.exit_code != 0:
            return None
        parsed = parse_cursor_pos(result.text())
        return UiPoint(parsed[0], parsed[1]) if parsed else None

    async def _find_window(self, window_id: str) -> UiWindow:
        normalized = str(window_id or "").strip().lower()
        if not re.fullmatch(r"0x[0-9a-f]{1,16}", normalized):
            raise UiError(
                "invalid_request",
                "Hyprland window ids are hex addresses like 0x55a1c2d3; "
                f"got {window_id}",
            )
        windows = await self.windows()
        match = next(
            (window for window in windows if window.id.lower() == normalized), None
        )
        if match is None:
            available = ", ".join(
                f"{window.app} ({window.id})" for window in windows
            ) or "none"
            raise UiError(
                "window_not_found",
                f"No window {normalized}. Open windows: {available}",
            )
        return match

    # ── Actions ──────────────────────────────────────────────────────────

    def _uses_lua_dispatchers(self) -> bool:
        match = re.search(r"(\d+)\.(\d+)", self._hyprland_version or "")
        if not match:
            return False
        major = int(match.group(1))
        minor = int(match.group(2))
        return major > 0 or minor >= 56

    async def _ensure_version_probed(self) -> None:
        if self._hyprland_version is None:
            await self.environment()

    async def focus_window(self, window_id: str) -> UiActionResult:
        started = time.monotonic()
        window = await self._find_window(window_id)
        if not window.visible:
            raise UiError(
                "window_not_visible",
                f"{window.app} ({window.id}) is on a hidden or special "
                "workspace; focus it from Hyprland before acting.",
            )
        await self._ensure_version_probed()
        if self._uses_lua_dispatchers():
            args = [
                "dispatch",
                f'hl.dsp.focus({{window="address:{window.id}"}})',
            ]
        else:
            args = ["dispatch", "focuswindow", f"address:{window.id}"]
        result = await self._hyprctl(args, timeout_ms=6_000)
        if not result.ran:
            raise UiError(
                "no_graphical_session",
                "hyprctl could not run; the compositor session is unavailable.",
            )
        if result.exit_code != 0:
            raise UiError(
                "input_failed",
                f"focuswindow failed: {result.stderr.strip() or result.exit_code}",
            )

        active = await self._active_window()
        verified = active is not None and active.get("id") == window.id
        return UiActionResult(
            ok=verified,
            backend="hyprctl",
            action="focus_window",
            performed_at=iso_now(),
            duration_ms=int((time.monotonic() - started) * 1000),
            verification=UiVerification(
                method="active_window",
                ok=verified,
                active_window=active,
                detail=(
                    f"focused {window.app}"
                    if verified
                    else f"focus did not settle on {window.id}"
                ),
            ),
        )

    async def capture(
        self, request: UiCaptureRequest | Mapping[str, Any]
    ) -> UiCapture:
        request = coerce_capture_request(request)
        grim = self._resolve_binary("grim")
        if not grim:
            raise UiError(
                "missing_dependency",
                "grim is not installed; cannot capture the screen.",
            )
        displays = await self.displays()
        if not displays:
            raise UiError(
                "no_graphical_session", "Hyprland reports no active monitors."
            )

        fmt = request.format if request.format in ("png", "jpeg") else None
        if fmt is None:
            raise UiError(
                "invalid_request", f"Unknown capture format {request.format!r}."
            )
        target = request.target or "display"
        if target not in ("display", "window", "region", "all"):
            raise UiError(
                "invalid_request",
                f"Unknown capture target {target!r}; use display, window, "
                "region, or all.",
            )

        warnings: list[str] = []
        origin = UiPoint(0, 0)
        scale = 1.0
        display_name = "all"
        logical_width = 0
        logical_height = 0
        uniform_scale: float | None = None
        args: list[str] = []

        if target in ("display", "window"):
            region: UiRect | None = None
            if target == "window":
                if not request.window_id:
                    raise UiError(
                        "invalid_request", "target=window requires window_id."
                    )
                window = await self._find_window(request.window_id)
                display = (
                    next(
                        (item for item in displays if item.name == window.display),
                        None,
                    )
                    or display_at(displays, window.rect.x, window.rect.y)
                )
                if display is None:
                    raise UiError(
                        "invalid_request",
                        "Could not determine the display for this window capture.",
                    )
                region = window.rect
                logical_width = region.width
                logical_height = region.height
                if not window.visible:
                    warnings.append(
                        "Window is not currently visible; the capture may show "
                        "another window."
                    )
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
                            "invalid_request",
                            f"Unknown display {request.display}. Displays: "
                            + ", ".join(item.name for item in displays),
                        )
                else:
                    display = next(
                        (item for item in displays if item.focused), displays[0]
                    )
                logical_width = display.rect.width
                logical_height = display.rect.height

            origin = UiPoint(display.rect.x, display.rect.y)
            scale = display.scale or 1.0
            display_name = display.name
            if region is not None:
                origin = UiPoint(region.x, region.y)
                logical_width = region.width
                logical_height = region.height
            if request.region is not None:
                warnings.append(
                    "region is ignored when target is display/window; use "
                    "target=region."
                )
            args = [
                "-g",
                f"{origin.x},{origin.y} {logical_width}x{logical_height}",
                "-t",
                fmt,
            ]
        elif target == "region":
            bounds = normalize_rect(request.region)
            if bounds is None:
                raise UiError(
                    "invalid_request",
                    "target=region requires {x,y,width,height} with positive "
                    "width and height.",
                )
            display = display_at(displays, bounds.x, bounds.y)
            if display is None:
                raise UiError(
                    "invalid_request",
                    f"Region origin {bounds.x},{bounds.y} is outside the "
                    "desktop layout.",
                )
            origin = UiPoint(bounds.x, bounds.y)
            scale = display.scale or 1.0
            display_name = display.name
            logical_width = bounds.width
            logical_height = bounds.height
            args = [
                "-g",
                f"{bounds.x},{bounds.y} {bounds.width}x{bounds.height}",
                "-t",
                fmt,
            ]
        else:
            bounds = layout_bounds(displays)
            if bounds is None:
                raise UiError(
                    "no_graphical_session",
                    "No monitors are available to capture.",
                )
            origin = UiPoint(bounds.x, bounds.y)
            scales = {display.scale for display in displays}
            if len(scales) > 1:
                warnings.append(
                    "Displays use different scales; capture a single display for "
                    "exact pixel-to-coordinate mapping."
                )
            else:
                uniform_scale = next(iter(scales))
            scale = (
                next(
                    (item.scale for item in displays if item.focused),
                    next(iter(scales)),
                )
                or 1.0
            )
            display_name = "all"
            logical_width = bounds.width
            logical_height = bounds.height
            args = ["-t", fmt]

        if fmt == "jpeg":
            quality = min(100, max(1, _int_or(request.quality, 80)))
            args.extend(["-q", str(quality)])
        args.append("-")

        result = await self.runner.buffer(
            grim, args, timeout_ms=20_000, env=self.env
        )
        if not result.ran:
            raise UiError("missing_dependency", "grim could not be executed.")
        if result.exit_code != 0 or not result.stdout:
            detail = result.stderr.strip()[:400] or f"exit {result.exit_code}"
            raise UiError("capture_failed", f"grim failed: {detail}")

        saved_path: str | None = None
        if request.save_to:
            saved_path = request.save_to
            try:
                parent = os.path.dirname(saved_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(saved_path, "wb") as handle:
                    handle.write(result.stdout)
            except OSError as exc:
                raise UiError(
                    "capture_failed", f"could not write {saved_path}: {exc}"
                ) from exc

        decoded = image_size(fmt, result.stdout)
        if decoded is not None:
            pixel_width, pixel_height = decoded
        else:
            fallback_scale = uniform_scale if target == "all" else scale
            if logical_width and logical_height and fallback_scale:
                pixel_width = round(logical_width * fallback_scale)
                pixel_height = round(logical_height * fallback_scale)
            else:
                pixel_width = pixel_height = 0
            warnings.append(
                f"Could not read {fmt.upper()} dimensions from the capture."
            )

        return UiCapture(
            format=fmt,
            bytes_base64=(
                None
                if saved_path
                else base64.b64encode(result.stdout).decode("ascii")
            ),
            saved_path=saved_path,
            size_bytes=len(result.stdout),
            pixel_width=int(pixel_width),
            pixel_height=int(pixel_height),
            origin=origin,
            scale=scale,
            display=display_name,
            target=target,
            warnings=warnings,
        )

    async def _visual_hash(self) -> str | None:
        try:
            capture = await self.capture(UiCaptureRequest(target="display"))
        except UiError:
            return None
        if not capture.bytes_base64:
            return None
        try:
            return sha256_bytes(base64.b64decode(capture.bytes_base64))
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _require_point(request: UiPointerRequest) -> tuple[int, int]:
        if request.x is None or request.y is None:
            raise UiError(
                "invalid_request",
                "Pointer actions require integer x and y coordinates.",
            )
        return int(request.x), int(request.y)

    @staticmethod
    def _button_for(request: UiPointerRequest) -> str:
        if request.action == "right_click":
            button = "right"
        elif request.action == "middle_click":
            button = "middle"
        else:
            button = request.button or "left"
        if button not in BUTTONS:
            raise UiError(
                "invalid_request",
                f"Unknown mouse button {button!r}; use left, right, or middle.",
            )
        return button

    async def _run_pointer_helper(
        self, request: UiPointerRequest, point: tuple[int, int]
    ) -> tuple[str, list[str]]:
        helper = self._resolve_binary("wl-input")
        displays = await self.displays()
        bounds = layout_bounds(displays)
        if bounds is None:
            raise UiError(
                "no_graphical_session", "No monitors are available for pointer input."
            )

        if helper:
            args = [
                "--space",
                str(bounds.x),
                str(bounds.y),
                str(bounds.width),
                str(bounds.height),
            ]
            if request.action == "move":
                args.extend(["move", str(point[0]), str(point[1])])
            elif request.action == "drag":
                if request.to_x is None or request.to_y is None:
                    raise UiError(
                        "invalid_request", "drag requires to_x and to_y."
                    )
                duration = min(
                    5_000, max(40, _int_or(request.duration_ms, 400))
                )
                args.extend(
                    [
                        "drag",
                        str(point[0]),
                        str(point[1]),
                        str(int(request.to_x)),
                        str(int(request.to_y)),
                        self._button_for(request),
                        str(duration),
                    ]
                )
            elif request.action == "scroll":
                delta_x = _int_or(request.delta_x)
                delta_y = _int_or(request.delta_y)
                if delta_x == 0 and delta_y == 0:
                    raise UiError(
                        "invalid_request",
                        "scroll needs delta_x or delta_y (wheel notches).",
                    )
                args.extend(
                    [
                        "scroll",
                        str(point[0]),
                        str(point[1]),
                        str(delta_x),
                        str(delta_y),
                    ]
                )
            else:
                count = (
                    2
                    if request.action == "double_click"
                    else min(3, max(1, _int_or(request.count, 1)))
                )
                args.extend(
                    [
                        "click",
                        str(point[0]),
                        str(point[1]),
                        self._button_for(request),
                        str(count),
                    ]
                )
            result = await self.runner.text(
                helper, args, timeout_ms=15_000, env=self.env
            )
            if not result.ran:
                raise UiError(
                    "missing_dependency", "wl-input could not be executed."
                )
            if result.exit_code != 0:
                detail = result.stderr.strip()[:400] or f"exit {result.exit_code}"
                code = (
                    "compositor_unsupported"
                    if result.exit_code == 3
                    else "input_failed"
                )
                raise UiError(code, f"wl-input failed: {detail}")
            return "wl-input", []

        # Fallback: cursor warp through the compositor + wlrctl button/axis
        # events.  Two clients means the known Hyprland warp/button races apply;
        # the cursor is verified afterwards and the fallback is reported.
        hyprctl = self._resolve_binary("hyprctl")
        if not hyprctl:
            raise UiError("missing_dependency", "hyprctl is not installed.")
        if request.action == "move":
            await self._ensure_version_probed()
            if self._uses_lua_dispatchers():
                move_args = [
                    "dispatch",
                    f"hl.dsp.cursor.move({{x={point[0]}, y={point[1]}}})",
                ]
            else:
                move_args = [
                    "dispatch",
                    "movecursor",
                    str(point[0]),
                    str(point[1]),
                ]
            warp = await self.runner.text(
                hyprctl, move_args, timeout_ms=6_000, env=self.env
            )
            if not warp.ran:
                raise UiError(
                    "no_graphical_session",
                    "hyprctl could not run; the compositor session is unavailable.",
                )
            if warp.exit_code != 0:
                raise UiError(
                    "input_failed",
                    f"movecursor failed: {warp.stderr.strip() or warp.exit_code}",
                )
            return "hyprctl", []

        if request.action == "drag":
            raise UiError(
                "capability_unavailable",
                "Drag needs the wl-input helper; wlrctl has no button down/up "
                "requests. Run `hermes-relay setup-input` on this machine.",
            )
        wlrctl = self._resolve_binary("wlrctl")
        if not wlrctl:
            raise UiError(
                "missing_dependency",
                "Pointer buttons need the wl-input helper (recommended) or "
                "wlrctl. Run `hermes-relay setup-input` on this machine.",
            )
        await self._ensure_version_probed()
        if self._uses_lua_dispatchers():
            warp_args = [
                "dispatch",
                f"hl.dsp.cursor.move({{x={point[0]}, y={point[1]}}})",
            ]
        else:
            warp_args = ["dispatch", "movecursor", str(point[0]), str(point[1])]
        warp = await self.runner.text(
            hyprctl, warp_args, timeout_ms=6_000, env=self.env
        )
        if not warp.ran:
            raise UiError(
                "no_graphical_session",
                "hyprctl could not run; the compositor session is unavailable.",
            )
        if warp.exit_code != 0:
            raise UiError(
                "input_failed",
                f"movecursor failed: {warp.stderr.strip() or warp.exit_code}",
            )
        await self._sleep(0.04)

        button = self._button_for(request)
        if request.action == "scroll":
            delta_x = _int_or(request.delta_x)
            delta_y = _int_or(request.delta_y)
            if delta_x == 0 and delta_y == 0:
                raise UiError(
                    "invalid_request",
                    "scroll needs delta_x or delta_y (wheel notches).",
                )
            commands = [["pointer", "scroll", str(delta_y), str(delta_x)]]
        else:
            count = (
                2
                if request.action == "double_click"
                else min(3, max(1, _int_or(request.count, 1)))
            )
            commands = [["pointer", "click", button] for _ in range(count)]
        for command in commands:
            result = await self.runner.text(
                wlrctl, command, timeout_ms=6_000, env=self.env
            )
            if not result.ran:
                raise UiError(
                    "missing_dependency", "wlrctl could not be executed."
                )
            if result.exit_code != 0:
                raise UiError(
                    "input_failed",
                    f"wlrctl failed: {result.stderr.strip() or result.exit_code}",
                )
        return "wlrctl", [
            (
                "Used the wlrctl fallback: cursor warp and button events are "
                "separate clients, which has known Hyprland quirks (#4167, "
                "#6749). Install the wl-input helper for atomic input."
            )
        ]

    async def _verify_pointer(
        self,
        expected: tuple[int, int],
        before: str | None,
    ) -> UiVerification:
        cursor = await self._cursor_position()
        active = await self._active_window()
        detail: list[str] = []
        ok: bool | None = None
        if cursor is not None:
            ok = (
                abs(cursor.x - expected[0]) <= 2
                and abs(cursor.y - expected[1]) <= 2
            )
            detail.append(
                f"cursor at {cursor.x},{cursor.y}"
                if ok
                else f"cursor at {cursor.x},{cursor.y}, expected "
                f"{expected[0]},{expected[1]}"
            )
        else:
            detail.append("cursor position unavailable")

        image_changed: bool | None = None
        if before is not None:
            after = await self._visual_hash()
            if after is not None:
                image_changed = after != before
                detail.append("screen changed" if image_changed else "screen unchanged")
                if ok is None:
                    ok = image_changed
        methods = [
            "cursor",
            "pixels" if before is not None else "",
            "active_window" if active is not None else "",
        ]
        return UiVerification(
            method="+".join(part for part in methods if part) or "none",
            ok=ok,
            detail="; ".join(detail) or None,
            cursor=cursor,
            active_window=active,
            image_changed=image_changed,
        )

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
        point = self._require_point(request)
        displays = await self.displays()
        bounds = layout_bounds(displays)
        if bounds is None:
            raise UiError(
                "no_graphical_session", "No monitors are available for pointer input."
            )
        if not bounds.contains(point[0], point[1]):
            raise UiError(
                "invalid_request",
                f"Point {point[0]},{point[1]} is outside the desktop layout "
                f"(x {bounds.x}..{bounds.x + bounds.width - 1}, "
                f"y {bounds.y}..{bounds.y + bounds.height - 1}). Displays: "
                + "; ".join(
                    f"{item.name} {item.rect.x},{item.rect.y} "
                    f"{item.rect.width}x{item.rect.height}"
                    for item in displays
                ),
            )

        before = await self._visual_hash() if request.verify else None
        backend, warnings = await self._run_pointer_helper(request, point)
        expected = (
            (int(request.to_x), int(request.to_y))
            if request.action == "drag"
            and request.to_x is not None
            and request.to_y is not None
            else point
        )
        verification = await self._verify_pointer(expected, before)
        if request.action == "drag":
            verification.detail = "; ".join(
                part
                for part in [
                    verification.detail,
                    f"dragged to {expected[0]},{expected[1]}",
                ]
                if part
            )
        if verification.ok is None:
            warnings.append(
                "Pointer effect could not be verified; cursor position is "
                "unavailable."
            )
        return UiActionResult(
            ok=verification.ok is True,
            backend=backend,
            action=request.action,
            performed_at=iso_now(),
            duration_ms=int((time.monotonic() - started) * 1000),
            verification=verification,
            warnings=warnings,
        )

    # ── Keyboard ─────────────────────────────────────────────────────────

    def _build_key_args(self, keys: str) -> list[str]:
        parts = [
            part.strip().lower() for part in (keys or "").split("+") if part.strip()
        ]
        if not parts:
            raise UiError("invalid_request", "keys is empty.")
        key_part = parts[-1]
        modifiers: list[str] = []
        for part in parts[:-1]:
            modifier = MODIFIER_ALIASES.get(part)
            if modifier is None:
                raise UiError(
                    "invalid_request", f"Unknown modifier {part} in {keys}."
                )
            modifiers.append(modifier)

        if re.fullmatch(r"[a-z0-9]", key_part):
            key = key_part
        elif re.fullmatch(r"f([1-9]|1[0-2])", key_part):
            key = key_part.upper()
        elif key_part in KEY_ALIASES:
            key = KEY_ALIASES[key_part]
        elif re.fullmatch(r"[a-z][a-z0-9_]*", key_part, re.IGNORECASE):
            key = key_part
        else:
            raise UiError(
                "invalid_request",
                f"Unsupported key {key_part}. Use XKB keysym names or common "
                "aliases (enter, tab, esc, arrows, f1-f12).",
            )

        args: list[str] = []
        for modifier in modifiers:
            args.extend(["-M", modifier])
        args.extend(["-k", key])
        for modifier in reversed(modifiers):
            args.extend(["-m", modifier])
        return args

    async def keyboard(
        self, request: UiKeyboardRequest | Mapping[str, Any]
    ) -> UiActionResult:
        request = coerce_keyboard_request(request)
        started = time.monotonic()
        wtype = self._resolve_binary("wtype")
        if not wtype:
            raise UiError(
                "missing_dependency",
                "wtype is not installed; cannot type on Hyprland.",
            )
        warnings: list[str] = []
        if request.window_id:
            focus = await self.focus_window(request.window_id)
            if not focus.ok:
                warnings.append(
                    focus.verification.detail
                    if focus.verification and focus.verification.detail
                    else "focus did not settle before typing"
                )
        if not request.text and not request.keys:
            raise UiError("invalid_request", "keyboard requires text or keys.")
        if request.text and len(request.text) > MAX_TEXT_CHARS:
            raise UiError(
                "invalid_request",
                f"text is limited to {MAX_TEXT_CHARS} characters per call.",
            )

        before = await self._visual_hash() if request.verify else None
        stdin: str | None = None
        if request.text:
            delay = min(200, max(0, _int_or(request.delay_ms, 10)))
            args = ["-s", "30", "-d", str(delay), "-"]
            stdin = request.text
        else:
            args = self._build_key_args(request.keys or "")

        result = await self.runner.text(
            wtype, args, timeout_ms=20_000, stdin=stdin, env=self.env
        )
        if not result.ran:
            raise UiError("missing_dependency", "wtype could not be executed.")
        if result.exit_code != 0:
            detail = result.stderr.strip()[:400] or str(result.exit_code)
            raise UiError("input_failed", f"wtype failed: {detail}")

        active = await self._active_window()
        image_changed: bool | None = None
        detail_parts: list[str] = []
        if before is not None:
            after = await self._visual_hash()
            if after is not None:
                image_changed = after != before
                detail_parts.append(
                    "screen changed" if image_changed else "screen unchanged"
                )
        methods = [
            "pixels" if before is not None else "",
            "active_window" if active is not None else "",
        ]
        verification = UiVerification(
            method="+".join(part for part in methods if part) or "none",
            ok=image_changed,
            detail="; ".join(detail_parts) or None,
            active_window=active,
            image_changed=image_changed,
        )
        if request.verify and before is None:
            warnings.append(
                "Pixel verification requested but grim is unavailable; keyboard "
                "effect is unverified."
            )
        return UiActionResult(
            ok=True,
            backend="wtype",
            action="type" if request.text else "keys",
            performed_at=iso_now(),
            duration_ms=int((time.monotonic() - started) * 1000),
            verification=verification,
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


__all__ = ["HyprlandAdapter"]
