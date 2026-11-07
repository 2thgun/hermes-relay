"""Transport, pairing, dispatch, and reconnect tests for the Python client.

The fake relay in ``_fake_relay.py`` speaks the same envelopes as
``plugin/relay/channels/desktop.py``; these tests drive ``pair`` and ``run``
end to end, including ``desktop_read_file`` and ``desktop_terminal``.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from plugin.relay.device import dispatch as dispatch_mod
from plugin.relay.device.dispatch import ClientState, DesktopDispatcher, ToolRegistry
from plugin.relay.device.identity import (
    ClientIdentity,
    IdentityError,
    normalize_relay_url,
)
from plugin.relay.device.runtime import ServeOptions, pair, prepare_serve, serve
from plugin.relay.device.transport import AuthError, RelayTransport
from plugin.tests._fake_relay import FakeRelay


class PairingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.relay = await FakeRelay().start()
        self.addAsyncCleanup(self.relay.stop)
        self.env = patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        self.env.start()
        self.addCleanup(self.env.stop)

    async def test_pair_stores_token_and_identity(self) -> None:
        result = await pair(self.relay.url, "abc123", name="test-box")
        self.assertEqual(result.device_name, "test-box")
        self.assertEqual(result.server_version, "test-relay-1.0")
        self.assertEqual(result.url, normalize_relay_url(self.relay.url))

        auth = self.relay.auth_requests[0]
        self.assertEqual(auth["pairing_code"], "ABC123")
        self.assertEqual(auth["device_name"], "test-box")
        self.assertEqual(auth["device_id"], result.device_id)
        self.assertEqual(auth["client_surface"], "python")
        self.assertEqual(auth["supports"], {"typed_stream_events": True, "event_schema_version": 1})

        store = self.home / "relay" / "client.json"
        self.assertTrue(store.exists())
        self.assertEqual(stat.S_IMODE(store.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(store.parent.stat().st_mode), 0o700)
        data = json.loads(store.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["device_id"], result.device_id)
        record = data["hosts"][normalize_relay_url(self.relay.url)]
        self.assertEqual(record["session_token"], self.relay.token)
        self.assertEqual(record["server_version"], "test-relay-1.0")

        # The token must never be printed or stored in device metadata.
        self.assertNotIn(self.relay.token, result.stored_path)

    async def test_pair_rejects_bad_code_without_writing_store(self) -> None:
        with self.assertRaises(AuthError) as raised:
            await pair(self.relay.url, "ZZZZZZ")
        self.assertIn("Invalid pairing code", str(raised.exception))
        store = self.home / "relay" / "client.json"
        # A failed pair does not create a token record.  (The identity helper
        # only writes on save(), which pair() calls after auth.ok.)
        self.assertFalse(store.exists())

    async def test_pair_rejects_malformed_code_locally(self) -> None:
        with self.assertRaises(IdentityError):
            await pair(self.relay.url, "SHORT")
        self.assertEqual(self.relay.connection_count, 0)

    async def test_identity_round_trip_and_corrupt_store(self) -> None:
        identity = ClientIdentity.load(self.home / "relay" / "client.json")
        identity.remember_pairing(
            self.relay.url,
            session_token="tok",
            server_version="1.0",
            grants={"desktop": None},
        )
        path = identity.save()
        loaded = ClientIdentity.load(path)
        self.assertEqual(loaded.device_id, identity.device_id)
        record = loaded.record_for(self.relay.url)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.session_token, "tok")
        self.assertIsNone(record.grants["desktop"])

        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(IdentityError):
            ClientIdentity.load(path)


class ServeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workdir = Path(self.tmp.name)
        self.relay = await FakeRelay().start()
        self.addAsyncCleanup(self.relay.stop)
        self._serve_task: asyncio.Task[None] | None = None
        self._handle = None

    async def _start_serve(self, **overrides) -> None:
        options = ServeOptions(
            url=self.relay.url,
            token=self.relay.token,
            cwd=str(self.workdir),
            heartbeat_interval=0.2,
            handler_timeout=5.0,
            reconnect_base=0.05,
            reconnect_max=0.2,
            **overrides,
        )
        handle, identity = prepare_serve(options)
        self._handle = handle
        self._serve_task = asyncio.create_task(serve(handle, identity, options))
        await self.relay.wait_connected()

    async def _stop_serve(self) -> None:
        if self._handle is not None:
            self._handle.stop_event.set()
            self._handle.transport.stop()
        if self._serve_task is not None:
            try:
                await asyncio.wait_for(self._serve_task, timeout=5)
            except asyncio.CancelledError:
                pass

    async def test_serves_read_file_and_terminal(self) -> None:
        sample = self.workdir / "sample.txt"
        sample.write_text("hello from the client\n", encoding="utf-8")
        await self._start_serve()

        status = await self.relay.wait_status()
        self.assertEqual(
            set(status["advertised_tools"]),
            set(self._handle.dispatcher.registry.names()),
        )
        self.assertIn("desktop_read_file", status["advertised_tools"])
        self.assertIn("desktop_terminal", status["advertised_tools"])
        self.assertNotIn("desktop_powershell", status["advertised_tools"])
        self.assertTrue(status["device_id"])
        self.assertEqual(status["platform"], sys.platform)

        read_response = await self.relay.send_command(
            "desktop_read_file", {"path": str(sample)}
        )
        self.assertTrue(read_response["ok"], read_response)
        self.assertEqual(read_response["result"], "hello from the client\n")
        self.assertEqual(
            set(read_response), {"request_id", "ok", "result"}
        )

        terminal_response = await self.relay.send_command(
            "desktop_terminal", {"command": "printf 'terminal-ok'"}
        )
        self.assertTrue(terminal_response["ok"], terminal_response)
        result = terminal_response["result"]
        self.assertEqual(result["stdout"], "terminal-ok")
        self.assertEqual(result["stderr"], "")
        self.assertEqual(result["exit_code"], 0)
        self.assertGreaterEqual(result["duration_ms"], 0)

        await self._stop_serve()

    async def test_unknown_tool_returns_structured_error(self) -> None:
        await self._start_serve()
        response = await self.relay.send_command("desktop_nope", {"x": 1})
        self.assertFalse(response["ok"])
        self.assertTrue(response["error"].startswith("unsupported_tool:"))
        self.assertEqual(response["code"], "unsupported_tool")
        self.assertEqual(response["tool"], "desktop_nope")
        await self._stop_serve()

    async def test_allowlist_shapes_advertisement_and_dispatch(self) -> None:
        await self._start_serve(allow={"desktop_read_file", "desktop_health"})
        status = await self.relay.wait_status()
        self.assertEqual(
            status["advertised_tools"], ["desktop_health", "desktop_read_file"]
        )
        response = await self.relay.send_command("desktop_terminal", {"command": "echo hi"})
        self.assertFalse(response["ok"])
        self.assertEqual(response["code"], "unsupported_tool")
        await self._stop_serve()

    async def test_reconnects_after_socket_drop(self) -> None:
        relay = FakeRelay(close_after_status_count=1)
        await relay.start()
        self.addAsyncCleanup(relay.stop)
        options = ServeOptions(
            url=relay.url,
            token=relay.token,
            cwd=str(self.workdir),
            heartbeat_interval=0.1,
            handler_timeout=5.0,
            reconnect_base=0.05,
            reconnect_max=0.2,
        )
        handle, identity = prepare_serve(options)
        task = asyncio.create_task(serve(handle, identity, options))
        try:
            await relay.wait_connections(2, timeout=8.0)
            deadline = asyncio.get_running_loop().time() + 5.0
            while len(relay.status_updates) < 2:
                if asyncio.get_running_loop().time() >= deadline:
                    self.fail(
                        f"expected a status heartbeat on reconnect, saw {len(relay.status_updates)}"
                    )
                await asyncio.sleep(0.02)
            response = await relay.send_command(
                "desktop_read_file", {"path": str(self.workdir / "missing")}
            )
            self.assertFalse(response["ok"])
            self.assertIn("ENOENT", response["error"])
        finally:
            handle.stop_event.set()
            handle.transport.stop()
            await asyncio.wait_for(task, timeout=5)

    async def test_disconnect_cancels_in_flight_request(self) -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def slow(args, ctx):
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        await self._start_serve()
        self._handle.dispatcher.registry.add("desktop_slow", slow)
        command_task = asyncio.create_task(
            self.relay.send_command("desktop_slow", {}, timeout=5)
        )
        await asyncio.wait_for(started.wait(), timeout=3)
        await self.relay.close_current()
        await asyncio.wait_for(cancelled.wait(), timeout=5)
        command_task.cancel()
        try:
            await command_task
        except (asyncio.CancelledError, AssertionError):
            pass
        await self._stop_serve()

    async def test_auth_failure_is_terminal(self) -> None:
        relay = FakeRelay(reject_auth_reason="bad token")
        await relay.start()
        self.addAsyncCleanup(relay.stop)
        options = ServeOptions(
            url=relay.url,
            token="wrong-token",
            cwd=str(self.workdir),
            reconnect_base=0.05,
            reconnect_max=0.1,
        )
        handle, identity = prepare_serve(options)
        with self.assertRaises(AuthError):
            await asyncio.wait_for(serve(handle, identity, options), timeout=5)
        self.assertEqual(relay.connection_count, 1)


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def _wait_for(self, predicate, timeout: float = 2.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("condition not met")
            await asyncio.sleep(0.01)

    async def test_handler_timeout_serializes_as_error(self) -> None:
        registry = ToolRegistry()

        async def slow(args, ctx):
            await asyncio.sleep(5)
            return "never"

        registry.add("desktop_slow", slow)
        sent: list[dict] = []

        async def send(payload):
            sent.append(payload)

        dispatcher = DesktopDispatcher(registry, send, timeout=0.05)
        await dispatcher.handle_envelope(
            {
                "channel": "desktop",
                "type": "desktop.command",
                "payload": {"request_id": "r1", "tool": "desktop_slow", "args": {}},
            }
        )
        await self._wait_for(lambda: bool(sent))
        self.assertFalse(sent[0]["ok"])
        self.assertIn("timed out", sent[0]["error"])
        await dispatcher.close()

    async def test_wire_budget_rejects_oversized_result(self) -> None:
        registry = ToolRegistry()

        async def big(args, ctx):
            return "x" * 5000

        registry.add("desktop_big", big)
        sent: list[dict] = []

        async def send(payload):
            sent.append(payload)

        dispatcher = DesktopDispatcher(registry, send, max_wire_bytes=512)
        await dispatcher.handle_envelope(
            {
                "channel": "desktop",
                "type": "desktop.command",
                "payload": {"request_id": "r2", "tool": "desktop_big", "args": {}},
            }
        )
        await self._wait_for(lambda: bool(sent))
        self.assertFalse(sent[0]["ok"])
        self.assertIn("response budget", sent[0]["error"])

    async def test_duplicate_request_id_rejected(self) -> None:
        registry = ToolRegistry()

        async def slow(args, ctx):
            await asyncio.sleep(0.2)
            return "ok"

        registry.add("desktop_slow", slow)
        sent: list[dict] = []

        async def send(payload):
            sent.append(payload)

        dispatcher = DesktopDispatcher(registry, send, timeout=2.0)
        envelope = {
            "channel": "desktop",
            "type": "desktop.command",
            "payload": {"request_id": "dup", "tool": "desktop_slow", "args": {}},
        }
        await dispatcher.handle_envelope(envelope)
        await dispatcher.handle_envelope(envelope)
        await self._wait_for(lambda: len(sent) >= 2)
        self.assertEqual([item["ok"] for item in sent[:2]], [False, True])
        self.assertEqual(sent[0]["code"], "duplicate_request_id")
        await dispatcher.close()

    async def test_malformed_command_is_dropped(self) -> None:
        registry = ToolRegistry()
        sent: list[dict] = []

        async def send(payload):
            sent.append(payload)

        dispatcher = DesktopDispatcher(registry, send)
        await dispatcher.handle_envelope(
            {"channel": "desktop", "type": "desktop.command", "payload": {"tool": "x"}}
        )
        await asyncio.sleep(0.05)
        self.assertEqual(sent, [])

    async def test_state_records_last_error(self) -> None:
        registry = ToolRegistry()

        async def boom(args, ctx):
            raise dispatch_mod.ToolError("kaboom")

        registry.add("desktop_boom", boom)
        state = ClientState()
        sent: list[dict] = []

        async def send(payload):
            sent.append(payload)

        dispatcher = DesktopDispatcher(registry, send, state=state)
        await dispatcher.handle_envelope(
            {
                "channel": "desktop",
                "type": "desktop.command",
                "payload": {"request_id": "r3", "tool": "desktop_boom", "args": {}},
            }
        )
        await self._wait_for(lambda: bool(sent))
        self.assertEqual(sent[0]["error"], "kaboom")
        self.assertEqual(state.failed, 1)
        self.assertEqual(state.last_error["message"], "kaboom")


class BackoffTests(unittest.TestCase):
    def _transport(self) -> RelayTransport:
        identity = ClientIdentity.load(Path("/tmp/definitely-not-a-client-store.json"))
        return RelayTransport(
            "ws://127.0.0.1:1/ws",
            identity=identity,
            token="t",
            reconnect_base=1.0,
            reconnect_max=30.0,
        )

    def test_exponential_backoff_matches_typescript_client(self) -> None:
        transport = self._transport()
        self.assertEqual(transport.backoff_for(1), 1.0)
        self.assertEqual(transport.backoff_for(2), 2.0)
        self.assertEqual(transport.backoff_for(3), 4.0)
        self.assertEqual(transport.backoff_for(4), 8.0)
        self.assertEqual(transport.backoff_for(5), 16.0)
        self.assertEqual(transport.backoff_for(6), 30.0)
        self.assertEqual(transport.backoff_for(20), 30.0)

    def test_rate_limit_close_gets_five_minutes(self) -> None:
        transport = self._transport()
        self.assertEqual(transport.backoff_for(1, "HTTP 429"), 300.0)
        self.assertEqual(transport.backoff_for(1, "rate-limited"), 300.0)
