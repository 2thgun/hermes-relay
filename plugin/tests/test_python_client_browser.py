"""Phase-3 browser/CDP tests against a fake CDP server.

Covers the structured surface end to end at the handler boundary:

* inspect refs + rects, max-element clamping, include_text
* click by ref/selector/coordinates, ref expiry after navigation
* type/clear/submit/value modes, key combos, scroll wheel
* screenshots (png/jpeg, full page, save_to, size guard)
* upload through DOM.setFileInputFiles, download settling + timeout
* wait for selector/text and timeout
* open/close, URL-scheme restriction, dedicated-profile status shape

The fake server is a real loopback aiohttp app, so CDP request ids, timeouts,
event dispatch and the aiohttp websocket client are all exercised.  No
third-party network access and no real browser are required.
"""

from __future__ import annotations

import asyncio
import base64
import tempfile
import unittest
from pathlib import Path
from typing import Any

from plugin.relay.device.browser.cdp import CdpError, open_cdp
from plugin.relay.device.browser.controller import (
    BrowserController,
    BrowserError,
    ensure_allowed_url,
)
from plugin.relay.device.dispatch import (
    ClientState,
    DesktopDispatcher,
    ToolContext,
    ToolRegistry,
)
from plugin.relay.device.handlers import browser as browser_handler
from plugin.relay.device.handlers import build_registry
from plugin.tests._fake_browser import TINY_PNG, FakeBrowserLauncher, FakeCdpServer

BUTTON = {
    "tag": "button",
    "role": "button",
    "name": "Sign in",
    "disabled": False,
    "rect": {"x": 10, "y": 20, "width": 100, "height": 30},
}
EMAIL = {
    "tag": "input",
    "type": "email",
    "name": "Email",
    "value": "",
    "disabled": False,
    "checked": False,
    "rect": {"x": 10, "y": 70, "width": 200, "height": 32},
}
FILE_INPUT = {
    "tag": "input",
    "type": "file",
    "name": "Attachment",
    "disabled": False,
    "rect": {"x": 10, "y": 120, "width": 200, "height": 32},
}


class BrowserHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.server = FakeCdpServer()
        await self.server.start()
        self.addAsyncCleanup(self.server.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.launcher = FakeBrowserLauncher(self.server, Path(self.tmp.name))
        self.controller = BrowserController(
            self.launcher,  # type: ignore[arg-type]
            timeout_ms=5_000,
            wait_poll_s=0.01,
            download_poll_s=0.02,
            download_stable_ms=10,
        )
        self.addAsyncCleanup(self.controller.close)
        browser_handler.set_browser_controller_for_tests(self.controller)
        self.addCleanup(browser_handler.reset_browser_controller_for_tests)
        self.ctx = ToolContext(cwd=self.tmp.name)

    async def inspect(self, **args: Any) -> dict[str, Any]:
        return await browser_handler.desktop_browser_inspect(args, self.ctx)

    async def click(self, **args: Any) -> dict[str, Any]:
        return await browser_handler.desktop_browser_click(args, self.ctx)


class StatusAndOpenTests(BrowserHarness):
    async def test_status_reports_running_browser_and_pages(self) -> None:
        result = await browser_handler.desktop_browser_status({}, self.ctx)
        self.assertTrue(result["ok"])
        self.assertTrue(result["available"])
        self.assertTrue(result["running"])
        self.assertEqual(result["binary_path"], "/usr/bin/chromium")
        self.assertEqual(result["profile_dir"], self.launcher.profile_dir)
        self.assertEqual(result["endpoint"], self.server.endpoint)
        self.assertEqual(result["web_socket_url"], self.server.browser_ws)
        self.assertEqual(result["browser_version"], "FakeChrome/1.0")
        self.assertEqual(
            result["pages"], [{"id": "page-1", "url": "about:blank", "title": "Fake page"}]
        )
        self.assertNotIn("note", result)

    async def test_status_notes_unavailable_browser(self) -> None:
        class Unavailable:
            async def status(self) -> dict[str, Any]:
                return {
                    "available": False,
                    "running": False,
                    "profile_dir": "/tmp/profile",
                    "reason": "No Chrome/Chromium/Edge/Brave binary found.",
                }

        browser_handler.set_browser_controller_for_tests(Unavailable())  # type: ignore[arg-type]
        result = await browser_handler.desktop_browser_status({}, self.ctx)
        self.assertTrue(result["ok"])
        self.assertFalse(result["available"])
        self.assertEqual(result["note"], "Browser automation is unavailable on this computer.")

    async def test_open_navigates_and_marks_dedicated_profile(self) -> None:
        self.server.titles["https://example.test/start"] = "Start"
        result = await browser_handler.desktop_browser_open(
            {"url": "https://example.test/start"}, self.ctx
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["detail"], "navigated")
        self.assertEqual(result["url"], "https://example.test/start")
        self.assertEqual(result["title"], "Start")
        self.assertEqual(result["profile"], "dedicated automation profile")
        self.assertEqual(self.server.url, "https://example.test/start")

    async def test_open_forwards_headless_to_the_launcher(self) -> None:
        await browser_handler.desktop_browser_open(
            {"url": "https://example.test/", "headless": True}, self.ctx
        )
        self.assertIn(True, self.launcher.ensure_calls)

    async def test_open_allows_about_blank(self) -> None:
        result = await browser_handler.desktop_browser_open({"url": "about:blank"}, self.ctx)
        self.assertTrue(result["ok"])
        self.assertEqual(result["url"], "about:blank")

    async def test_open_rejects_unsupported_schemes(self) -> None:
        for url in (
            "file:///etc/passwd",
            "javascript:alert(1)",
            "chrome://settings",
            "data:text/html,hi",
            "ftp://example.test/",
        ):
            result = await browser_handler.desktop_browser_open({"url": url}, self.ctx)
            self.assertFalse(result["ok"], url)
            self.assertEqual(result["code"], "invalid_request")
            self.assertEqual(result["message"], "Only http(s) URLs (or about:blank) are allowed.")
        self.assertEqual(self.server.calls, [])

    async def test_open_requires_url(self) -> None:
        result = await browser_handler.desktop_browser_open({"url": "   "}, self.ctx)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "invalid_request")
        self.assertEqual(result["message"], "desktop_browser_open requires a url.")

    async def test_open_reports_navigation_error(self) -> None:
        self.server.navigate_error = "net::ERR_NAME_NOT_RESOLVED"
        result = await browser_handler.desktop_browser_open(
            {"url": "https://missing.invalid/"}, self.ctx
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "browser_error")
        self.assertIn("Navigation failed: net::ERR_NAME_NOT_RESOLVED", result["message"])

    async def test_controller_rejects_scheme_without_touching_browser(self) -> None:
        with self.assertRaisesRegex(BrowserError, "Only http"):
            await self.controller.open("file:///tmp/index.html")
        with self.assertRaisesRegex(BrowserError, "url is required"):
            await self.controller.open("")
        self.assertEqual(self.server.connections, [])

    def test_url_helper_accepts_http_https_and_about_blank(self) -> None:
        self.assertEqual(ensure_allowed_url(" https://example.test/a "), "https://example.test/a")
        self.assertEqual(ensure_allowed_url("HTTP://example.test/"), "HTTP://example.test/")
        self.assertEqual(ensure_allowed_url("ABOUT:BLANK"), "ABOUT:BLANK")
        with self.assertRaises(BrowserError):
            ensure_allowed_url("about:config")


