"""Unit tests for the phase-1 non-GUI tool handlers.

These exercise the handlers directly (no relay) against a temporary working
directory, verifying both result shapes and error semantics.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from plugin.relay.client.dispatch import ClientState, ToolContext, ToolError
from plugin.relay.client.handlers import build_registry, clipboard, files, jobs, processes, shell


def _ctx(cwd: Path, cancel: asyncio.Event | None = None) -> ToolContext:
    return ToolContext(cwd=str(cwd), cancel=cancel or asyncio.Event())


class FileHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ctx = _ctx(self.root)

    async def test_read_file_plain_and_truncated(self) -> None:
        path = self.root / "hello.txt"
        path.write_text("hello world\n", encoding="utf-8")
        self.assertEqual(await files.read_file({"path": str(path)}, self.ctx), "hello world\n")

        truncated = await files.read_file({"path": str(path), "max_bytes": 5}, self.ctx)
        self.assertEqual(truncated, "hello\n[... truncated at 5 bytes]")

    async def test_read_file_relative_to_cwd(self) -> None:
        (self.root / "rel.txt").write_text("relative", encoding="utf-8")
        self.assertEqual(
            await files.read_file({"path": "rel.txt"}, self.ctx), "relative"
        )

    async def test_read_file_errors(self) -> None:
        with self.assertRaisesRegex(ToolError, "ENOENT"):
            await files.read_file({"path": "missing.txt"}, self.ctx)
        with self.assertRaisesRegex(ToolError, "not a regular file"):
            await files.read_file({"path": str(self.root)}, self.ctx)

    async def test_write_file_create_dirs_and_bytes(self) -> None:
        target = self.root / "nested" / "dir" / "out.txt"
        result = await files.write_file(
            {"path": str(target), "content": "héllo", "create_dirs": True}, self.ctx
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["bytes_written"], len("héllo".encode("utf-8")))
        self.assertEqual(target.read_text(encoding="utf-8"), "héllo")

    async def test_patch_applies_strict_hunks(self) -> None:
        path = self.root / "code.py"
        path.write_text("a\nb\nc\n", encoding="utf-8")
        patch_text = (
            "--- a/code.py\n"
            "+++ b/code.py\n"
            "@@ -1,3 +1,3 @@\n"
            " a\n"
            "-b\n"
            "+B\n"
            " c\n"
        )
        result = await files.patch_file({"path": str(path), "patch": patch_text}, self.ctx)
        self.assertEqual(result["hunks_applied"], 1)
        self.assertEqual(result["approved"], "auto")
        self.assertEqual(path.read_text(encoding="utf-8"), "a\nB\nc\n")

    async def test_patch_rejects_context_mismatch_without_writing(self) -> None:
        path = self.root / "code.py"
        path.write_text("a\nb\nc\n", encoding="utf-8")
        patch_text = "@@ -1,3 +1,3 @@\n x\n-b\n+B\n c\n"
        with self.assertRaisesRegex(ToolError, "patch did not apply cleanly"):
            await files.patch_file({"path": str(path), "patch": patch_text}, self.ctx)
        self.assertEqual(path.read_text(encoding="utf-8"), "a\nb\nc\n")

    async def test_search_paths_and_contents(self) -> None:
        (self.root / "one.py").write_text("print('needle')\n", encoding="utf-8")
        (self.root / "two.txt").write_text("also needle here\n", encoding="utf-8")
        sub = self.root / "sub"
        sub.mkdir()
        (sub / "three.py").write_text("pass\n", encoding="utf-8")

        path_result = await files.search_files({"pattern": "*.py"}, self.ctx)
        self.assertEqual(len(path_result["matches"]), 2)
        self.assertFalse(path_result["truncated"])

        content_result = await files.search_files(
            {"pattern": "needle", "content": True}, self.ctx
        )
        self.assertEqual(len(content_result["matches"]), 2)
        for match in content_result["matches"]:
            self.assertIn("needle", Path(match).read_text(encoding="utf-8"))

    async def test_checksum_matches_hashlib(self) -> None:
        path = self.root / "bin.dat"
        payload = b"x" * 10000
        path.write_bytes(payload)
        result = await files.checksum({"path": str(path), "algorithm": "sha1"}, self.ctx)
        self.assertEqual(result["algorithm"], "sha1")
        self.assertEqual(result["hash"], hashlib.sha1(payload).hexdigest())
        self.assertEqual(result["size_bytes"], len(payload))
        with self.assertRaisesRegex(ToolError, "algorithm"):
            await files.checksum({"path": str(path), "algorithm": "crc32"}, self.ctx)


class ShellHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ctx = _ctx(self.root)

    async def test_terminal_captures_stdout_stderr_and_exit(self) -> None:
        result = await shell.terminal(
            {
                "command": "printf 'out'; printf 'err' >&2; exit 7",
                "cwd": str(self.root),
            },
            self.ctx,
        )
        self.assertEqual(result["stdout"], "out")
        self.assertEqual(result["stderr"], "err")
        self.assertEqual(result["exit_code"], 7)
        self.assertGreaterEqual(result["duration_ms"], 0)

    async def test_terminal_timeout_kills_child(self) -> None:
        with self.assertRaisesRegex(ToolError, "timed out"):
            await shell.terminal(
                {"command": "sleep 5", "timeout": 0.2}, self.ctx
            )

    async def test_terminal_reports_missing_working_directory(self) -> None:
        with self.assertRaises(ToolError):
            await shell.terminal(
                {"command": "true", "cwd": str(self.root / "missing")}, self.ctx
            )

    async def test_powershell_is_rejected_off_windows(self) -> None:
        if sys.platform == "win32":
            self.skipTest("Windows client exercises the real PowerShell path")
        with self.assertRaisesRegex(ToolError, "only available on Windows"):
            await shell.powershell({"script": "Write-Output hi"}, self.ctx)


class JobHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.jobs_dir = self.root / "jobs"
        self.env = patch.dict(
            os.environ,
            {"HERMES_RELAY_JOBS_DIR": str(self.jobs_dir)},
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.ctx = _ctx(self.root)

    async def _wait_state(self, job_id: str, states: set[str], timeout: float = 5.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            result = await jobs.job_status({"job_id": job_id}, self.ctx)
            if result["state"] in states:
                return result
            if asyncio.get_running_loop().time() >= deadline:
                self.fail(f"job {job_id} stayed in state {result['state']}")
            await asyncio.sleep(0.02)

    async def test_start_status_logs_list(self) -> None:
        started = await jobs.job_start(
            {
                "command": "printf 'job-out'; printf 'job-err' >&2; exit 3",
                "cwd": str(self.root),
            },
            self.ctx,
        )
        self.assertEqual(started["shell"], "cmd" if sys.platform == "win32" else "bash")
        self.assertGreater(started["pid"], 0)
        self.assertTrue(Path(started["log_paths"]["stdout"]).exists())

        status = await self._wait_state(started["job_id"], {"failed", "exited", "killed"})
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["exit_code"], 3)
        self.assertEqual(status["command"], "printf 'job-out'; printf 'job-err' >&2; exit 3")

        logs = await jobs.job_logs({"job_id": started["job_id"]}, self.ctx)
        self.assertEqual(logs["state"], "failed")
        self.assertEqual(logs["stdout"]["content"], "job-out")
        self.assertEqual(logs["stderr"]["content"], "job-err")

        listing = await jobs.job_list({}, self.ctx)
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["jobs"][0]["job_id"], started["job_id"])
        self.assertFalse(listing["truncated"])

    async def test_cancel_running_job(self) -> None:
        await jobs.job_start(
            {"command": "sleep 30", "job_id": "cancel-me"}, self.ctx
        )
        await asyncio.sleep(0.1)
        result = await jobs.job_cancel({"job_id": "cancel-me"}, self.ctx)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["signaled"], "TERM")
        status = await self._wait_state("cancel-me", {"killed", "failed", "exited"})
        self.assertEqual(status["state"], "killed")

    async def test_cancel_unknown_job_errors(self) -> None:
        with self.assertRaisesRegex(ToolError, "unknown job_id"):
            await jobs.job_cancel({"job_id": "nope"}, self.ctx)

    async def test_invalid_job_id_is_rejected(self) -> None:
        with self.assertRaises(ToolError):
            await jobs.job_start(
                {"command": "true", "job_id": "../../etc/passwd"}, self.ctx
            )


class ProcessHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = _ctx(Path(self.tmp.name))

    async def test_list_processes_shape_and_filter(self) -> None:
        result = await processes.list_processes({"filter": "python", "limit": 50}, self.ctx)
        self.assertIn("processes", result)
        self.assertGreaterEqual(result["count"], 1)
        self.assertGreaterEqual(result["total"], result["count"])
        for proc in result["processes"]:
            self.assertIn("pid", proc)
            self.assertIn("command", proc)
            self.assertIn("python", proc["command"].lower())

    async def test_kill_process_by_pid(self) -> None:
        proc = await asyncio.create_subprocess_exec("sleep", "30")
        try:
            result = await processes.kill_process({"pid": proc.pid}, self.ctx)
            self.assertEqual(result["killed"], [proc.pid])
            self.assertEqual(result["failed"], [])
            self.assertEqual(result["requested"], 1)
        finally:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()

    async def test_find_pid_by_port(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            result = await processes.find_pid_by_port({"port": port}, self.ctx)
        finally:
            listener.close()
        self.assertEqual(result["port"], port)
        self.assertEqual(result["protocol"], "tcp")
        if not result["listeners"]:
            self.skipTest("ss/lsof could not expose listener pids in this environment")
        self.assertIn(os.getpid(), [row["pid"] for row in result["listeners"]])


class ClipboardHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = _ctx(Path(self.tmp.name))

    async def test_read_and_write_shapes(self) -> None:
        async def fake_read():
            return b"clipboard text", "fake"

        async def fake_write(content: str):
            return len(content.encode("utf-8")), "fake"

        with patch.object(clipboard, "_read_clipboard", fake_read):
            read = await clipboard.clipboard_read({}, self.ctx)
        self.assertEqual(read, {"content": "clipboard text", "format": "text"})

        with patch.object(clipboard, "_write_clipboard", fake_write):
            written = await clipboard.clipboard_write({"content": "héllo"}, self.ctx)
        self.assertEqual(written, {"wrote": 6})

    async def test_write_rejects_missing_content(self) -> None:
        with self.assertRaisesRegex(ToolError, "content"):
            await clipboard.clipboard_write({}, self.ctx)

    async def test_write_rejects_oversized_content(self) -> None:
        with self.assertRaisesRegex(ToolError, "too large"):
            await clipboard.clipboard_write(
                {"content": "x" * (clipboard.MAX_CLIPBOARD_BYTES + 1)}, self.ctx
            )


class RegistryAndHealthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = _ctx(Path(self.tmp.name))

    async def test_registry_advertises_phase_one_plus_linux_gui_set(self) -> None:
        state = ClientState(device_id="dev-1", device_name="box", url="ws://relay/ws")
        registry = build_registry(state=state)
        names = registry.names()
        expected = {
            "desktop_read_file",
            "desktop_write_file",
            "desktop_patch",
            "desktop_search_files",
            "desktop_checksum",
            "desktop_terminal",
            "desktop_job_start",
            "desktop_job_status",
            "desktop_job_logs",
            "desktop_job_cancel",
            "desktop_job_list",
            "desktop_list_processes",
            "desktop_kill_process",
            "desktop_find_pid_by_port",
            "desktop_clipboard_read",
            "desktop_clipboard_write",
            "desktop_health",
            # Phase 2 Linux graphical adapter.
            "desktop_ui_status",
            "desktop_ui_windows",
            "desktop_ui_screenshot",
            "desktop_ui_action",
            "desktop_screenshot",
            # Phase 3 structured browser/CDP surface.
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
        self.assertEqual(set(names), expected)
        if sys.platform == "win32":
            self.assertIn("desktop_powershell", names)
        else:
            self.assertNotIn("desktop_powershell", names)

    async def test_denylist_removes_from_advertisement(self) -> None:
        registry = build_registry(state=ClientState(), deny={"desktop_terminal"})
        self.assertNotIn("desktop_terminal", registry.names())
        self.assertIn("desktop_read_file", registry.names())

    async def test_health_reports_client_metadata(self) -> None:
        state = ClientState(device_id="dev-1", device_name="box", url="ws://relay/ws")
        registry = build_registry(state=state)
        result = await registry.invoke("desktop_health", {}, self.ctx)
        self.assertTrue(result["ok"])
        self.assertEqual(result["client"], "python")
        self.assertEqual(result["device_id"], "dev-1")
        self.assertEqual(set(result["advertised_tools"]), set(registry.names()))
        self.assertGreaterEqual(result["uptime_ms"], 0)
