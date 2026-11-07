"""Subprocess tests for ``python -m plugin.relay.client``.

These cover the actual command line: ``pair`` stores a token, ``run --once``
serves a real ``desktop.command`` over a fake relay, and ``tools``/``status``
stay usable without a network.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from plugin.tests._fake_relay import FakeRelay

REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable


class CliTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.workdir = Path(self.tmp.name) / "work"
        self.workdir.mkdir()
        self.relay = await FakeRelay().start()
        self.addAsyncCleanup(self.relay.stop)

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["HERMES_HOME"] = str(self.home)
        env["PYTHONUNBUFFERED"] = "1"
        return env

    async def _run_cli(self, *args: str, timeout: float = 20.0):
        process = await asyncio.create_subprocess_exec(
            PYTHON,
            "-m",
            "plugin.relay.client",
            *args,
            cwd=str(REPO_ROOT),
            env=self._env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        return process.returncode, stdout.decode(), stderr.decode()

    async def _pair(self) -> dict:
        code, stdout, stderr = await self._run_cli(
            "pair",
            "--url",
            self.relay.url,
            "--code",
            "ABC123",
            "--name",
            "cli-test-box",
            "--json",
        )
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout.strip().splitlines()[-1])
        self.assertTrue(payload["ok"], payload)
        return payload

    async def test_pair_persists_token_via_cli(self) -> None:
        payload = await self._pair()
        self.assertEqual(payload["device_name"], "cli-test-box")
        self.assertEqual(payload["server_version"], "test-relay-1.0")
        self.assertNotIn(self.relay.token, json.dumps(payload))

        store = self.home / "relay" / "client.json"
        self.assertTrue(store.exists())
        data = json.loads(store.read_text(encoding="utf-8"))
        record = data["hosts"][payload["url"]]
        self.assertEqual(record["session_token"], self.relay.token)
        self.assertEqual(self.relay.auth_requests[0]["pairing_code"], "ABC123")

    async def test_run_once_serves_desktop_read_file(self) -> None:
        paired = await self._pair()
        sample = self.workdir / "cli.txt"
        sample.write_text("served through the CLI\n", encoding="utf-8")

        process = await asyncio.create_subprocess_exec(
            PYTHON,
            "-m",
            "plugin.relay.client",
            "run",
            "--url",
            self.relay.url,
            "--cwd",
            str(self.workdir),
            "--once",
            "--heartbeat",
            "0.2",
            "--json",
            cwd=str(REPO_ROOT),
            env=self._env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            ready_line = await asyncio.wait_for(process.stdout.readline(), timeout=15)
            ready = json.loads(ready_line.decode())
            self.assertEqual(ready["event"], "ready")
            self.assertEqual(ready["device_id"], paired["device_id"])
            self.assertIn("desktop_read_file", ready["advertised_tools"])

            await self.relay.wait_status(
                predicate=lambda status: "desktop_read_file" in status["advertised_tools"]
            )
            response = await self.relay.send_command(
                "desktop_read_file", {"path": str(sample)}
            )
            self.assertTrue(response["ok"], response)
            self.assertEqual(response["result"], "served through the CLI\n")

            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
            self.assertEqual(
                process.returncode, 0, stderr.decode() + stdout.decode()
            )
            events = [json.loads(line) for line in stdout.decode().splitlines() if line.strip()]
            self.assertEqual(events[-1]["event"], "stopped")
            self.assertEqual(events[-1]["reason"], "once")
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def test_run_without_pairing_fails_cleanly(self) -> None:
        code, stdout, stderr = await self._run_cli(
            "run", "--state", str(self.home / "relay" / "client.json"), "--json"
        )
        self.assertEqual(code, 2)
        payload = json.loads(stderr.strip().splitlines()[-1])
        self.assertFalse(payload["ok"])
        self.assertIn("no relay URL", payload["error"])

    async def test_tools_and_status_commands(self) -> None:
        code, stdout, stderr = await self._run_cli("tools", "--json")
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertIn("desktop_read_file", payload["tools"])
        self.assertIn("desktop_terminal", payload["tools"])
        if sys.platform != "win32":
            self.assertNotIn("desktop_powershell", payload["tools"])

        await self._pair()
        code, stdout, stderr = await self._run_cli("status", "--json")
        self.assertEqual(code, 0, stderr)
        status = json.loads(stdout)
        self.assertEqual(status["device_name"], "cli-test-box")
        self.assertTrue(status["hosts"][0]["token_present"])
        self.assertNotIn(self.relay.token, json.dumps(status))