class InspectTests(BrowserHarness):
    async def test_inspect_returns_refs_rects_and_text(self) -> None:
        self.server.inspect_elements = [BUTTON, EMAIL]
        result = await self.inspect(max_elements=10, include_text=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["url"], "about:blank")
        self.assertEqual(result["title"], "Fake page")
        self.assertFalse(result["truncated"])
        self.assertEqual(result["text"], "hello from the fake page")
        self.assertEqual(len(result["elements"]), 2)
        button, email = result["elements"]
        self.assertRegex(button["ref"], r"^h[0-9a-f]+0-1$")
        self.assertEqual(button["tag"], "button")
        self.assertEqual(button["role"], "button")
        self.assertEqual(button["name"], "Sign in")
        self.assertFalse(button["disabled"])
        self.assertEqual(
            button["rect"], {"x": 10, "y": 20, "width": 100, "height": 30}
        )
        self.assertEqual(email["type"], "email")
        self.assertEqual(email["value"], "")
        self.assertTrue(email["ref"].endswith("-2"))
        # Re-inspection uses a new prefix, so old refs never silently resolve.
        second = await self.inspect(max_elements=10)
        self.assertNotEqual(second["elements"][0]["ref"], button["ref"])

    async def test_inspect_clamps_max_elements_to_200(self) -> None:
        self.server.inspect_elements = [
            {**BUTTON, "name": f"Button {index}"} for index in range(205)
        ]
        result = await self.inspect(max_elements=500)
        self.assertEqual(len(result["elements"]), 200)
        self.assertTrue(result["truncated"])

    async def test_inspect_rejects_zero_and_uses_one(self) -> None:
        self.server.inspect_elements = [BUTTON, EMAIL]
        result = await self.inspect(max_elements=0)
        self.assertEqual(len(result["elements"]), 1)
        self.assertTrue(result["truncated"])


class ClickTests(BrowserHarness):
    async def test_click_by_ref_dispatches_center_click(self) -> None:
        self.server.inspect_elements = [BUTTON]
        inspected = await self.inspect()
        ref = inspected["elements"][0]["ref"]
        result = await self.click(ref=ref)
        self.assertTrue(result["ok"])
        self.assertEqual(result["detail"], "clicked left")
        self.assertEqual((result["x"], result["y"]), (40, 60))
        self.assertEqual(result["click_count"], 1)
        self.assertEqual(result["url"], "about:blank")
        types = [event["type"] for event in self.server.mouse_events]
        self.assertEqual(types, ["mouseMoved", "mousePressed", "mouseReleased"])
        pressed = self.server.mouse_events[1]
        self.assertEqual(pressed["button"], "left")
        self.assertEqual(pressed["clickCount"], 1)
        self.assertEqual(pressed["buttons"], 1)
        self.assertEqual(self.server.mouse_events[2]["buttons"], 0)

    async def test_click_by_selector_right_button_double_click(self) -> None:
        self.server.selector_points["#menu"] = (120, 140)
        result = await self.click(selector="#menu", button="right", count=2)
        self.assertTrue(result["ok"])
        self.assertEqual(result["detail"], "clicked right")
        self.assertEqual(result["x"], 120)
        self.assertEqual(result["click_count"], 2)
        pressed = [event for event in self.server.mouse_events if event["type"] == "mousePressed"]
        self.assertEqual([event["clickCount"] for event in pressed], [1, 2])
        self.assertTrue(all(event["button"] == "right" for event in pressed))
        self.assertTrue(all(event["buttons"] == 2 for event in pressed))

    async def test_click_by_coordinates_skips_dom_lookup(self) -> None:
        result = await self.click(x=11.6, y=22.4)
        self.assertTrue(result["ok"])
        self.assertEqual((result["x"], result["y"]), (12, 22))
        self.assertFalse(any("querySelector" in str(call[2]) for call in self.server.calls))

    async def test_click_requires_a_target(self) -> None:
        result = await self.click()
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "invalid_request")
        self.assertEqual(result["message"], "Provide ref, selector, or x and y.")

    async def test_refs_expire_after_navigation(self) -> None:
        self.server.inspect_elements = [BUTTON]
        inspected = await self.inspect()
        ref = inspected["elements"][0]["ref"]
        first = await self.click(ref=ref)
        self.assertTrue(first["ok"], first)
        await browser_handler.desktop_browser_open(
            {"url": "https://example.test/next"}, self.ctx
        )
        result = await self.click(ref=ref)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "browser_error")
        self.assertIn("references expire after navigation", result["message"])
        self.assertIn('[data-hermes-ref="' + ref + '"]', result["message"])

    async def test_unknown_selector_returns_actionable_error(self) -> None:
        result = await self.click(selector="#does-not-exist")
        self.assertFalse(result["ok"])
        self.assertIn("No element matches #does-not-exist.", result["message"])
        self.assertIn("desktop_browser_inspect", result["message"])

    async def test_click_on_invisible_element(self) -> None:
        self.server.invisible_selectors.add("#hidden")
        result = await self.click(selector="#hidden")
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "Element is not visible.")

    async def test_click_rejects_invalid_ref_characters(self) -> None:
        result = await self.click(ref='x"];alert(1)//')
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "Invalid element reference.")


