"""Unit tests for the phase-4 Windows platform adapter.

Every PowerShell call goes through ``FakeRunner``; no Windows host, PowerShell
binary, or desktop session is required.  The suite covers the parts that broke
on real hardware during the TypeScript port:

* a 2880x1800 panel driven at 1920x1200 logical reports dpi_scale 1.5, not 1;
* focus is only a success when the foreground actually moved, otherwise the
  adapter raises ``input_failed`` with the real foreground window;
* window listing drops zero-size and hidden windows;
* display capture is physical-pixel capture with an honest coordinate contract;
* window/region capture and drag are explicit ``capability_unavailable``
  results rather than silently degrading to display capture or a click;
* capture paths with spaces and quotes survive PowerShell quoting.

A live-Windows smoke test is deliberately absent from the default suite.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

from plugin.relay.client.dispatch import ClientState, ToolContext
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
    UiPoint,
    UiPointerRequest,
    UiRect,
)
from plugin.relay.client.platform.command import CommandResult, CommandRunner
from plugin.relay.client.platform.windows import (
    DRAG_REASON,
    WindowsAdapter,
    escape_send_keys_text,
    normalize_keys,
)

POWERSHELL = r"C:\Windows\System32\powershell.exe"

ENV = {
    "SESSIONNAME": "Console",
    "PATH": r"C:\Windows\System32",
    "TEMP": r"C:\Temp",
    "SystemRoot": "/nonexistent-system-root-for-tests",
}

DISPLAY_150 = {
    "name": r"\\.\DISPLAY1",
    "primary": True,
    "x": 0,
    "y": 0,
    "width": 1920,
    "height": 1200,
    "physical_width": 2880,
    "physical_height": 1800,
    "dpi": 144,
}

WINDOW_ROWS: list[dict[str, Any]] = [
    {
        "id": 4242,
        "app": "notepad",
        "title": "Notes - Notepad",
        "x": 100,
        "y": 120,
        "width": 800,
        "height": 600,
        "visible": True,
    },
    {
        "id": 5151,
        "app": "chrome",
        "title": "Docs",
        "x": 1000,
        "y": 200,
        "width": 900,
        "height": 700,
        "visible": True,
    },
    {
        "id": 6000,
        "app": "phantom",
        "title": "Zero size",
        "x": 0,
        "y": 0,
        "width": 0,
        "height": 0,
        "visible": True,
    },
    {
        "id": 7000,
        "app": "hidden",
        "title": "Hidden",
        "x": 10,
        "y": 10,
        "width": 300,
        "height": 200,
        "visible": False,
    },
    {
        "id": "4243",
        "app": "string-id",
        "title": "Bad id",
        "x": 0,
        "y": 0,
        "width": 300,
        "height": 200,
        "visible": True,
    },
    {
        "id": True,
        "app": "bool-id",
        "title": "Bad id",
        "x": 0,
        "y": 0,
        "width": 300,
        "height": 200,
        "visible": True,
    },
    {
        "app": "missing-id",
        "title": "Bad id",
        "x": 0,
        "y": 0,
        "width": 300,
        "height": 200,
        "visible": True,
    },
]


def png_header(width: int = 2880, height: int = 1800) -> bytes:
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
    def script(self) -> str:
        return self.args[-1] if self.args else ""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Call({self.cmd!r}, {self.args[:5]!r}, ...)"


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

    def scripts(self, marker: str) -> list[str]:
        return [call.script for call in self.calls if marker in call.script]


def ok(stdout: Any = "", stderr: str = "", exit_code: int = 0) -> CommandResult:
    if not isinstance(stdout, (bytes, bytearray)):
        stdout = str(stdout).encode("utf-8")
    return CommandResult(True, exit_code, bytes(stdout), stderr)


def fail(stderr: str = "boom", exit_code: int = 1) -> CommandResult:
    return CommandResult(True, exit_code, b"", stderr)


def decode_payload(script: str) -> dict[str, Any]:
    match = re.search(r"FromBase64String\('([A-Za-z0-9+/=]+)'\)", script)
    assert match is not None, "script has no base64 payload"
    return json.loads(base64.b64decode(match.group(1)).decode("utf-8"))


def extract_path(script: str) -> str:
    match = re.search(r"\$outPath = '((?:[^']|'')*)'", script)
    assert match is not None, "script has no quoted output path"
    return match.group(1).replace("''", "'")


class WindowsResponder:
    """Emulates the PowerShell scripts on a 150% single-monitor desktop."""

    def __init__(
        self,
        *,
        displays: Any = None,
        windows: list[dict[str, Any]] | None = None,
        focused_pid: int = 4242,
        focus_moves: bool = True,
        focus_activated: bool = True,
        cursor: tuple[int, int] = (150, 300),
        cursor_available: bool = True,
        capture_size: tuple[int, int] = (2880, 1800),
        write_capture: bool = True,
        display_exit: int = 0,
        list_exit: int = 0,
        keyboard_response: dict[str, Any] | None = None,
    ) -> None:
        self.displays = DISPLAY_150 if displays is None else displays
        self.windows = WINDOW_ROWS if windows is None else windows
        self.focused_pid = focused_pid
        self.focus_moves = focus_moves
        self.focus_activated = focus_activated
        self.cursor = cursor
        self.cursor_available = cursor_available
        self.capture_size = capture_size
        self.write_capture = write_capture
        self.display_exit = display_exit
        self.list_exit = list_exit
        self.keyboard_response = keyboard_response
        self.last_pointer: dict[str, Any] | None = None
        self.last_keyboard: dict[str, Any] | None = None
        self.capture_paths: list[str] = []

    def __call__(self, call: Call) -> CommandResult:
        script = call.script
        if "RelayWindowsDisplay" in script:
            if self.display_exit:
                return fail("Get-CimInstance failed", self.display_exit)
            return ok(json.dumps(self.displays))
        if "RelayWindowsList" in script:
            if self.list_exit:
                return fail("Get-Process failed", self.list_exit)
            rows = [
                dict(row, focused=row.get("id") == self.focused_pid)
                for row in self.windows
            ]
            return ok(json.dumps(rows))
        if "RelayWindowsFocus" in script:
            payload = decode_payload(script)
            if self.focus_moves:
                self.focused_pid = int(payload["pid"])
            return ok(
                json.dumps(
                    {
                        "activated": self.focus_activated,
                        "foreground_pid": self.focused_pid,
                        "foreground_handle": 1,
                    }
                )
            )
        if "RelayWindowsCapture" in script:
            path = extract_path(script)
            self.capture_paths.append(path)
            if self.write_capture:
                width, height = self.capture_size
                if "EncoderParameters" in script or "::Jpeg" in script:
                    payload = jpeg_header(width, height)
                else:
                    payload = png_header(width, height)
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                Path(path).write_bytes(payload)
            return ok(
                json.dumps(
                    {
                        "ok": True,
                        "path": path,
                        "width": self.capture_size[0],
                        "height": self.capture_size[1],
                    }
                )
            )
        if "RelayWindowsPointer" in script:
            self.last_pointer = decode_payload(script)
            if not self.cursor_available:
                return ok(json.dumps({"ok": True}))
            return ok(
                json.dumps(
                    {
                        "ok": True,
                        "cursor_x": self.cursor[0],
                        "cursor_y": self.cursor[1],
                    }
                )
            )
        if "RelayWindowsKeyboard" in script:
            self.last_keyboard = decode_payload(script)
            if self.keyboard_response is not None:
                return ok(json.dumps(self.keyboard_response))
            return ok(
                json.dumps(
                    {
                        "ok": True,
                        "foreground_pid": 4242,
                        "foreground_app": "notepad",
                        "foreground_title": "Notes - Notepad",
                        "after_pid": 4242,
                    }
                )
            )
        raise AssertionError(f"unexpected PowerShell script: {script[:160]}")


def make_windows(
    runner: FakeRunner,
    *,
    available: tuple[str, ...] = ("powershell",),
    temp_dir: str | None = None,
    platform: str = "win32",
    env: dict[str, str] | None = None,
) -> WindowsAdapter:
    async def no_sleep(_: float) -> None:
        return None

    return WindowsAdapter(
        runner=runner,
        env={**ENV, **(env or {})},
        platform=platform,
        which=(
            lambda name: rf"C:\Windows\System32\{name}.exe"
            if name in available
            else None
        ),
        sleep=no_sleep,
        temp_dir=temp_dir,
    )


def no_pointer_scripts(runner: FakeRunner) -> bool:
    return not runner.scripts("RelayWindowsPointer")


class EnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_environment_reports_windows_session_and_powershell(self) -> None:
        adapter = make_windows(FakeRunner(WindowsResponder()))
        environment = await adapter.environment()
        self.assertTrue(environment.graphical)
        self.assertEqual(environment.platform, "win32")
        self.assertEqual(environment.session, "windows")
        self.assertEqual(environment.compositor, "win32")
        self.assertEqual(environment.desktop, "Console")
        self.assertIsNone(environment.reason)
        dependency = environment.dependencies["powershell"]
        self.assertTrue(dependency["available"])
        self.assertTrue(str(dependency["path"]).endswith("powershell.exe"))

    async def test_missing_powershell_is_not_graphical(self) -> None:
        runner = FakeRunner(WindowsResponder())
        adapter = make_windows(runner, available=())
        environment = await adapter.environment()
        self.assertFalse(environment.graphical)
        self.assertIn("PowerShell is not available", environment.reason or "")
        capabilities = {item.id: item for item in await adapter.capabilities()}
        self.assertFalse(capabilities["screen.capture"].available)
        self.assertEqual(capabilities["windows.list"].reason, environment.reason)
        self.assertFalse(capabilities["pointer.drag"].available)
        self.assertIn("legacy user32", capabilities["pointer.drag"].reason or "")
        with self.assertRaises(UiError) as context:
            await adapter.displays()
        self.assertEqual(context.exception.code, "missing_dependency")

    async def test_session_zero_warns_about_the_interactive_session(self) -> None:
        adapter = make_windows(
            FakeRunner(WindowsResponder()), env={"SESSIONNAME": "Services"}
        )
        environment = await adapter.environment()
        self.assertEqual(environment.desktop, "Services")
        self.assertTrue(
            any("session 0" in warning for warning in environment.warnings)
        )

    async def test_non_windows_platform_is_refused(self) -> None:
        adapter = make_windows(
            FakeRunner(WindowsResponder()), platform="linux"
        )
        environment = await adapter.environment()
        self.assertFalse(environment.graphical)
        self.assertEqual(environment.session, "none")
        self.assertIn("requires win32", environment.reason or "")


class DisplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_150_percent_display_reports_logical_rect_and_physical_pixels(
        self,
    ) -> None:
        runner = FakeRunner(WindowsResponder())
        displays = await make_windows(runner).displays()
        self.assertEqual(len(displays), 1)
        display = displays[0]
        self.assertEqual(display.rect, UiRect(0, 0, 1920, 1200))
        self.assertEqual(display.physical_width, 2880)
        self.assertEqual(display.physical_height, 1800)
        self.assertAlmostEqual(display.dpi_scale or 0, 1.5)
        self.assertAlmostEqual(display.scale, 1.5)
        self.assertTrue(display.primary)
        self.assertTrue(display.focused)

        call = runner.calls[0]
        self.assertEqual(call.cmd, POWERSHELL)
        self.assertEqual(
            call.args[:5],
            [
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
            ],
        )
        self.assertIn("Win32_VideoController", call.script)
        self.assertIn("GetDpiForMonitor", call.script)
        self.assertIn("System.Windows.Forms.Screen", call.script)

    async def test_physical_bounds_with_applied_dpi_never_report_scale_one(
        self,
    ) -> None:
        # A DPI-aware host reports Screen.Bounds in physical pixels, so the
        # physical/logical ratio is 1.0; AppliedDPI must still say 1.5 and the
        # rect must be normalized back to logical coordinates.
        display = dict(DISPLAY_150, width=2880, height=1800)
        adapter = make_windows(
            FakeRunner(WindowsResponder(displays=[display]))
        )
        displays = await adapter.displays()
        self.assertAlmostEqual(displays[0].dpi_scale or 0, 1.5)
        self.assertAlmostEqual(displays[0].scale, 1.5)
        self.assertEqual(displays[0].rect, UiRect(0, 0, 1920, 1200))
        self.assertEqual(displays[0].physical_width, 2880)

    async def test_single_object_json_is_accepted(self) -> None:
        adapter = make_windows(
            FakeRunner(WindowsResponder(displays=DISPLAY_150))
        )
        displays = await adapter.displays()
        self.assertEqual(len(displays), 1)
        self.assertEqual(displays[0].name, r"\\.\DISPLAY1")

    async def test_multi_display_logical_layout_is_preserved(self) -> None:
        secondary = {
            "name": r"\\.\DISPLAY2",
            "primary": False,
            "x": 1920,
            "y": 0,
            "width": 1920,
            "height": 1080,
            "physical_width": 1920,
            "physical_height": 1080,
            "dpi": 96,
        }
        adapter = make_windows(
            FakeRunner(WindowsResponder(displays=[DISPLAY_150, secondary]))
        )
        displays = await adapter.displays()
        self.assertEqual(len(displays), 2)
        self.assertEqual(displays[1].rect, UiRect(1920, 0, 1920, 1080))
        self.assertAlmostEqual(displays[1].dpi_scale or 0, 1.0)
        self.assertFalse(displays[1].primary)

    async def test_display_failure_is_compositor_error(self) -> None:
        adapter = make_windows(
            FakeRunner(WindowsResponder(display_exit=1))
        )
        with self.assertRaises(UiError) as context:
            await adapter.displays()
        self.assertEqual(context.exception.code, "compositor_error")


class WindowTests(unittest.IsolatedAsyncioTestCase):
    async def test_window_listing_filters_zero_size_hidden_and_bad_rows(
        self,
    ) -> None:
        windows = await make_windows(FakeRunner(WindowsResponder())).windows()
        self.assertEqual([window.id for window in windows], ["4242", "5151"])
        first = windows[0]
        self.assertEqual(first.pid, 4242)
        self.assertEqual(first.app, "notepad")
        self.assertEqual(first.title, "Notes - Notepad")
        self.assertEqual(first.rect, UiRect(100, 120, 800, 600))
        self.assertTrue(first.focused)
        self.assertTrue(first.visible)
        self.assertFalse(windows[1].focused)

    async def test_window_failure_is_compositor_error(self) -> None:
        adapter = make_windows(FakeRunner(WindowsResponder(list_exit=1)))
        with self.assertRaises(UiError) as context:
            await adapter.windows()
        self.assertEqual(context.exception.code, "compositor_error")
        self.assertIn("Window enumeration failed", str(context.exception))


class FocusTests(unittest.IsolatedAsyncioTestCase):
    async def test_focus_success_verifies_the_foreground(self) -> None:
        responder = WindowsResponder()
        runner = FakeRunner(responder)
        result = await make_windows(runner).focus_window("4242")
        self.assertTrue(result.ok)
        self.assertEqual(result.backend, "powershell+user32")
        self.assertEqual(result.action, "focus_window")
        self.assertTrue(result.verification.ok)
        self.assertEqual(
            result.verification.active_window,
            {"id": "4242", "app": "notepad", "title": "Notes - Notepad"},
        )
        scripts = runner.scripts("RelayWindowsFocus")
        self.assertEqual(len(scripts), 1)
        script = scripts[0]
        for marker in (
            "AttachThreadInput",
            "BringWindowToTop",
            "SetForegroundWindow",
            "[IntPtr](-1)",
            "[IntPtr](-2)",
            "AppActivate",
        ):
            self.assertIn(marker, script)
        self.assertEqual(decode_payload(script), {"pid": 4242})

    async def test_focus_failure_raises_input_failed_with_actual_foreground(
        self,
    ) -> None:
        responder = WindowsResponder(focused_pid=5151, focus_moves=False)
        adapter = make_windows(FakeRunner(responder))
        with self.assertRaises(UiError) as context:
            await adapter.focus_window("4242")
        self.assertEqual(context.exception.code, "input_failed")
        message = str(context.exception)
        self.assertIn("Focus did not move to notepad (Notes - Notepad)", message)
        self.assertIn("chrome (Docs)", message)
        self.assertIn("blocking a background process", message)

    async def test_activation_refusal_is_input_failed(self) -> None:
        responder = WindowsResponder(
            focused_pid=5151, focus_moves=False, focus_activated=False
        )
        adapter = make_windows(FakeRunner(responder))
        with self.assertRaises(UiError) as context:
            await adapter.focus_window("4242")
        self.assertEqual(context.exception.code, "input_failed")
        self.assertIn("AppActivate failed for notepad", str(context.exception))

    async def test_non_numeric_window_id_is_invalid_request(self) -> None:
        runner = FakeRunner(WindowsResponder())
        with self.assertRaises(UiError) as context:
            await make_windows(runner).focus_window("0x1234")
        self.assertEqual(context.exception.code, "invalid_request")
        self.assertEqual(runner.calls, [])

    async def test_missing_window_is_window_not_found(self) -> None:
        rows = [row for row in WINDOW_ROWS if row.get("id") != 4242]
        responder = WindowsResponder(windows=rows, focused_pid=5151)
        adapter = make_windows(FakeRunner(responder))
        with self.assertRaises(UiError) as context:
            await adapter.focus_window("4242")
        self.assertEqual(context.exception.code, "window_not_found")


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="hermes-relay-windows-")
        self.addCleanup(shutil.rmtree, self.base, True)

    async def test_display_capture_reports_physical_pixels_and_contract(
        self,
    ) -> None:
        responder = WindowsResponder()
        runner = FakeRunner(responder)
        adapter = make_windows(runner, temp_dir=self.base)
        capture = await adapter.capture(UiCaptureRequest(target="display"))
        self.assertEqual((capture.pixel_width, capture.pixel_height), (2880, 1800))
        self.assertEqual(capture.origin, UiPoint(0, 0))
        self.assertAlmostEqual(capture.scale, 1.5)
        self.assertAlmostEqual(capture.dpi_scale or 0, 1.5)
        self.assertEqual(capture.physical_width, 2880)
        self.assertEqual(capture.physical_height, 1800)
        self.assertEqual(capture.display, r"\\.\DISPLAY1")
        self.assertEqual(capture.target, "display")
        self.assertIsNotNone(capture.bytes_base64)
        payload = base64.b64decode(capture.bytes_base64 or "")
        self.assertTrue(payload.startswith(b"\x89PNG"))
        self.assertEqual(capture.size_bytes, len(payload))
        # global_logical = origin + pixel / scale
        self.assertEqual(round(2880 / capture.scale), 1920)
        self.assertEqual(round(1800 / capture.scale), 1200)

        script = runner.scripts("RelayWindowsCapture")[0]
        self.assertIn("SetProcessDpiAwarenessContext", script)
        self.assertIn("CopyFromScreen([int]0, [int]0", script)
        self.assertIn("[int]2880", script)
        self.assertIn("[int]1800", script)
        self.assertFalse(Path(responder.capture_paths[0]).exists())

    async def test_capture_path_quotes_spaces_and_quotes(self) -> None:
        temp_dir = os.path.join(self.base, "Hermes Relay 'quoted' shots")
        responder = WindowsResponder()
        runner = FakeRunner(responder)
        adapter = make_windows(runner, temp_dir=temp_dir)
        capture = await adapter.capture(UiCaptureRequest(target="display"))
        self.assertTrue(capture.bytes_base64)
        script = runner.scripts("RelayWindowsCapture")[0]
        expected = "'" + temp_dir.replace("'", "''") + os.sep
        self.assertIn("$outPath = " + expected, script)
        self.assertIn("''quoted''", script)
        self.assertNotIn("$outPath = '" + temp_dir + "'", script)
        self.assertTrue(responder.capture_paths[0].startswith(temp_dir))

    async def test_save_to_writes_the_exact_path_with_spaces_and_quotes(
        self,
    ) -> None:
        target = os.path.join(
            self.base, "Hermes 'Relay' Shots", "screen 1.png"
        )
        adapter = make_windows(
            FakeRunner(WindowsResponder()), temp_dir=self.base
        )
        capture = await adapter.capture(
            UiCaptureRequest(target="display", save_to=target)
        )
        self.assertEqual(capture.saved_path, target)
        self.assertIsNone(capture.bytes_base64)
        self.assertTrue(Path(target).exists())
        self.assertEqual(Path(target).read_bytes(), png_header(2880, 1800))

    async def test_window_capture_is_capability_unavailable(self) -> None:
        runner = FakeRunner(WindowsResponder())
        adapter = make_windows(runner)
        with self.assertRaises(UiError) as context:
            await adapter.capture(
                UiCaptureRequest(target="window", window_id="4242")
            )
        self.assertEqual(context.exception.code, "capability_unavailable")
        self.assertIn("window capture is not implemented", str(context.exception))
        self.assertEqual(runner.calls, [])

    async def test_region_and_all_capture_are_capability_unavailable(
        self,
    ) -> None:
        runner = FakeRunner(WindowsResponder())
        adapter = make_windows(runner)
        cases = [
            (UiCaptureRequest(target="region", region=UiRect(10, 20, 300, 200)),
             "region capture is not implemented"),
            (UiCaptureRequest(target="all"), "virtual-desktop capture is not implemented"),
        ]
        for request, message in cases:
            with self.subTest(target=request.target):
                with self.assertRaises(UiError) as context:
                    await adapter.capture(request)
                self.assertEqual(
                    context.exception.code, "capability_unavailable"
                )
                self.assertIn(message, str(context.exception))
        self.assertEqual(runner.calls, [])

    async def test_missing_capture_file_is_capture_failed(self) -> None:
        adapter = make_windows(
            FakeRunner(WindowsResponder(write_capture=False)),
            temp_dir=self.base,
        )
        with self.assertRaises(UiError) as context:
            await adapter.capture(UiCaptureRequest(target="display"))
        self.assertEqual(context.exception.code, "capture_failed")
        self.assertIn("did not write", str(context.exception))

    async def test_capture_size_mismatch_warns_instead_of_lying(self) -> None:
        adapter = make_windows(
            FakeRunner(WindowsResponder(capture_size=(1920, 1200))),
            temp_dir=self.base,
        )
        capture = await adapter.capture(UiCaptureRequest(target="display"))
        self.assertEqual((capture.pixel_width, capture.pixel_height), (1920, 1200))
        self.assertAlmostEqual(capture.dpi_scale or 0, 1.5)
        self.assertEqual(capture.physical_width, 2880)
        self.assertTrue(
            any("virtualizing" in warning for warning in capture.warnings)
        )

    async def test_jpeg_capture_sets_quality_and_reads_header(self) -> None:
        runner = FakeRunner(WindowsResponder())
        adapter = make_windows(runner, temp_dir=self.base)
        capture = await adapter.capture(
            UiCaptureRequest(target="display", format="jpeg", quality=77)
        )
        self.assertEqual(capture.format, "jpeg")
        self.assertEqual((capture.pixel_width, capture.pixel_height), (2880, 1800))
        script = runner.scripts("RelayWindowsCapture")[0]
        self.assertIn("EncoderParameters", script)
        self.assertIn("Quality, [long]77", script)


class PointerTests(unittest.IsolatedAsyncioTestCase):
    async def test_click_scales_logical_coordinates_and_verifies_cursor(
        self,
    ) -> None:
        responder = WindowsResponder(cursor=(150, 300))
        runner = FakeRunner(responder)
        result = await make_windows(runner).pointer(
            UiPointerRequest(action="click", x=100, y=200)
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.backend, "powershell+user32")
        self.assertEqual(result.action, "click")
        self.assertEqual(
            responder.last_pointer,
            {
                "action": "left_click",
                "x": 150,
                "y": 300,
                "count": 1,
                "scroll_x": 0,
                "scroll_y": 0,
            },
        )
        self.assertEqual(result.verification.method, "cursor")
        self.assertEqual(result.verification.cursor, UiPoint(100, 200))
        self.assertTrue(result.verification.ok)

    async def test_move_without_cursor_readback_reports_not_available(
        self,
    ) -> None:
        responder = WindowsResponder(cursor_available=False)
        result = await make_windows(FakeRunner(responder)).pointer(
            UiPointerRequest(action="move", x=100, y=200)
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.verification.method, "not_available")
        self.assertIsNone(result.verification.ok)

    async def test_cursor_mismatch_returns_not_ok(self) -> None:
        responder = WindowsResponder(cursor=(10, 10))
        result = await make_windows(FakeRunner(responder)).pointer(
            UiPointerRequest(action="click", x=100, y=200)
        )
        self.assertFalse(result.ok)
        self.assertFalse(result.verification.ok)
        self.assertIn("expected 100,200", result.verification.detail or "")

    async def test_uniform_multi_display_uses_global_scale(self) -> None:
        secondary = {
            "name": r"\\.\DISPLAY2",
            "primary": False,
            "x": 1920,
            "y": 0,
            "width": 1920,
            "height": 1080,
            "physical_width": 0,
            "physical_height": 0,
            "dpi": 144,
        }
        responder = WindowsResponder(
            displays=[DISPLAY_150, secondary], cursor=(3000, 150)
        )
        result = await make_windows(FakeRunner(responder)).pointer(
            UiPointerRequest(action="click", x=2000, y=100)
        )
        self.assertTrue(result.ok)
        assert responder.last_pointer is not None
        assert result.verification is not None
        self.assertEqual(responder.last_pointer["x"], 3000)
        self.assertEqual(responder.last_pointer["y"], 150)
        self.assertEqual(result.verification.cursor, UiPoint(2000, 100))
        self.assertEqual(result.warnings, [])

    async def test_scroll_uses_wheel_notches(self) -> None:
        responder = WindowsResponder(cursor=(150, 300))
        result = await make_windows(FakeRunner(responder)).pointer(
            UiPointerRequest(
                action="scroll", x=100, y=200, delta_y=2, delta_x=-1
            )
        )
        self.assertTrue(result.ok)
        self.assertEqual(responder.last_pointer["scroll_y"], 240)
        self.assertEqual(responder.last_pointer["scroll_x"], -120)
        self.assertEqual(responder.last_pointer["action"], "scroll")

    async def test_drag_is_capability_unavailable(self) -> None:
        runner = FakeRunner(WindowsResponder())
        with self.assertRaises(UiError) as context:
            await make_windows(runner).pointer(
                UiPointerRequest(
                    action="drag", x=100, y=100, to_x=200, to_y=200
                )
            )
        self.assertEqual(context.exception.code, "capability_unavailable")
        self.assertEqual(str(context.exception), DRAG_REASON)
        self.assertIn("legacy user32 input bridge", str(context.exception))
        self.assertEqual(runner.calls, [])

    async def test_off_layout_point_is_refused(self) -> None:
        runner = FakeRunner(WindowsResponder())
        with self.assertRaises(UiError) as context:
            await make_windows(runner).pointer(
                UiPointerRequest(action="click", x=5000, y=100)
            )
        self.assertEqual(context.exception.code, "invalid_request")
        self.assertIn("outside the desktop layout", str(context.exception))
        self.assertTrue(no_pointer_scripts(runner))

    async def test_unknown_action_is_invalid_request(self) -> None:
        runner = FakeRunner(WindowsResponder())
        with self.assertRaises(UiError) as context:
            await make_windows(runner).pointer(
                UiPointerRequest(action="teleport", x=10, y=10)
            )
        self.assertEqual(context.exception.code, "invalid_request")
        self.assertEqual(runner.calls, [])


class KeyboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_type_text_escapes_sendkeys_metacharacters(self) -> None:
        responder = WindowsResponder()
        result = await make_windows(FakeRunner(responder)).type_text(
            "hello + {world}"
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.backend, "powershell+sendkeys")
        self.assertEqual(result.action, "type")
        self.assertEqual(
            responder.last_keyboard["text"], "hello {+} {{}world{}}"
        )
        self.assertEqual(responder.last_keyboard["keys"], "")
        self.assertIsNone(responder.last_keyboard["expected_pid"])
        self.assertEqual(
            result.verification.active_window,
            {"id": "4242", "app": "notepad", "title": "Notes - Notepad"},
        )

    async def test_keys_normalizes_aliases_and_modifiers(self) -> None:
        responder = WindowsResponder()
        adapter = make_windows(FakeRunner(responder))
        await adapter.keys("Enter")
        self.assertEqual(responder.last_keyboard["keys"], "{ENTER}")
        await adapter.keys("ctrl+shift+s")
        self.assertEqual(responder.last_keyboard["keys"], "^+s")
        await adapter.keys("f5")
        self.assertEqual(responder.last_keyboard["keys"], "{F5}")

    async def test_typing_with_window_focuses_first_and_verifies(self) -> None:
        responder = WindowsResponder()
        runner = FakeRunner(responder)
        result = await make_windows(runner).type_text("hi", window_id="4242")
        self.assertTrue(result.ok)
        self.assertEqual(responder.last_keyboard["expected_pid"], 4242)
        self.assertEqual(len(runner.scripts("RelayWindowsFocus")), 1)

    async def test_typing_refuses_when_focus_does_not_move(self) -> None:
        responder = WindowsResponder(focused_pid=5151, focus_moves=False)
        runner = FakeRunner(responder)
        adapter = make_windows(runner)
        with self.assertRaises(UiError) as context:
            await adapter.type_text("secret", window_id="4242")
        self.assertEqual(context.exception.code, "input_failed")
        self.assertIn("Focus did not move", str(context.exception))
        self.assertIsNone(responder.last_keyboard)
        self.assertEqual(runner.scripts("RelayWindowsKeyboard"), [])

    async def test_typing_without_foreground_is_input_failed(self) -> None:
        responder = WindowsResponder(
            keyboard_response={
                "ok": False,
                "code": "no_foreground",
                "error": "No foreground window is available.",
            }
        )
        adapter = make_windows(FakeRunner(responder))
        with self.assertRaises(UiError) as context:
            await adapter.type_text("hello")
        self.assertEqual(context.exception.code, "input_failed")
        self.assertIn("No foreground window", str(context.exception))

    async def test_focus_lost_before_typing_is_input_failed(self) -> None:
        responder = WindowsResponder(
            keyboard_response={
                "ok": False,
                "code": "focus_lost",
                "foreground_pid": 5151,
                "error": "The foreground window changed before typing.",
            }
        )
        adapter = make_windows(FakeRunner(responder))
        with self.assertRaises(UiError) as context:
            await adapter.type_text("hello", window_id="4242")
        self.assertEqual(context.exception.code, "input_failed")
        self.assertIn("refusing to type into the wrong window", str(context.exception))

    async def test_unknown_key_is_invalid_request(self) -> None:
        runner = FakeRunner(WindowsResponder())
        with self.assertRaises(UiError) as context:
            await make_windows(runner).keys("hyper+x")
        self.assertEqual(context.exception.code, "invalid_request")
        self.assertEqual(runner.scripts("RelayWindowsKeyboard"), [])

    async def test_text_length_is_capped(self) -> None:
        runner = FakeRunner(WindowsResponder())
        with self.assertRaises(UiError) as context:
            await make_windows(runner).type_text("x" * 2_001)
        self.assertEqual(context.exception.code, "invalid_request")
        self.assertEqual(runner.calls, [])


class KeyNormalizationTests(unittest.TestCase):
    def test_ps_literal_quotes_paths_with_spaces_and_quotes(self) -> None:
        self.assertEqual(
            WindowsAdapter._ps_literal(
                "C:\\Users\\Test User\\Hermes 'Relay' \"Shots\"\\screen 1.png"
            ),
            "'C:\\Users\\Test User\\Hermes ''Relay'' \"Shots\"\\screen 1.png'",
        )

    def test_aliases(self) -> None:
        self.assertEqual(normalize_keys("Enter"), "{ENTER}")
        self.assertEqual(normalize_keys("return"), "{ENTER}")
        self.assertEqual(normalize_keys(" esc "), "{ESC}")
        self.assertEqual(normalize_keys("page down"), "{PGDN}")
        self.assertEqual(normalize_keys("ctrl+c"), "^c")

    def test_modifier_combinations(self) -> None:
        self.assertEqual(normalize_keys("ctrl+shift+s"), "^+s")
        self.assertEqual(normalize_keys("alt+f4"), "%{F4}")
        self.assertEqual(normalize_keys("shift+tab"), "+{TAB}")

    def test_rejects_unknown_names(self) -> None:
        self.assertIsNone(normalize_keys("hyper+x"))
        self.assertIsNone(normalize_keys("ctrl+hyper+x"))
        self.assertIsNone(normalize_keys(""))

    def test_escape_sendkeys_text(self) -> None:
        self.assertEqual(
            escape_send_keys_text("a+b{c}(d)[e]~%^"),
            "a{+}b{{}c{}}{(}d{)}{[}e{]}{~}{%}{^}",
        )


class SelectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        reset_platform_adapter_for_tests()

    async def test_sys_platform_win32_selects_windows_adapter(self) -> None:
        with patch.object(sys, "platform", "win32"):
            selection = await select_platform_adapter(env={}, refresh=True)
        self.assertEqual(selection.kind, "windows")
        self.assertIsInstance(selection.adapter, WindowsAdapter)

    async def test_win32_selects_windows_adapter(self) -> None:
        selection = await select_platform_adapter(
            platform="win32", env={}, refresh=True
        )
        self.assertEqual(selection.kind, "windows")
        self.assertIsInstance(selection.adapter, WindowsAdapter)
        self.assertEqual(selection.reason, "win32 platform")

    async def test_explicit_windows_override_on_linux_is_honest(self) -> None:
        selection = await select_platform_adapter(
            platform="linux",
            env={"HERMES_RELAY_DESKTOP_BACKEND": "windows"},
            refresh=True,
            which=lambda name: None,
        )
        self.assertEqual(selection.kind, "windows")
        environment = await selection.adapter.environment()
        self.assertFalse(environment.graphical)
        self.assertIn("requires win32", environment.reason or "")

    async def test_kind_override_selects_windows(self) -> None:
        selection = await select_platform_adapter(
            kind="windows", platform="linux", env={}, refresh=True
        )
        self.assertEqual(selection.kind, "windows")
        self.assertEqual(selection.reason, "explicit Windows selection")

    async def test_linux_display_still_selects_x11(self) -> None:
        selection = await select_platform_adapter(
            platform="linux", env={"DISPLAY": ":0"}, refresh=True
        )
        self.assertEqual(selection.kind, "x11")

    async def test_no_session_is_still_unsupported(self) -> None:
        selection = await select_platform_adapter(
            platform="linux", env={}, refresh=True
        )
        self.assertEqual(selection.kind, "unsupported")
        self.assertIsInstance(selection.adapter, UnsupportedAdapter)


class HandlerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="hermes-relay-windows-handler-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.ctx = ToolContext(cwd=self.base)
        responder = WindowsResponder()
        self.runner = FakeRunner(responder)
        self.adapter = make_windows(self.runner, temp_dir=self.base)
        selection = AdapterSelection(self.adapter, "windows", "test selection")

        async def fake_select(**_: Any) -> AdapterSelection:
            return selection

        patcher = patch.object(
            desktop_ui_handler, "select_platform_adapter", fake_select
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.registry = build_registry(state=ClientState())

    async def test_status_wire_shape_carries_dpi_and_physical_metadata(
        self,
    ) -> None:
        result = await self.registry.invoke("desktop_ui_status", {}, self.ctx)
        self.assertTrue(result["ok"])
        self.assertEqual(result["adapter"], "windows")
        display = result["displays"][0]
        self.assertEqual(display["rect"], {"x": 0, "y": 0, "width": 1920, "height": 1200})
        self.assertEqual(display["physical_width"], 2880)
        self.assertEqual(display["physical_height"], 1800)
        self.assertEqual(display["dpi_scale"], 1.5)
        capabilities = {
            item["id"]: item for item in result["capabilities"]
        }
        self.assertTrue(capabilities["screen.capture"]["available"])
        self.assertFalse(capabilities["pointer.drag"]["available"])
        self.assertIn("legacy user32", capabilities["pointer.drag"]["reason"])

    async def test_screenshot_returns_capture_metadata(self) -> None:
        result = await self.registry.invoke(
            "desktop_ui_screenshot", {"target": "display"}, self.ctx
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["pixel_width"], 2880)
        self.assertEqual(result["pixel_height"], 1800)
        self.assertEqual(result["origin"], {"x": 0, "y": 0})
        self.assertEqual(result["scale"], 1.5)
        self.assertEqual(result["dpi_scale"], 1.5)
        self.assertEqual(result["physical_width"], 2880)
        self.assertTrue(result["bytes_base64"].startswith("iVBORw0KGgo"))

    async def test_screenshot_window_capture_is_refused(self) -> None:
        result = await self.registry.invoke(
            "desktop_ui_screenshot",
            {"target": "window", "window_id": "4242"},
            self.ctx,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "capability_unavailable")
        self.assertIn("window capture is not implemented", result["message"])

    async def test_focus_and_drag_through_the_tool_handlers(self) -> None:
        focused = await self.registry.invoke(
            "desktop_ui_windows", {"focus": "4242"}, self.ctx
        )
        self.assertTrue(focused["ok"])
        self.assertEqual(focused["action"], "focus_window")

        dragged = await self.registry.invoke(
            "desktop_ui_action",
            {"action": "drag", "x": 10, "y": 10, "to_x": 20, "to_y": 20},
            self.ctx,
        )
        self.assertFalse(dragged["ok"])
        self.assertEqual(dragged["code"], "capability_unavailable")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
