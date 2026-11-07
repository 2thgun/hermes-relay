"""Unit tests for the phase-2 Linux desktop adapter.

Every compositor interaction goes through ``FakeRunner``: no Hyprland/X11
session is required, so the suite runs inside the LXC.  The assertions cover
the exact argv each adapter would emit, the 0.54 vs 0.56 Hyprland dispatch
split, verification failures, missing dependency reasons, drag capability
gating, screenshot metadata, and multi-display bounds.

A live-compositor smoke test is intentionally **not** part of the default
suite.  On a machine with a real Hyprland session you can run the adapter
manually with::

    WAYLAND_DISPLAY=wayland-1 HYPRLAND_INSTANCE_SIGNATURE=... \
        python -m plugin.relay.client run --url ws://127.0.0.1:8767

or point an opt-in sway fixture at ``HyprlandAdapter(runner=SubprocessRunner())``
with a ``hyprctl`` shim; set ``HERMES_RELAY_TEST_HEADLESS_SWAY=1`` if such a
fixture is added later.  The default suite never touches a live display.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

from plugin.relay.client.dispatch import ClientState, ToolContext, ToolRegistry
from plugin.relay.client.handlers import build_registry
from plugin.relay.client.handlers import desktop_ui as desktop_ui_handler
from plugin.relay.client.platform import (
    AdapterSelection,
    UnsupportedAdapter,
    reset_platform_adapter_for_tests,
    select_platform_adapter,
)
from plugin.relay.client.platform.base import (
    UiCaptureRequest,
    UiError,
    UiPointerRequest,
    UiRect,
)
from plugin.relay.client.platform.command import (
    CommandResult,
    CommandRunner,
    SubprocessRunner,
    which_binary,
)
from plugin.relay.client.platform.linux_hyprland import HyprlandAdapter
from plugin.relay.client.platform.linux_x11 import X11Adapter
from plugin.relay.client.runtime import build_live_status_payload

HYPR_ENV = {
    "WAYLAND_DISPLAY": "wayland-1",
    "HYPRLAND_INSTANCE_SIGNATURE": "abc123",
    "XDG_RUNTIME_DIR": "/run/user/1000",
    "PATH": "/usr/bin",
    "HOME": "/home/tester",
}

MONITORS = [
    {
        "id": 0,
        "name": "DP-1",
        "width": 2560,
        "height": 1440,
        "x": 0,
        "y": 0,
        "scale": 1.25,
        "transform": 0,
        "focused": True,
    },
    {
        "id": 1,
        "name": "HDMI-A-1",
        "width": 1920,
        "height": 1080,
        "x": 2048,
        "y": 0,
        "scale": 1,
        "transform": 0,
        "focused": False,
    },
]

CLIENTS = [
    {
        "address": "0x55aa11",
        "mapped": True,
        "hidden": False,
        "at": [100, 120],
        "size": [800, 600],
        "workspace": {"id": 1, "name": "1"},
        "monitor": 0,
        "class": "foot",
        "title": "terminal",
        "pid": 4242,
        "focusHistoryID": 0,
        "floating": False,
        "xwayland": False,
    },
    {
        "address": "0x55bb22",
        "mapped": True,
        "hidden": False,
        "at": [2100, 40],
        "size": [600, 400],
        "workspace": {"id": 2, "name": "2"},
        "monitor": 1,
        "class": "firefox",
        "title": "docs",
        "pid": 5151,
        "focusHistoryID": -1,
    },
]


def png_header(width: int = 2560, height: int = 1440) -> bytes:
    payload = bytearray(33)
    payload[0:8] = b"\x89PNG\r\n\x1a\n"
    payload[8:12] = (13).to_bytes(4, "big")
    payload[12:16] = b"IHDR"
    payload[16:20] = width.to_bytes(4, "big")
    payload[20:24] = height.to_bytes(4, "big")
    return bytes(payload)


def jpeg_header(width: int, height: int) -> bytes:
    payload = bytearray(b"\xff\xd8")
    payload.extend(b"\xff\xc0")
    payload.extend((11).to_bytes(2, "big"))
    payload.append(8)
    payload.extend(height.to_bytes(2, "big"))
    payload.extend(width.to_bytes(2, "big"))
    payload.append(3)
    payload.extend(b"\x00" * 9)
    return bytes(payload)


class Call:
    def __init__(self, cmd: str, args: list[str], stdin: str | bytes | None) -> None:
        self.cmd = cmd
        self.args = args
        self.stdin = stdin

    @property
    def name(self) -> str:
        return self.cmd.rsplit("/", 1)[-1]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Call({self.name!r}, {self.args!r})"


class FakeRunner(CommandRunner):
    def __init__(self, responder: Callable[[Call], CommandResult]) -> None:
        self.calls: list[Call] = []
        self.responder = responder

    async def run(
        self,
        cmd: str,
        args: Any,
        *,
        timeout_ms: int = 15_000,
        stdin: str | bytes | None = None,
        env: Any = None,
        max_bytes: int = 0,
        signal_event: asyncio.Event | None = None,
    ) -> CommandResult:
        call = Call(cmd, list(args), stdin)
        self.calls.append(call)
        return self.responder(call)

    def calls_for(self, name: str) -> list[Call]:
        return [call for call in self.calls if call.name == name]

    def find(self, name: str, predicate: Callable[[Call], bool] | None = None) -> Call | None:
        for call in self.calls_for(name):
            if predicate is None or predicate(call):
                return call
        return None


def ok(stdout: Any = "", stderr: str = "", exit_code: int = 0) -> CommandResult:
    if not isinstance(stdout, (bytes, bytearray)):
        stdout = str(stdout).encode("utf-8")
    return CommandResult(True, exit_code, bytes(stdout), stderr)


def hyprland_responder(
    *,
    version: str = "0.54.3",
    active: str | None = "0x55aa11",
    cursor: str = "300, 200",
    grim_width: int = 2560,
    grim_height: int = 1440,
    dispatch_exit: int = 0,
) -> Callable[[Call], CommandResult]:
    def respond(call: Call) -> CommandResult:
        if call.name == "hyprctl":
            if call.args[0] == "version" and "-j" in call.args:
                return ok(json.dumps({"version": version, "tag": f"v{version}"}))
            if call.args[0] == "monitors" and "-j" in call.args:
                return ok(json.dumps(MONITORS))
            if call.args[0] == "clients" and "-j" in call.args:
                return ok(json.dumps(CLIENTS))
            if call.args[0] == "activewindow" and "-j" in call.args:
                payload = (
                    {"address": active, "class": "foot", "title": "terminal"}
                    if active
                    else {}
                )
                return ok(json.dumps(payload))
            if call.args[:1] == ["cursorpos"]:
                return ok(cursor + "\n")
            if call.args[:1] == ["dispatch"]:
                return CommandResult(
                    True,
                    dispatch_exit,
                    b"ok" if dispatch_exit == 0 else b"",
                    "" if dispatch_exit == 0 else "lua parse error",
                )
        if call.name == "grim":
            return ok(png_header(grim_width, grim_height))
        if call.name in {"wl-input", "wtype", "wlrctl"}:
            return ok()
        return CommandResult(False, 127, b"", "not found")

    return respond


def make_hyprland(
    runner: FakeRunner,
    available: tuple[str, ...] = (
        "hyprctl",
        "grim",
        "wtype",
        "wl-input",
        "wlrctl",
    ),
) -> HyprlandAdapter:
    async def no_sleep(_: float) -> None:
        return None

    return HyprlandAdapter(
        runner=runner,
        env=HYPR_ENV,
        home="/home/tester",
        which=lambda name: f"/usr/bin/{name}" if name in available else None,
        sleep=no_sleep,
    )


def x11_responder(
    *,
    monitors: str = (
        "Monitors: 2\n"
        " 0: +*eDP-1 1920/344x1080/193+0+0  eDP-1\n"
        " 1: +HDMI-1 2560/600x1440/340+1920+0  HDMI-1\n"
    ),
    active: str = "1001",
    cursor: str = "X=300\nY=200\nSCREEN=0\nWINDOW=1001\n",
    grim_width: int = 640,
    grim_height: int = 480,
) -> Callable[[Call], CommandResult]:
    def respond(call: Call) -> CommandResult:
        if call.name == "xdotool":
            args = call.args
            if args[:1] == ["getdisplaygeometry"]:
                return ok("1920 1080\n")
            if args[:1] == ["search"]:
                return ok("1001\n1002\n")
            if args[:1] == ["getactivewindow"]:
                return ok(active + "\n")
            if args[:2] == ["getwindowname", "1001"]:
                return ok("Terminal\n")
            if args[:2] == ["getwindowname", "1002"]:
                return ok("Browser\n")
            if args[:1] == ["getwindowgeometry"]:
                window_id = args[-1]
                if window_id == "1001":
                    return ok("WINDOW=1001\nX=100\nY=120\nWIDTH=800\nHEIGHT=600\nSCREEN=0\n")
                return ok("WINDOW=1002\nX=2200\nY=40\nWIDTH=600\nHEIGHT=400\nSCREEN=0\n")
            if args[:1] == ["getwindowpid"]:
                return ok("4242\n")
            if args[:1] == ["getmouselocation"]:
                return ok(cursor)
            if args[:1] == ["windowactivate"]:
                return ok()
            return ok()
        if call.name == "xrandr" and call.args[:1] == ["--listmonitors"]:
            return ok(monitors)
        if call.name == "import":
            return ok(png_header(grim_width, grim_height))
        if call.name == "scrot":
            return ok()
        return CommandResult(False, 127, b"", "not found")

    return respond


def make_x11(
    runner: FakeRunner,
    available: tuple[str, ...] = ("xdotool", "import", "scrot", "xrandr"),
) -> X11Adapter:
    async def no_sleep(_: float) -> None:
        return None

    return X11Adapter(
        runner=runner,
        env={"DISPLAY": ":0", "PATH": "/usr/bin", "HOME": "/home/tester"},
        home="/home/tester",
        which=lambda name: f"/usr/bin/{name}" if name in available else None,
        sleep=no_sleep,
    )


class HyprlandDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_054_focus_uses_string_dispatch(self) -> None:
        runner = FakeRunner(hyprland_responder(version="0.54.3"))
        adapter = make_hyprland(runner)
        result = await adapter.focus_window("0x55aa11")
        dispatch = runner.find("hyprctl", lambda call: call.args[:1] == ["dispatch"])
        assert dispatch is not None
        self.assertEqual(
            dispatch.args, ["dispatch", "focuswindow", "address:0x55aa11"]
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.verification.ok)

    async def test_056_focus_uses_lua_dispatch(self) -> None:
        runner = FakeRunner(hyprland_responder(version="0.56.2"))
        adapter = make_hyprland(runner)
        result = await adapter.focus_window("0x55aa11")
        dispatch = runner.find("hyprctl", lambda call: call.args[:1] == ["dispatch"])
        assert dispatch is not None
        self.assertEqual(
            dispatch.args,
            ["dispatch", 'hl.dsp.focus({window="address:0x55aa11"})'],
        )
        self.assertTrue(result.ok)

    async def test_056_wlrctl_fallback_uses_lua_cursor_move(self) -> None:
        runner = FakeRunner(hyprland_responder(version="0.56.2"))
        adapter = make_hyprland(
            runner, ("hyprctl", "grim", "wtype", "wlrctl")
        )
        result = await adapter.pointer(
            UiPointerRequest(action="click", x=300, y=200)
        )
        dispatch = runner.find("hyprctl", lambda call: call.args[:1] == ["dispatch"])
        assert dispatch is not None
        self.assertEqual(
            dispatch.args, ["dispatch", "hl.dsp.cursor.move({x=300, y=200})"]
        )
        wlrctl = runner.find("wlrctl")
        assert wlrctl is not None
        self.assertEqual(wlrctl.args, ["pointer", "click", "left"])
        self.assertEqual(result.backend, "wlrctl")
        self.assertIn("wlrctl fallback", result.warnings[0])

    async def test_focus_verification_failure_reports_not_ok(self) -> None:
        runner = FakeRunner(hyprland_responder(active="0xdeadbeef"))
        adapter = make_hyprland(runner)
        result = await adapter.focus_window("0x55aa11")
        self.assertFalse(result.ok)
        assert result.verification is not None
        self.assertFalse(result.verification.ok)
        self.assertIn("did not settle", result.verification.detail or "")

    async def test_pointer_cursor_verification_failure_reports_not_ok(self) -> None:
        runner = FakeRunner(hyprland_responder(cursor="900, 300"))
        adapter = make_hyprland(runner)
        result = await adapter.pointer(
            UiPointerRequest(action="click", x=300, y=200)
        )
        self.assertFalse(result.ok)
        assert result.verification is not None
        self.assertFalse(result.verification.ok)
        self.assertIn("expected 300,200", result.verification.detail or "")


class HyprlandCapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_grim_reports_capability_and_capture_error(self) -> None:
        runner = FakeRunner(hyprland_responder())
        adapter = make_hyprland(
            runner, ("hyprctl", "wtype", "wl-input", "wlrctl")
        )
        environment = await adapter.environment()
        self.assertTrue(environment.graphical)
        capabilities = await adapter.capabilities()
        capture = next(item for item in capabilities if item.id == "screen.capture")
        self.assertFalse(capture.available)
        self.assertEqual(capture.reason, "grim not installed")
        with self.assertRaises(UiError) as context:
            await adapter.capture(UiCaptureRequest(target="display"))
        self.assertEqual(context.exception.code, "missing_dependency")
        self.assertIn("grim", str(context.exception))

    async def test_missing_wtype_disables_keyboard(self) -> None:
        runner = FakeRunner(hyprland_responder())
        adapter = make_hyprland(
            runner, ("hyprctl", "grim", "wl-input", "wlrctl")
        )
        capabilities = await adapter.capabilities()
        keyboard = next(item for item in capabilities if item.id == "keyboard.type")
        self.assertFalse(keyboard.available)
        self.assertEqual(keyboard.reason, "wtype not installed")
        with self.assertRaises(UiError) as context:
            await adapter.type_text("hello")
        self.assertEqual(context.exception.code, "missing_dependency")

    async def test_drag_is_unavailable_without_wl_input(self) -> None:
        runner = FakeRunner(hyprland_responder())
        adapter = make_hyprland(
            runner, ("hyprctl", "grim", "wtype", "wlrctl")
        )
        capabilities = await adapter.capabilities()
        drag = next(item for item in capabilities if item.id == "pointer.drag")
        self.assertFalse(drag.available)
        self.assertEqual(drag.reason, "drag needs the wl-input helper")
        with self.assertRaises(UiError) as context:
            await adapter.pointer(
                UiPointerRequest(
                    action="drag", x=100, y=100, to_x=200, to_y=200
                )
            )
        self.assertEqual(context.exception.code, "capability_unavailable")
        self.assertIn("wl-input", str(context.exception))
        self.assertEqual(runner.calls_for("wlrctl"), [])

    async def test_environment_warns_about_missing_helpers(self) -> None:
        runner = FakeRunner(hyprland_responder())
        adapter = make_hyprland(runner, ("hyprctl", "wlrctl"))
        environment = await adapter.environment()
        self.assertFalse(environment.dependencies["grim"]["available"])
        self.assertFalse(environment.dependencies["wtype"]["available"])
        self.assertFalse(environment.dependencies["wl-input"]["available"])
        joined = " ".join(environment.warnings)
        self.assertIn("grim is not installed", joined)
        self.assertIn("wtype is not installed", joined)
        self.assertIn("wl-input helper not found", joined)


class HyprlandCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

    async def test_display_capture_metadata(self) -> None:
        runner = FakeRunner(hyprland_responder(grim_width=2560, grim_height=1440))
        adapter = make_hyprland(runner)
        capture = await adapter.capture(
            UiCaptureRequest(target="display", display="DP-1")
        )
        grim = runner.find("grim")
        assert grim is not None
        self.assertEqual(grim.args, ["-g", "0,0 2048x1152", "-t", "png", "-"])
        self.assertEqual((capture.pixel_width, capture.pixel_height), (2560, 1440))
        self.assertEqual(capture.origin.x, 0)
        self.assertEqual(capture.origin.y, 0)
        self.assertEqual(capture.scale, 1.25)
        self.assertEqual(capture.display, "DP-1")
        # global = origin + pixel / scale
        self.assertEqual(round(2560 / capture.scale), 2048)
        self.assertEqual(round(1440 / capture.scale), 1152)

    async def test_window_capture_metadata(self) -> None:
        runner = FakeRunner(hyprland_responder(grim_width=1000, grim_height=750))
        adapter = make_hyprland(runner)
        capture = await adapter.capture(
            UiCaptureRequest(target="window", window_id="0x55aa11")
        )
        grim = runner.find("grim")
        assert grim is not None
        self.assertEqual(grim.args, ["-g", "100,120 800x600", "-t", "png", "-"])
        self.assertEqual(capture.origin.x, 100)
        self.assertEqual(capture.origin.y, 120)
        self.assertEqual(capture.scale, 1.25)
        self.assertEqual((capture.pixel_width, capture.pixel_height), (1000, 750))

    async def test_region_capture_uses_second_display(self) -> None:
        runner = FakeRunner(hyprland_responder(grim_width=600, grim_height=400))
        adapter = make_hyprland(runner)
        capture = await adapter.capture(
            UiCaptureRequest(
                target="region",
                region=UiRect(2100, 40, 600, 400),
            )
        )
        grim = runner.find("grim")
        assert grim is not None
        self.assertEqual(grim.args, ["-g", "2100,40 600x400", "-t", "png", "-"])
        self.assertEqual(capture.origin.x, 2100)
        self.assertEqual(capture.scale, 1.0)
        self.assertEqual(capture.display, "HDMI-A-1")

    async def test_all_capture_reports_layout_bounds_and_mixed_scale_warning(self) -> None:
        runner = FakeRunner(hyprland_responder(grim_width=5120, grim_height=1440))
        adapter = make_hyprland(runner)
        capture = await adapter.capture(UiCaptureRequest(target="all"))
        grim = runner.find("grim")
        assert grim is not None
        self.assertEqual(grim.args, ["-t", "png", "-"])
        self.assertEqual((capture.origin.x, capture.origin.y), (0, 0))
        self.assertEqual(capture.scale, 1.25)
        self.assertEqual(capture.display, "all")
        self.assertTrue(any("different scales" in item for item in capture.warnings))

    async def test_save_to_writes_file_and_omits_base64(self) -> None:
        runner = FakeRunner(hyprland_responder(grim_width=2560, grim_height=1440))
        adapter = make_hyprland(runner)
        target = Path(self.tempdir.name) / "nested" / "screen.png"
        capture = await adapter.capture(
            UiCaptureRequest(target="display", save_to=str(target))
        )
        self.assertTrue(target.exists())
        self.assertEqual(target.stat().st_size, capture.size_bytes)
        self.assertIsNone(capture.bytes_base64)
        self.assertEqual(capture.saved_path, str(target))

    async def test_jpeg_capture_sets_quality_and_reads_header(self) -> None:
        runner = FakeRunner(
            lambda call: (
                ok(jpeg_header(800, 600))
                if call.name == "grim"
                else hyprland_responder()(call)
            )
        )
        adapter = make_hyprland(runner)
        capture = await adapter.capture(
            UiCaptureRequest(target="display", format="jpeg", quality=77)
        )
        grim = runner.find("grim")
        assert grim is not None
        self.assertEqual(
            grim.args, ["-g", "0,0 2048x1152", "-t", "jpeg", "-q", "77", "-"]
        )
        self.assertEqual((capture.pixel_width, capture.pixel_height), (800, 600))


class HyprlandPointerBoundsTests(unittest.IsolatedAsyncioTestCase):
    async def test_multi_display_bounds_and_space_argument(self) -> None:
        runner = FakeRunner(hyprland_responder(cursor="2100, 40"))
        adapter = make_hyprland(runner)
        result = await adapter.pointer(
            UiPointerRequest(action="click", x=2100, y=40)
        )
        helper = runner.find("wl-input")
        assert helper is not None
        self.assertEqual(
            helper.args,
            [
                "--space",
                "0",
                "0",
                "3968",
                "1152",
                "click",
                "2100",
                "40",
                "left",
                "1",
            ],
        )
        self.assertTrue(result.ok)

    async def test_off_layout_point_is_refused_without_input(self) -> None:
        runner = FakeRunner(hyprland_responder())
        adapter = make_hyprland(runner)
        with self.assertRaises(UiError) as context:
            await adapter.pointer(
                UiPointerRequest(action="click", x=4000, y=100)
            )
        self.assertEqual(context.exception.code, "invalid_request")
        self.assertIn("outside the desktop layout", str(context.exception))
        self.assertEqual(runner.calls_for("wl-input"), [])

    async def test_drag_passes_destination_and_duration(self) -> None:
        runner = FakeRunner(hyprland_responder(cursor="640, 480"))
        adapter = make_hyprland(runner)
        result = await adapter.pointer(
            UiPointerRequest(
                action="drag",
                x=120,
                y=140,
                to_x=640,
                to_y=480,
                duration_ms=600,
            )
        )
        helper = runner.find("wl-input")
        assert helper is not None
        self.assertEqual(
            helper.args[-7:],
            ["drag", "120", "140", "640", "480", "left", "600"],
        )
        self.assertTrue(result.ok)
        self.assertIn("dragged to 640,480", result.verification.detail or "")

    async def test_move_without_pointer_helpers_uses_hyprctl_warp(self) -> None:
        runner = FakeRunner(hyprland_responder(cursor="300, 200"))
        adapter = make_hyprland(runner, ("hyprctl", "grim", "wtype"))
        result = await adapter.pointer(
            UiPointerRequest(action="move", x=300, y=200)
        )
        dispatch = runner.find(
            "hyprctl", lambda call: call.args[:1] == ["dispatch"]
        )
        assert dispatch is not None
        self.assertEqual(dispatch.args, ["dispatch", "movecursor", "300", "200"])
        self.assertEqual(runner.calls_for("wlrctl"), [])
        self.assertEqual(result.backend, "hyprctl")
        self.assertTrue(result.ok)


class SelectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        reset_platform_adapter_for_tests()

    async def test_explicit_signature_selects_hyprland(self) -> None:
        selection = await select_platform_adapter(
            platform="linux",
            env={
                "HYPRLAND_INSTANCE_SIGNATURE": "abc",
                "WAYLAND_DISPLAY": "wayland-1",
            },
            refresh=True,
        )
        self.assertEqual(selection.kind, "hyprland")
        self.assertIsInstance(selection.adapter, HyprlandAdapter)
        self.assertEqual(selection.reason, "HYPRLAND_INSTANCE_SIGNATURE")

    async def test_display_only_selects_x11(self) -> None:
        selection = await select_platform_adapter(
            platform="linux", env={"DISPLAY": ":0"}, refresh=True
        )
        self.assertEqual(selection.kind, "x11")
        self.assertIsInstance(selection.adapter, X11Adapter)
        self.assertEqual(selection.reason, "DISPLAY is set")

    async def test_no_session_is_unsupported_even_on_linux(self) -> None:
        selection = await select_platform_adapter(
            platform="linux", env={}, refresh=True
        )
        self.assertEqual(selection.kind, "unsupported")
        self.assertIsInstance(selection.adapter, UnsupportedAdapter)
        environment = await selection.adapter.environment()
        self.assertFalse(environment.graphical)
        self.assertIn("No graphical session", environment.reason or "")

    async def test_wayland_without_hyprland_falls_back_to_x11(self) -> None:
        selection = await select_platform_adapter(
            platform="linux",
            env={"WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":0"},
            refresh=True,
            which=lambda name: None,
        )
        self.assertEqual(selection.kind, "x11")
        self.assertIn("not Hyprland", selection.reason)

    async def test_explicit_backend_override_wins(self) -> None:
        selection = await select_platform_adapter(
            platform="linux",
            env={
                "HYPRLAND_INSTANCE_SIGNATURE": "abc",
                "WAYLAND_DISPLAY": "wayland-1",
                "HERMES_RELAY_DESKTOP_BACKEND": "x11",
            },
            refresh=True,
        )
        self.assertEqual(selection.kind, "x11")
        self.assertEqual(selection.reason, "explicit X11 selection")

    async def test_non_linux_is_unsupported(self) -> None:
        selection = await select_platform_adapter(
            platform="darwin", env={}, refresh=True
        )
        self.assertEqual(selection.kind, "unsupported")
        environment = await selection.adapter.environment()
        self.assertIn("No desktop adapter", environment.reason or "")


class X11Tests(unittest.IsolatedAsyncioTestCase):
    async def test_environment_and_displays(self) -> None:
        runner = FakeRunner(x11_responder())
        adapter = make_x11(runner)
        environment = await adapter.environment()
        self.assertTrue(environment.graphical)
        self.assertEqual(environment.session, "x11")
        displays = await adapter.displays()
        self.assertEqual(len(displays), 2)
        self.assertEqual(displays[0].name, "eDP-1")
        self.assertEqual(displays[0].rect, UiRect(0, 0, 1920, 1080))
        self.assertEqual(displays[1].rect, UiRect(1920, 0, 2560, 1440))
        self.assertEqual(displays[0].scale, 1.0)

    async def test_focus_verification_failure(self) -> None:
        runner = FakeRunner(x11_responder(active="1002"))
        adapter = make_x11(runner)
        result = await adapter.focus_window("1001")
        self.assertFalse(result.ok)
        self.assertFalse(result.verification.ok)
        self.assertIn("did not settle", result.verification.detail or "")

    async def test_windows_listing_and_filtering_shape(self) -> None:
        runner = FakeRunner(x11_responder())
        adapter = make_x11(runner)
        windows = await adapter.windows()
        self.assertEqual([window.id for window in windows], ["1001", "1002"])
        self.assertEqual(windows[0].title, "Terminal")
        self.assertTrue(windows[0].focused)
        self.assertEqual(windows[1].display, "HDMI-1")

    async def test_pointer_scroll_and_drag(self) -> None:
        runner = FakeRunner(x11_responder(cursor="X=300\nY=200\n"))
        adapter = make_x11(runner)
        await adapter.pointer(
            UiPointerRequest(action="scroll", x=300, y=200, delta_y=2)
        )
        clicks = [
            call.args for call in runner.calls_for("xdotool") if call.args[0] == "click"
        ]
        self.assertIn(["--repeat", "2", "5"], [args[1:] for args in clicks])

        runner.calls.clear()
        await adapter.pointer(
            UiPointerRequest(
                action="drag", x=300, y=200, to_x=500, to_y=400
            )
        )
        commands = [call.args[:1] for call in runner.calls_for("xdotool")]
        self.assertIn(["mousedown"], commands)
        self.assertIn(["mouseup"], commands)
        self.assertIn(
            ["mousemove", "--sync", "500", "400"],
            [call.args for call in runner.calls_for("xdotool")],
        )

    async def test_capture_metadata_and_origin(self) -> None:
        runner = FakeRunner(x11_responder(grim_width=600, grim_height=400))
        adapter = make_x11(runner)
        capture = await adapter.capture(
            UiCaptureRequest(target="region", region=UiRect(2100, 40, 600, 400))
        )
        import_call = runner.find("import")
        assert import_call is not None
        self.assertEqual(
            import_call.args,
            ["-window", "root", "-crop", "600x400+2100+40", "png:-"],
        )
        self.assertEqual((capture.pixel_width, capture.pixel_height), (600, 400))
        self.assertEqual((capture.origin.x, capture.origin.y), (2100, 40))
        self.assertEqual(capture.scale, 1.0)
        self.assertEqual(capture.display, "HDMI-1")


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.ctx = ToolContext(cwd=self.tempdir.name)
        reset_platform_adapter_for_tests()

    async def asyncTearDown(self) -> None:
        reset_platform_adapter_for_tests()

    def _register_with(self, adapter: Any) -> ToolRegistry:
        selection = AdapterSelection(adapter, "hyprland", "test selection")

        async def fake_select(**_: Any) -> AdapterSelection:
            return selection

        patcher = patch.object(
            desktop_ui_handler, "select_platform_adapter", fake_select
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return build_registry(state=ClientState())

    async def test_status_reports_adapter_environment_and_capabilities(self) -> None:
        runner = FakeRunner(hyprland_responder())
        registry = self._register_with(make_hyprland(runner))
        result = await registry.invoke("desktop_ui_status", {}, self.ctx)
        self.assertTrue(result["ok"])
        self.assertEqual(result["adapter"], "linux-hyprland")
        self.assertTrue(result["environment"]["graphical"])
        self.assertGreaterEqual(len(result["displays"]), 2)
        self.assertTrue(
            any(item["id"] == "screen.capture" for item in result["capabilities"])
        )

    async def test_windows_filter_and_focus(self) -> None:
        runner = FakeRunner(hyprland_responder())
        registry = self._register_with(make_hyprland(runner))
        listing = await registry.invoke(
            "desktop_ui_windows", {"app": "firefox"}, self.ctx
        )
        self.assertTrue(listing["ok"])
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["windows"][0]["id"], "0x55bb22")
        self.assertFalse(listing["truncated"])

        focused = await registry.invoke(
            "desktop_ui_windows", {"focus": "0x55aa11"}, self.ctx
        )
        self.assertTrue(focused["ok"])
        self.assertEqual(focused["action"], "focus_window")

    async def test_screenshot_returns_coordinate_contract(self) -> None:
        runner = FakeRunner(hyprland_responder(grim_width=2560, grim_height=1440))
        registry = self._register_with(make_hyprland(runner))
        result = await registry.invoke(
            "desktop_ui_screenshot", {"target": "display", "display": "DP-1"}, self.ctx
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["pixel_width"], 2560)
        self.assertEqual(result["pixel_height"], 1440)
        self.assertEqual(result["origin"], {"x": 0, "y": 0})
        self.assertEqual(result["scale"], 1.25)
        self.assertIn("bytes_base64", result)

    async def test_action_drag_returns_capability_unavailable(self) -> None:
        runner = FakeRunner(hyprland_responder())
        registry = self._register_with(
            make_hyprland(runner, ("hyprctl", "grim", "wtype", "wlrctl"))
        )
        result = await registry.invoke(
            "desktop_ui_action",
            {"action": "drag", "x": 100, "y": 100, "to_x": 200, "to_y": 200},
            self.ctx,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "capability_unavailable")
        self.assertIn("wl-input", result["message"])

    async def test_action_wait_and_click(self) -> None:
        runner = FakeRunner(hyprland_responder())
        registry = self._register_with(make_hyprland(runner))
        waited = await registry.invoke(
            "desktop_ui_action", {"action": "wait", "duration_ms": 1}, self.ctx
        )
        self.assertTrue(waited["ok"])
        clicked = await registry.invoke(
            "desktop_ui_action",
            {"action": "click", "x": 300, "y": 200, "verify": False},
            self.ctx,
        )
        self.assertTrue(clicked["ok"])
        self.assertEqual(clicked["action"], "click")

    async def test_action_type_uses_wtype_keyboard(self) -> None:
        runner = FakeRunner(hyprland_responder())
        registry = self._register_with(make_hyprland(runner))
        result = await registry.invoke(
            "desktop_ui_action", {"action": "type", "text": "hello"}, self.ctx
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "type")
        typing = runner.find("wtype")
        assert typing is not None
        self.assertEqual(typing.args, ["-s", "30", "-d", "10", "-"])
        self.assertEqual(typing.stdin, "hello")

    async def test_legacy_desktop_screenshot_shape(self) -> None:
        runner = FakeRunner(hyprland_responder(grim_width=2560, grim_height=1440))
        registry = self._register_with(make_hyprland(runner))
        result = await registry.invoke("desktop_screenshot", {}, self.ctx)
        self.assertEqual(set(result), {"format", "bytes_base64", "size_bytes"})
        self.assertEqual(result["format"], "png")
        self.assertTrue(result["bytes_base64"].startswith("iVBORw0KGgo"))

    async def test_legacy_desktop_screenshot_display_aliases(self) -> None:
        runner = FakeRunner(hyprland_responder(grim_width=2560, grim_height=1440))
        registry = self._register_with(make_hyprland(runner))
        await registry.invoke("desktop_screenshot", {"display": "all"}, self.ctx)
        all_call = runner.find("grim")
        assert all_call is not None
        self.assertEqual(all_call.args, ["-t", "png", "-"])

        runner.calls.clear()
        await registry.invoke("desktop_screenshot", {"display": "primary"}, self.ctx)
        primary_call = runner.find("grim")
        assert primary_call is not None
        self.assertEqual(
            primary_call.args, ["-g", "0,0 2048x1152", "-t", "png", "-"]
        )

    async def test_heartbeat_reports_selected_adapter_capabilities(self) -> None:
        runner = FakeRunner(hyprland_responder())
        adapter = make_hyprland(runner)
        registry = build_registry(state=ClientState())
        selection = AdapterSelection(adapter, "hyprland", "test selection")

        async def fake_select(**_: Any) -> AdapterSelection:
            return selection

        with patch(
            "plugin.relay.client.platform.select_platform_adapter", fake_select
        ):
            payload = await build_live_status_payload(
                ClientState(device_id="dev-1"), registry
            )
        block = payload["capabilities"]
        self.assertEqual(block["adapter"], "linux-hyprland")
        self.assertTrue(block["graphical"])
        self.assertEqual(block["compositor"], "hyprland")
        self.assertTrue(
            any(item["id"] == "pointer.drag" for item in block["capabilities"])
        )

    async def test_unavailable_session_reports_no_graphical_session(self) -> None:
        registry = self._register_with(
            UnsupportedAdapter("linux", "No graphical session found.")
        )
        status = await registry.invoke("desktop_ui_status", {}, self.ctx)
        self.assertTrue(status["ok"])
        self.assertFalse(status["environment"]["graphical"])
        windows = await registry.invoke("desktop_ui_windows", {}, self.ctx)
        self.assertFalse(windows["ok"])
        self.assertEqual(windows["code"], "no_graphical_session")


class SubprocessRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_kills_child_and_reports_it(self) -> None:
        runner = SubprocessRunner()
        result = await runner.text(
            sys.executable,
            ["-c", "import time; time.sleep(5)"],
            timeout_ms=100,
        )
        self.assertTrue(result.ran)
        self.assertTrue(result.timed_out)
        self.assertIn("timed out", result.stderr)
        self.assertNotEqual(result.exit_code, 0)

    async def test_output_cap_kills_and_marks_too_large(self) -> None:
        runner = SubprocessRunner()
        result = await runner.run(
            sys.executable,
            ["-c", "print('x' * 100000)"],
            timeout_ms=2_000,
            max_bytes=1_000,
        )
        self.assertTrue(result.ran)
        self.assertTrue(result.too_large)
        self.assertFalse(result.timed_out)
        self.assertLessEqual(len(result.stdout), 1_000)
        self.assertIn("output exceeded", result.stderr)

    async def test_missing_binary_is_ran_false(self) -> None:
        runner = SubprocessRunner()
        result = await runner.text(
            "/definitely/not/a/binary", [], timeout_ms=100
        )
        self.assertFalse(result.ran)

    async def test_which_binary_resolves_and_rejects_missing(self) -> None:
        resolved = which_binary("sh", path_value="/bin:/usr/bin")
        self.assertIsNotNone(resolved)
        self.assertIsNone(which_binary("hermes-relay-no-such-binary"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