class TypeAndKeyTests(BrowserHarness):
    async def test_type_focuses_clears_types_and_submits(self) -> None:
        self.server.inspect_elements = [EMAIL]
        inspected = await self.inspect()
        ref = inspected["elements"][0]["ref"]
        result = await browser_handler.desktop_browser_type(
            {"ref": ref, "text": "agent@example.test", "clear": True, "submit": True},
            self.ctx,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["detail"], "typed")
        self.assertEqual(result["characters"], len("agent@example.test"))
        self.assertEqual(self.server.inserted_texts, ["agent@example.test"])
        keys = self.server.key_events
        self.assertEqual(keys[0]["type"], "keyDown")
        self.assertEqual(keys[0]["key"], "Enter")
        self.assertEqual(keys[1]["type"], "keyUp")

    async def test_type_value_mode_uses_native_setter(self) -> None:
        self.server.selector_points["#email"] = (110, 86)
        result = await browser_handler.desktop_browser_type(
            {"selector": "#email", "text": "a@b.test", "mode": "value"}, self.ctx
        )
        self.assertTrue(result["ok"])
        self.assertEqual(self.server.set_value_text, "a@b.test")
        self.assertEqual(self.server.inserted_texts, [])

    async def test_type_value_mode_requires_selector(self) -> None:
        result = await browser_handler.desktop_browser_type(
            {"text": "orphan", "mode": "value"}, self.ctx
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "mode=value requires ref or selector.")

    async def test_type_rejects_oversized_text(self) -> None:
        result = await browser_handler.desktop_browser_type(
            {"text": "x" * 4_001}, self.ctx
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "text is limited to 4000 characters per call.")

    async def test_type_requires_text(self) -> None:
        result = await browser_handler.desktop_browser_type({"text": ""}, self.ctx)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "invalid_request")
        self.assertEqual(result["message"], "desktop_browser_type requires text.")

    async def test_key_combo_modifiers(self) -> None:
        result = await browser_handler.desktop_browser_key(
            {"keys": "ctrl+shift+t"}, self.ctx
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["detail"], "pressed ctrl+shift+t")
        down, up = self.server.key_events
        self.assertEqual(down["type"], "rawKeyDown")
        self.assertEqual(down["modifiers"], 10)
        self.assertEqual(down["key"], "t")
        self.assertEqual(down["code"], "KeyT")
        self.assertEqual(down["windowsVirtualKeyCode"], ord("T"))
        self.assertEqual(up["type"], "keyUp")

    async def test_plain_letter_key_emits_text(self) -> None:
        await browser_handler.desktop_browser_key({"keys": "a"}, self.ctx)
        down = self.server.key_events[0]
        self.assertEqual(down["type"], "keyDown")
        self.assertEqual(down["text"], "a")
        self.assertEqual(down["unmodifiedText"], "a")

    async def test_named_and_function_keys(self) -> None:
        for combo in ("escape", "pagedown", "f5"):
            self.server.key_events.clear()
            result = await browser_handler.desktop_browser_key({"keys": combo}, self.ctx)
            self.assertTrue(result["ok"], combo)
            down = self.server.key_events[0]
            self.assertIn("windowsVirtualKeyCode", down)

    async def test_key_rejects_unknown_name(self) -> None:
        result = await browser_handler.desktop_browser_key({"keys": "hyper"}, self.ctx)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "browser_error")
        self.assertIn("Unsupported key hyper.", result["message"])

    async def test_key_requires_keys(self) -> None:
        result = await browser_handler.desktop_browser_key({}, self.ctx)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "invalid_request")


