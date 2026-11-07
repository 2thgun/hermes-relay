"""Protocol compatibility tests against the *real* server-side desktop channel.

The fake relay proves the client behaves like a well-formed peer; this module
goes one step further and feeds the client's envelopes into
``plugin.relay.channels.desktop.DesktopHandler`` itself.  If the server renames
a field or changes the response contract, these tests fail without needing a
live relay.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from plugin.relay.channels.desktop import DesktopHandler
from plugin.relay.device.dispatch import ClientState, DesktopDispatcher
from plugin.relay.device.handlers import build_registry
from plugin.relay.device.runtime import build_status_payload


class _FakeWs:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send_str(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


class ServerChannelCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.handler = DesktopHandler()
        self.ws = _FakeWs()
        self.state = ClientState(device_id="python-device", device_name="python-box")
        self.registry = build_registry(state=self.state)
        self.responses: list[dict[str, Any]] = []

        async def send(payload: dict[str, Any]) -> None:
            self.responses.append(payload)

        self.dispatcher = DesktopDispatcher(
            self.registry, send, state=self.state, cwd=str(self.root)
        )

    async def _wait_for(self, predicate, timeout: float = 2.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("condition not met")
            await asyncio.sleep(0.01)

    async def asyncTearDown(self) -> None:
        await self.dispatcher.close()
        await self.handler.close()

    async def test_server_accepts_our_status_envelope(self) -> None:
        status = build_status_payload(self.state, self.registry)
        await self.handler.handle_envelope(
            self.ws,
            {"channel": "desktop", "type": "desktop.status", "payload": status},
        )
        self.assertTrue(self.handler.is_client_connected())
        self.assertTrue(self.handler.has_client_for("desktop_read_file"))
        self.assertFalse(self.handler.has_client_for("desktop_computer_screenshot"))
        session = self.handler.session_for(self.ws)
        assert session is not None
        self.assertEqual(session.advertised_tools, set(self.registry.names()))
        self.assertEqual(session.device_id, "python-device")

    async def test_server_command_round_trip(self) -> None:
        status = build_status_payload(self.state, self.registry)
        await self.handler.handle_envelope(
            self.ws,
            {"channel": "desktop", "type": "desktop.status", "payload": status},
        )
        sample = self.root / "roundtrip.txt"
        sample.write_text("round trip\n", encoding="utf-8")

        command_task = asyncio.create_task(
            self.handler.handle_command("desktop_read_file", {"path": str(sample)})
        )
        await self._wait_for(lambda: bool(self.ws.sent))
        command = self.ws.sent[-1]
        self.assertEqual(command["channel"], "desktop")
        self.assertEqual(command["type"], "desktop.command")
        payload = command["payload"]
        self.assertEqual(payload["tool"], "desktop_read_file")
        self.assertEqual(payload["args"], {"path": str(sample)})
        self.assertTrue(payload["request_id"])

        await self.dispatcher.handle_envelope(command)
        await self._wait_for(lambda: bool(self.responses))
        response = self.responses[0]
        self.assertTrue(response["ok"], response)

        await self.handler.handle_envelope(
            self.ws,
            {"channel": "desktop", "type": "desktop.response", "payload": response},
        )
        result = await asyncio.wait_for(command_task, timeout=2)
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"], "round trip\n")
        self.assertEqual(result["target"]["device_id"], "python-device")

    async def test_server_rejects_unadvertised_tool_before_sending(self) -> None:
        status = build_status_payload(self.state, self.registry)
        await self.handler.handle_envelope(
            self.ws,
            {"channel": "desktop", "type": "desktop.status", "payload": status},
        )
        with self.assertRaisesRegex(Exception, "does not advertise tool"):
            await self.handler.handle_command("desktop_computer_screenshot", {})
