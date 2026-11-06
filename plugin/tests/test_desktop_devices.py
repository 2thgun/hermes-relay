"""Device directory, offline errors, and target pinning.

Stdlib ``unittest`` only so this runs without the plugin's optional test deps:

    python -m unittest plugin.tests.test_desktop_devices
"""

from __future__ import annotations

import asyncio
import time
import unittest

from plugin.relay.channels.desktop import (
    DesktopError,
    DesktopHandler,
    DesktopRequesterContext,
    DesktopSession,
)


class FakeWebSocket:
    def __init__(self) -> None:
        self.closed = False


def make_session(
    device_id: str,
    name: str,
    *,
    platform: str = "linux",
    adapter: str = "linux-hyprland",
    session: str = "wayland",
    graphical: bool = True,
    capabilities: list[dict] | None = None,
    browser: dict | None = None,
    tools: set[str] | None = None,
) -> DesktopSession:
    session_obj = DesktopSession()
    session_obj.authenticated_device_id = device_id
    session_obj.device_id = device_id
    session_obj.device_name = name
    session_obj.last_seen_at = time.time()
    session_obj.advertised_tools = tools or {"desktop_read_file", "desktop_ui_action"}
    session_obj.client_status = {
        "device_id": device_id,
        "device_name": name,
        "platform": platform,
        "capabilities": {
            "adapter": adapter,
            "platform": platform,
            "session": session,
            "graphical": graphical,
            "capabilities": capabilities
            if capabilities is not None
            else [
                {"id": "screen.capture", "available": True, "backend": "grim"},
                {"id": "pointer.click", "available": True, "backend": "wl-input"},
                {"id": "pointer.drag", "available": False, "reason": "drag needs the wl-input helper"},
            ],
            "browser": browser or {"available": True, "running": False, "profile_dir": "/home/u/.hermes/browser-profile"},
        },
    }
    return session_obj


class DeviceDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.handler = DesktopHandler()
        self.ws = FakeWebSocket()

    def attach(self, session: DesktopSession) -> None:
        self.handler._sessions[self.ws] = session
        self.handler._observe_device(session)

    def test_snapshot_summarises_capabilities_and_browser(self) -> None:
        self.attach(make_session("dev-a", "Studio-PC", platform="win32", adapter="windows", session="windows"))
        snapshot = self.handler.devices_snapshot()
        self.assertTrue(snapshot["connected"])
        row = snapshot["devices"][0]
        self.assertEqual(row["device_id"], "dev-a")
        self.assertEqual(row["name"], "Studio-PC")
        self.assertEqual(row["platform"], "win32")
        self.assertEqual(row["adapter"], "windows")
        self.assertIn("screen.capture", row["capabilities"]["available"])
        self.assertEqual(
            row["capabilities"]["unavailable"][0]["reason"],
            "drag needs the wl-input helper",
        )
        self.assertTrue(row["capabilities"]["browser"]["available"])

    def test_offline_device_is_remembered_but_not_dispatchable(self) -> None:
        session = make_session("dev-a", "Laptop")
        self.attach(session)
        self.handler._mark_offline(session)
        self.handler._sessions.clear()

        snapshot = self.handler.devices_snapshot()
        self.assertFalse(snapshot["connected"])
        self.assertEqual(snapshot["online_count"], 0)
        self.assertEqual(snapshot["devices"][0]["device_id"], "dev-a")
        self.assertFalse(snapshot["devices"][0]["online"])
        self.assertIn("last_seen_age_s", snapshot["devices"][0])

        with self.assertRaises(DesktopError) as caught:
            self.handler._resolve_target(None, device_id="dev-a")
        message = str(caught.exception)
        self.assertIn("offline", message)
        self.assertIn("Laptop", message)

    def test_offline_selector_hint_and_no_silent_substitution(self) -> None:
        offline = make_session("dev-a", "Laptop")
        self.attach(offline)
        self.handler._mark_offline(offline)
        self.handler._sessions.clear()

        online = make_session("dev-b", "Studio")
        self.attach(online)

        with self.assertRaises(DesktopError) as caught:
            self.handler._resolve_target("Laptop")
        self.assertIn("offline", str(caught.exception))
        # The call must not silently land on the other connected computer.
        self.assertNotIn("dev-b", str(caught.exception))

    def test_ambiguous_name_requires_device_id(self) -> None:
        self.attach(make_session("dev-a", "Desktop"))
        other_ws = FakeWebSocket()
        other = make_session("dev-b", "Desktop")
        self.handler._sessions[other_ws] = other
        self.handler._observe_device(other)

        with self.assertRaises(DesktopError) as caught:
            self.handler._resolve_target("Desktop")
        self.assertIn("Ambiguous", str(caught.exception))

    def test_pin_keeps_a_failed_device_failed_instead_of_switching(self) -> None:
        requester = DesktopRequesterContext(chat_session_id="chat-1", profile="default")
        pinned = make_session("dev-a", "Laptop")
        self.attach(pinned)
        self.handler._set_pin(requester, "dev-a")
        self.assertEqual(self.handler._pin_for(requester), "dev-a")

        self.handler._mark_offline(pinned)
        self.handler._sessions.clear()
        replacement_ws = FakeWebSocket()
        replacement = make_session("dev-b", "Studio")
        self.handler._sessions[replacement_ws] = replacement
        self.handler._observe_device(replacement)

        with self.assertRaises(DesktopError) as caught:
            asyncio.run(self.handler.handle_command("desktop_read_file", {"path": "/tmp/x"}, requester=requester))
        message = str(caught.exception)
        self.assertIn("pinned", message)
        self.assertIn("dev-a", message)
        self.assertIn("offline", message)

    def test_pin_expires(self) -> None:
        requester = DesktopRequesterContext(chat_session_id="chat-2", profile="default")
        self.handler._set_pin(requester, "dev-a")
        key = self.handler._pin_key(requester)
        device_id, _ = self.handler._pins[key]
        self.handler._pins[key] = (device_id, time.time() - (13 * 3600))
        self.assertIsNone(self.handler._pin_for(requester))
        self.assertNotIn(key, self.handler._pins)

    def test_health_snapshot_carries_capabilities(self) -> None:
        self.attach(make_session("dev-a", "Laptop"))
        snapshot = self.handler.status_snapshot()
        self.assertTrue(snapshot["connected"])
        client = snapshot["clients"][0]
        self.assertEqual(client["adapter"], "linux-hyprland")
        self.assertIn("screen.capture", client["capabilities"]["available"])
        self.assertEqual(snapshot["devices"][0]["device_id"], "dev-a")


class ToolSchemaTests(unittest.TestCase):
    def test_new_tools_are_registered_without_device_injection(self) -> None:
        from plugin.tools import desktop_tool

        for name in (
            "desktop_devices",
            "desktop_ui_status",
            "desktop_ui_windows",
            "desktop_ui_screenshot",
            "desktop_ui_action",
            "desktop_browser_open",
            "desktop_browser_inspect",
            "desktop_browser_click",
            "desktop_browser_type",
            "desktop_browser_screenshot",
            "desktop_browser_upload",
            "desktop_browser_download",
        ):
            self.assertIn(name, desktop_tool._SCHEMAS, name)
            self.assertIn(name, desktop_tool._HANDLERS, name)

        properties = desktop_tool._SCHEMAS["desktop_devices"]["parameters"]["properties"]
        self.assertNotIn("device", properties)
        self.assertNotIn("device_id", properties)

        action_properties = desktop_tool._SCHEMAS["desktop_ui_action"]["parameters"]["properties"]
        self.assertIn("device", action_properties)
        self.assertIn("device_id", action_properties)
        self.assertEqual(desktop_tool._SCHEMAS["desktop_ui_action"]["x-hermes-capability"], "input.control")


if __name__ == "__main__":
    unittest.main()