class ScrollScreenshotTests(BrowserHarness):
    async def test_scroll_uses_viewport_center(self) -> None:
        result = await browser_handler.desktop_browser_scroll({"delta_y": 300}, self.ctx)
        self.assertTrue(result["ok"])
        self.assertEqual(result["detail"], "scrolled")
        wheel = self.server.mouse_events[0]
        self.assertEqual(wheel["type"], "mouseWheel")
        self.assertEqual((wheel["x"], wheel["y"]), (400, 300))
        self.assertEqual(wheel["deltaY"], 300)
        self.assertEqual(wheel["deltaX"], 0)

    async def test_scroll_at_explicit_point(self) -> None:
        await browser_handler.desktop_browser_scroll(
            {"x": 10, "y": 20, "delta_x": -50, "scroll_y": 80}, self.ctx
        )
        wheel = self.server.mouse_events[0]
        self.assertEqual((wheel["x"], wheel["y"]), (10, 20))
        self.assertEqual(wheel["deltaX"], -50)
        self.assertEqual(wheel["deltaY"], 80)

    async def test_scroll_requires_delta(self) -> None:
        result = await browser_handler.desktop_browser_scroll({}, self.ctx)
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "scroll needs delta_x or delta_y.")

    async def test_screenshot_png_metadata(self) -> None:
        result = await browser_handler.desktop_browser_screenshot(
            {"format": "png"}, self.ctx
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["format"], "png")
        self.assertEqual(result["size_bytes"], len(TINY_PNG))
        self.assertEqual(result["pixel_width"], 1)
        self.assertEqual(result["pixel_height"], 1)
        self.assertFalse(result["full_page"])
        self.assertEqual(base64.b64decode(result["bytes_base64"]), TINY_PNG)
        self.assertEqual(self.server.screenshot_params[0]["format"], "png")
        self.assertNotIn("quality", self.server.screenshot_params[0])

    async def test_screenshot_jpeg_clamps_quality(self) -> None:
        result = await browser_handler.desktop_browser_screenshot(
            {"format": "jpeg", "quality": 500}, self.ctx
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["format"], "jpeg")
        self.assertEqual(self.server.screenshot_params[0]["quality"], 100)
        self.assertIsNone(result["pixel_width"])  # PNG bytes are not valid JPEG

    async def test_screenshot_full_page_clips_content_size(self) -> None:
        await browser_handler.desktop_browser_screenshot(
            {"format": "png", "full_page": True}, self.ctx
        )
        params = self.server.screenshot_params[0]
        self.assertTrue(params["captureBeyondViewport"])
        self.assertEqual(
            params["clip"], {"x": 0, "y": 0, "width": 1200, "height": 1600, "scale": 1}
        )

    async def test_screenshot_save_to_writes_file_and_drops_base64(self) -> None:
        target = Path(self.tmp.name) / "shot.png"
        result = await browser_handler.desktop_browser_screenshot(
            {"format": "png", "save_to": str(target)}, self.ctx
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["saved_path"], str(target))
        self.assertNotIn("bytes_base64", result)
        self.assertEqual(target.read_bytes(), TINY_PNG)

    async def test_screenshot_save_to_relative_uses_cwd(self) -> None:
        result = await browser_handler.desktop_browser_screenshot(
            {"format": "png", "save_to": "relative.png"}, self.ctx
        )
        self.assertEqual(result["saved_path"], str(Path(self.tmp.name) / "relative.png"))
        self.assertTrue((Path(self.tmp.name) / "relative.png").is_file())

    async def test_screenshot_over_budget_fails_with_dimensions(self) -> None:
        class Oversized:
            async def screenshot(self, **kwargs: Any) -> dict[str, Any]:
                return {
                    "ok": True,
                    "format": "png",
                    "bytes_base64": "A" * (browser_handler.MAX_SCREENSHOT_BASE64 + 4),
                    "size_bytes": 1,
                    "pixel_width": 3840,
                    "pixel_height": 2160,
                    "full_page": True,
                }

        browser_handler.set_browser_controller_for_tests(Oversized())  # type: ignore[arg-type]
        result = await browser_handler.desktop_browser_screenshot(
            {"format": "png"}, self.ctx
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "screenshot_too_large")
        self.assertEqual(result["pixel_width"], 3840)
        self.assertEqual(result["pixel_height"], 2160)
        self.assertIn("over the relay response budget", result["message"])


class UploadDownloadTests(BrowserHarness):
    async def test_upload_sets_files_on_input(self) -> None:
        upload = Path(self.tmp.name) / "report.txt"
        upload.write_text("payload", encoding="utf-8")
        result = await browser_handler.desktop_browser_upload(
            {"selector": "#file", "files": [str(upload)]}, self.ctx
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["detail"], "uploaded")
        self.assertEqual(self.server.uploaded_files, [str(upload)])

    async def test_upload_requires_absolute_paths(self) -> None:
        result = await browser_handler.desktop_browser_upload(
            {"selector": "#file", "files": ["relative.txt"]}, self.ctx
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "files must contain 1..10 absolute paths.")

    async def test_upload_requires_files(self) -> None:
        result = await browser_handler.desktop_browser_upload(
            {"selector": "#file", "files": []}, self.ctx
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "invalid_request")

    async def test_upload_unknown_file_input(self) -> None:
        result = await browser_handler.desktop_browser_upload(
            {"selector": "#nope", "files": [str(Path(self.tmp.name) / "x")]}, self.ctx
        )
        self.assertFalse(result["ok"])
        self.assertIn("No element matches #nope.", result["message"])

    async def test_download_returns_settled_file(self) -> None:
        download_path = str(Path(self.tmp.name) / "file.bin")
        self.server.selector_points["#dl"] = (10, 10)

        def finish_download() -> None:
            self.launcher.download = {
                "path": download_path,
                "size": 1234,
                "modified_ms": 1_700_000_000_000,
            }

        self.server.on_click = finish_download
        result = await browser_handler.desktop_browser_download(
            {"selector": "#dl", "timeout_ms": 2_000}, self.ctx
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            result["download"],
            {"path": download_path, "size": 1234, "modified_ms": 1_700_000_000_000},
        )
        self.assertEqual(result["download_dir"], self.launcher.download_dir)
        self.assertEqual(self.server.download_behavior[0]["behavior"], "allow")
        self.assertEqual(self.server.download_behavior[0]["downloadPath"], self.launcher.download_dir)

    async def test_download_timeout_reports_directory_and_budget(self) -> None:
        self.server.selector_points["#dl"] = (10, 10)
        result = await browser_handler.desktop_browser_download(
            {"selector": "#dl", "timeout_ms": 1_000}, self.ctx
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["detail"], "No completed download appeared before the timeout.")
        self.assertEqual(result["download_dir"], self.launcher.download_dir)
        self.assertEqual(result["timeout_ms"], 1_000)


class WaitTests(BrowserHarness):
    async def test_wait_for_selector(self) -> None:
        self.server.wait_selector_default = True
        result = await browser_handler.desktop_browser_wait(
            {"selector": ".ready", "timeout_ms": 1_000}, self.ctx
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["detail"], "condition met")
        self.assertEqual(result["url"], "about:blank")

    async def test_wait_for_text(self) -> None:
        self.server.wait_text_default = True
        result = await browser_handler.desktop_browser_wait(
            {"text": "Signed in", "timeout_ms": 1_000}, self.ctx
        )
        self.assertTrue(result["ok"])

    async def test_wait_retries_until_condition_appears(self) -> None:
        self.server.wait_selector_sequence = [False, False, True]
        result = await browser_handler.desktop_browser_wait(
            {"selector": ".ready", "timeout_ms": 1_000}, self.ctx
        )
        self.assertTrue(result["ok"])

    async def test_wait_times_out_with_detail(self) -> None:
        result = await browser_handler.desktop_browser_wait(
            {"selector": ".never", "timeout_ms": 250}, self.ctx
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["detail"], "Condition not met within 250ms.")

    async def test_wait_requires_a_condition(self) -> None:
        result = await browser_handler.desktop_browser_wait({}, self.ctx)
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "wait_for requires selector or text.")


class CloseAndRegistryTests(BrowserHarness):
    async def test_close_page_only_keeps_browser(self) -> None:
        await self.inspect()
        result = await browser_handler.desktop_browser_close({"page_only": True}, self.ctx)
        self.assertTrue(result["ok"])
        self.assertEqual(
            result["detail"], "Closed the automation page; the browser stays running."
        )
        self.assertFalse(self.server.closed)

    async def test_close_browser(self) -> None:
        await self.inspect()
        result = await browser_handler.desktop_browser_close({}, self.ctx)
        self.assertTrue(result["ok"])
        self.assertEqual(result["detail"], "Closed the automation browser.")
        self.assertTrue(self.server.closed)
        self.assertTrue(self.launcher.closed)

    async def test_registry_advertises_all_browser_tools(self) -> None:
        registry = build_registry(state=ClientState())
        expected = {
            "desktop_browser_status",
            "desktop_browser_open",
            "desktop_browser_inspect",
            "desktop_browser_click",
            "desktop_browser_type",
            "desktop_browser_key",
            "desktop_browser_scroll",
            "desktop_browser_screenshot",
            "desktop_browser_upload",
            "desktop_browser_download",
            "desktop_browser_wait",
            "desktop_browser_close",
        }
        self.assertEqual(expected, {name for name in registry.names() if name.startswith("desktop_browser_")})

    async def test_registry_invokes_browser_tool_through_dispatch(self) -> None:
        registry = build_registry(state=ClientState())
        result = await registry.invoke("desktop_browser_status", {}, self.ctx)
        self.assertTrue(result["ok"])
        self.assertEqual(result["pages"][0]["id"], "page-1")


class DispatcherTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_tools_get_a_longer_dispatch_budget(self) -> None:
        self.assertEqual(
            browser_handler.timeouts()["desktop_browser_download"],
            browser_handler.TOOL_TIMEOUT_S,
        )
        self.assertEqual(
            set(browser_handler.timeouts()), set(browser_handler.handlers())
        )

    async def test_per_tool_timeout_overrides_the_default(self) -> None:
        registry = ToolRegistry()

        async def slow(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
            await asyncio.sleep(5)
            return {"ok": True}

        registry.add("desktop_browser_wait", slow)
        sent: list[dict[str, Any]] = []

        async def send(payload: dict[str, Any]) -> None:
            sent.append(payload)

        dispatcher = DesktopDispatcher(
            registry,
            send,
            timeout=5.0,
            tool_timeouts={"desktop_browser_wait": 0.05},
        )
        await dispatcher.handle_envelope(
            {
                "channel": "desktop",
                "type": "desktop.command",
                "payload": {
                    "request_id": "r-timeout",
                    "tool": "desktop_browser_wait",
                    "args": {},
                },
            }
        )
        for _ in range(200):
            if sent:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(sent)
        self.assertFalse(sent[0]["ok"])
        self.assertIn("timed out after 50ms", sent[0]["error"])


class CdpClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_error_surfaces_as_cdp_error(self) -> None:
        server = FakeCdpServer()
        await server.start()
        self.addAsyncCleanup(server.stop)
        connection = await open_cdp(server.page_ws)
        self.addAsyncCleanup(connection.close)
        with self.assertRaises(CdpError) as raised:
            await connection.send("Bogus.method")
        self.assertIn("Bogus.method: unknown method Bogus.method", str(raised.exception))
        self.assertEqual(raised.exception.code, -32601)

    async def test_commands_share_increasing_ids(self) -> None:
        server = FakeCdpServer()
        await server.start()
        self.addAsyncCleanup(server.stop)
        connection = await open_cdp(server.page_ws)
        self.addAsyncCleanup(connection.close)
        await connection.send("Page.enable")
        await connection.send("Runtime.enable")
        self.assertEqual([call[1] for call in server.calls], ["Page.enable", "Runtime.enable"])

    async def test_wait_for_event_times_out(self) -> None:
        server = FakeCdpServer()
        await server.start()
        self.addAsyncCleanup(server.stop)
        connection = await open_cdp(server.page_ws)
        self.addAsyncCleanup(connection.close)
        with self.assertRaises(CdpError) as raised:
            await connection.wait_for_event("Page.loadEventFired", 50)
        self.assertIn("Timed out waiting for Page.loadEventFired", str(raised.exception))


class FakeCdpServerSelfTests(unittest.IsolatedAsyncioTestCase):
    async def test_fake_server_speaks_version_and_list(self) -> None:
        server = FakeCdpServer()
        await server.start()
        self.addAsyncCleanup(server.stop)
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.get(f"{server.endpoint}/json/version") as response:
                version = await response.json()
            async with session.get(f"{server.endpoint}/json/list") as response:
                targets = await response.json()
        self.assertEqual(version["Browser"], "FakeChrome/1.0")
        self.assertEqual(version["webSocketDebuggerUrl"], server.browser_ws)
        self.assertEqual(targets[0]["id"], "page-1")


if __name__ == "__main__":
    unittest.main()
