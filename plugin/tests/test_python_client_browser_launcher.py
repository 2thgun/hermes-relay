"""Phase-3 launcher tests (fake process/DevToolsActivePort) + opt-in live smoke.

The default suite never starts Chromium: ``spawn`` and ``fetch_json`` are
injected, and every profile lives in a temporary directory.  The live smoke is
skipped unless ``HERMES_RELAY_TEST_LIVE_CHROMIUM=1`` and runs headless with
``--no-sandbox`` when the process is root, so a container without a GUI can
still exercise the real browser path.
"""

from __future__ import annotations

import base64
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from aiohttp import web

from plugin.relay.client.browser.controller import BrowserController, BrowserError
from plugin.relay.client.browser.launcher import (
    DEVTOOLS_PORT_FILE,
    BrowserLauncher,
    BrowserLaunchError,
)


def _env(home: Path, **extra: str) -> dict[str, str]:
    env = {"HOME": str(home), "HERMES_HOME": str(home / "hermes"), "PATH": ""}
    env.update(extra)
    return env


class LauncherStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.profile = Path(self.tmp.name) / "profile"
        self.profile.mkdir()
        self.fetched: list[str] = []

    def launcher(self, *, fetch: Any = None, **kwargs: Any) -> BrowserLauncher:
        async def default_fetch(url: str, timeout_ms: int) -> Any:
            self.fetched.append(url)
            return {
                "Browser": "Chromium/156.0",
                "webSocketDebuggerUrl": "ws://127.0.0.1:4567/devtools/browser/abc",
            }

        return BrowserLauncher(
            env=_env(
                self.home,
                HERMES_RELAY_BROWSER_PROFILE=str(self.profile),
            ),
            which=lambda name: "/usr/bin/chromium",
            fetch_json=fetch or default_fetch,
            **kwargs,
        )

    async def test_reports_running_browser_from_devtools_port(self) -> None:
        (self.profile / DEVTOOLS_PORT_FILE).write_text(
            "4567\n/devtools/browser/abc\n", encoding="utf-8"
        )
        status = await self.launcher().status()
        self.assertTrue(status.available)
        self.assertTrue(status.running)
        self.assertEqual(status.binary_path, "/usr/bin/chromium")
        self.assertEqual(status.profile_dir, str(self.profile))
        self.assertEqual(status.endpoint, "http://127.0.0.1:4567")
        self.assertEqual(status.web_socket_url, "ws://127.0.0.1:4567/devtools/browser/abc")
        self.assertEqual(status.browser_version, "Chromium/156.0")
        self.assertEqual(
            self.fetched, ["http://127.0.0.1:4567/json/version"]
        )

    async def test_stale_port_file_is_not_running(self) -> None:
        (self.profile / DEVTOOLS_PORT_FILE).write_text(
            "4567\n/devtools/browser/abc\n", encoding="utf-8"
        )

        async def unreachable(url: str, timeout_ms: int) -> Any:
            return None

        status = await self.launcher(fetch=unreachable).status()
        self.assertTrue(status.available)
        self.assertFalse(status.running)
        self.assertIn("not answering", status.reason or "")

    async def test_missing_browser_is_reported_and_ensure_fails(self) -> None:
        launcher = BrowserLauncher(
            env=_env(self.home, HERMES_RELAY_BROWSER_PROFILE=str(self.profile)),
            which=lambda name: None,
        )
        status = await launcher.status()
        self.assertFalse(status.available)
        self.assertFalse(status.running)
        self.assertIn("No Chrome/Chromium/Edge/Brave binary found", status.reason or "")
        with self.assertRaises(BrowserLaunchError) as raised:
            await launcher.ensure()
        self.assertEqual(raised.exception.code, "browser_not_found")

    async def test_default_profile_is_hermes_home_browser_profile(self) -> None:
        launcher = BrowserLauncher(
            env={"HOME": str(self.home), "HERMES_HOME": str(self.home / "custom")},
            which=lambda name: "/usr/bin/chromium",
        )
        self.assertEqual(launcher.profile_dir, self.home / "custom" / "browser-profile")
        self.assertEqual(
            launcher.downloads_dir, self.home / "custom" / "browser-profile" / "downloads"
        )

    async def test_status_refuses_a_signed_in_profile(self) -> None:
        chrome_profile = self.home / ".config" / "google-chrome"
        chrome_profile.mkdir(parents=True)
        (chrome_profile / DEVTOOLS_PORT_FILE).write_text(
            "4567\n/devtools/browser/abc\n", encoding="utf-8"
        )
        launcher = BrowserLauncher(
            env=_env(
                self.home,
                HERMES_RELAY_BROWSER_PROFILE=str(chrome_profile),
            ),
            which=lambda name: "/usr/bin/chromium",
        )
        status = await launcher.status()
        self.assertTrue(status.available)
        self.assertFalse(status.running)
        self.assertIn("signed-in browser profile", status.reason or "")

    async def test_profile_override_wins(self) -> None:
        override = Path(self.tmp.name) / "elsewhere"
        launcher = BrowserLauncher(
            env=_env(
                self.home,
                HERMES_HOME=str(self.home / "hermes"),
                HERMES_RELAY_BROWSER_PROFILE=str(override),
            ),
            which=lambda name: "/usr/bin/chromium",
        )
        self.assertEqual(launcher.profile_dir, override)

    async def test_port_file_validation(self) -> None:
        launcher = self.launcher()
        for content in ("nope\n/devtools/browser/x\n", "99999\n/devtools/browser/x\n", "1234\nrelative\n", ""):
            (self.profile / DEVTOOLS_PORT_FILE).write_text(content, encoding="utf-8")
            self.assertIsNone(launcher._read_endpoint(), content)

    async def test_binary_env_override_accepts_existing_path(self) -> None:
        binary = Path(self.tmp.name) / "custom-chrome"
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(0o755)
        launcher = BrowserLauncher(
            env=_env(
                self.home,
                HERMES_RELAY_BROWSER_PROFILE=str(self.profile),
                HERMES_RELAY_BROWSER=str(binary),
            ),
            which=lambda name: None,
        )
        self.assertEqual(await launcher.binary(), str(binary))


class LauncherEnsureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.profile = Path(self.tmp.name) / "profile"
        self.spawned: list[tuple[str, list[str], Path]] = []

    def launcher(self, **kwargs: Any) -> BrowserLauncher:
        profile = self.profile

        def spawn(binary: str, args: list[str] | tuple[str, ...], log_path: Path) -> int:
            self.spawned.append((binary, list(args), log_path))
            profile.mkdir(parents=True, exist_ok=True)
            (profile / DEVTOOLS_PORT_FILE).write_text(
                "5123\n/devtools/browser/xyz\n", encoding="utf-8"
            )
            return 4242

        async def fetch(url: str, timeout_ms: int) -> Any:
            return {
                "Browser": "Chromium/156.0",
                "webSocketDebuggerUrl": "ws://127.0.0.1:5123/devtools/browser/xyz",
            }

        return BrowserLauncher(
            env=_env(self.home, HERMES_RELAY_BROWSER_PROFILE=str(profile)),
            which=lambda name: "/usr/bin/chromium",
            spawn=spawn,
            fetch_json=fetch,
            launch_timeout_ms=1_000,
            poll_interval_ms=10,
            **kwargs,
        )

    async def test_ensure_launches_dedicated_profile_and_reads_endpoint(self) -> None:
        result = await self.launcher().ensure(headless=True)
        self.assertTrue(result.launched)
        self.assertEqual(result.endpoint, "http://127.0.0.1:5123")
        self.assertEqual(result.web_socket_url, "ws://127.0.0.1:5123/devtools/browser/xyz")
        self.assertEqual(result.profile_dir, str(self.profile))
        self.assertEqual(result.binary_path, "/usr/bin/chromium")
        self.assertEqual(result.browser_version, "Chromium/156.0")
        self.assertEqual(result.pid, 4242)
        self.assertEqual(result.log_path, str(self.profile / "chromium.log"))

        binary, args, log_path = self.spawned[0]
        self.assertEqual(binary, "/usr/bin/chromium")
        self.assertEqual(log_path, self.profile / "chromium.log")
        self.assertIn("--headless=new", args)
        self.assertIn("--remote-debugging-port=0", args)
        self.assertIn(f"--user-data-dir={self.profile}", args)
        self.assertIn("about:blank", args)
        self.assertEqual(
            [arg for arg in args if arg.startswith("--user-data-dir=")],
            [f"--user-data-dir={self.profile}"],
        )
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.assertIn("--no-sandbox", args)
        self.assertTrue(self.profile.is_dir())
        self.assertTrue((self.profile / "downloads").is_dir())
        self.assertEqual((self.profile.stat().st_mode & 0o777), 0o700)
        self.assertEqual(((self.profile / "downloads").stat().st_mode & 0o777), 0o700)

    async def test_ensure_removes_a_stale_port_file_before_spawn(self) -> None:
        self.profile.mkdir()
        stale = self.profile / DEVTOOLS_PORT_FILE
        stale.write_text("9999\n/devtools/browser/stale\n", encoding="utf-8")
        observed: list[bool] = []

        def spawn(binary: str, args: Any, log_path: Path) -> int:
            observed.append(stale.exists())
            stale.write_text("5123\n/devtools/browser/xyz\n", encoding="utf-8")
            return 1

        async def fetch(url: str, timeout_ms: int) -> Any:
            if "9999" in url:
                return None
            return {"Browser": "Chromium/156.0"}

        launcher = BrowserLauncher(
            env=_env(self.home, HERMES_RELAY_BROWSER_PROFILE=str(self.profile)),
            which=lambda name: "/usr/bin/chromium",
            spawn=spawn,
            fetch_json=fetch,
            launch_timeout_ms=1_000,
            poll_interval_ms=10,
        )
        await launcher.ensure()
        self.assertEqual(observed, [False])

    async def test_ensure_reuses_a_running_browser(self) -> None:
        self.profile.mkdir()
        (self.profile / DEVTOOLS_PORT_FILE).write_text(
            "5123\n/devtools/browser/xyz\n", encoding="utf-8"
        )
        result = await self.launcher().ensure()
        self.assertFalse(result.launched)
        self.assertEqual(result.endpoint, "http://127.0.0.1:5123")
        self.assertEqual(self.spawned, [])

    async def test_headless_defaults_from_env(self) -> None:
        launcher = self.launcher()
        launcher.env = _env(
            self.home,
            HERMES_RELAY_BROWSER_PROFILE=str(self.profile),
            HERMES_RELAY_BROWSER_HEADLESS="1",
        )
        await launcher.ensure()
        self.assertIn("--headless=new", self.spawned[0][1])

    async def test_operator_args_cannot_override_profile_or_port(self) -> None:
        launcher = self.launcher()
        launcher.env = _env(
            self.home,
            HERMES_RELAY_BROWSER_PROFILE=str(self.profile),
            HERMES_RELAY_BROWSER_ARGS=(
                "--no-sandbox --disable-gpu --user-data-dir=/home/u/.config/chromium "
                "--remote-debugging-port=9222 --headless=old"
            ),
        )
        await launcher.ensure()
        args = self.spawned[0][1]
        self.assertIn("--disable-gpu", args)
        self.assertNotIn("--headless=old", args)
        self.assertEqual(
            [arg for arg in args if arg.startswith("--user-data-dir=")],
            [f"--user-data-dir={self.profile}"],
        )
        self.assertEqual(
            [arg for arg in args if arg.startswith("--remote-debugging-port=")],
            ["--remote-debugging-port=0"],
        )
        self.assertEqual(args.count("--no-sandbox"), 1)

    async def test_refuses_a_signed_in_browser_profile(self) -> None:
        chrome_profile = self.home / ".config" / "google-chrome"
        launcher = BrowserLauncher(
            env=_env(
                self.home,
                HERMES_RELAY_BROWSER_PROFILE=str(chrome_profile),
            ),
            which=lambda name: "/usr/bin/chromium",
            spawn=lambda binary, args, log_path: 1,
        )
        with self.assertRaises(BrowserLaunchError) as raised:
            await launcher.ensure()
        self.assertEqual(raised.exception.code, "unsafe_profile")
        self.assertIn("signed-in browser profile", str(raised.exception))
        self.assertEqual(self.spawned, [])

    async def test_refuses_profile_directory_equal_to_home(self) -> None:
        launcher = BrowserLauncher(
            env=_env(self.home, HERMES_RELAY_BROWSER_PROFILE=str(self.home)),
            which=lambda name: "/usr/bin/chromium",
            spawn=lambda binary, args, log_path: 1,
        )
        with self.assertRaises(BrowserLaunchError) as raised:
            await launcher.ensure()
        self.assertEqual(raised.exception.code, "unsafe_profile")

    async def test_launch_timeout_is_a_structured_error(self) -> None:
        def spawn(binary: str, args: Any, log_path: Path) -> int:
            return 7

        async def never(url: str, timeout_ms: int) -> Any:
            return None

        launcher = BrowserLauncher(
            env=_env(self.home, HERMES_RELAY_BROWSER_PROFILE=str(self.profile)),
            which=lambda name: "/usr/bin/chromium",
            spawn=spawn,
            fetch_json=never,
            launch_timeout_ms=100,
            poll_interval_ms=10,
        )
        with self.assertRaises(BrowserLaunchError) as raised:
            await launcher.ensure()
        self.assertEqual(raised.exception.code, "launch_timeout")
        self.assertIn("100ms", str(raised.exception))


class DownloadDirectoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile = Path(self.tmp.name) / "profile"
        self.launcher = BrowserLauncher(
            env={"HOME": str(self.tmp.name), "HERMES_RELAY_BROWSER_PROFILE": str(self.profile)},
            which=lambda name: "/usr/bin/chromium",
        )

    async def test_downloads_path_is_private(self) -> None:
        path = await self.launcher.downloads_path()
        self.assertEqual(path, str(self.profile / "downloads"))
        self.assertEqual((Path(path).stat().st_mode & 0o777), 0o700)

    async def test_last_download_picks_newest_complete_file(self) -> None:
        downloads = await self.launcher.downloads_path()
        older = Path(downloads) / "older.txt"
        newer = Path(downloads) / "newer.txt"
        partial = Path(downloads) / "big.bin.crdownload"
        older.write_text("old", encoding="utf-8")
        newer.write_text("newer payload", encoding="utf-8")
        partial.write_text("incomplete", encoding="utf-8")
        os.utime(older, (1_700_000_000, 1_700_000_000))
        os.utime(newer, (1_700_000_100, 1_700_000_100))
        os.utime(partial, (1_700_000_200, 1_700_000_200))
        latest = await self.launcher.last_download()
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest["path"], str(newer))
        self.assertEqual(latest["size"], len("newer payload"))
        self.assertEqual(latest["modified_ms"], 1_700_000_100_000)

    async def test_last_download_none_when_directory_missing(self) -> None:
        self.assertIsNone(await self.launcher.last_download())


# ── Optional live Chromium smoke ────────────────────────────────────────────


class FixtureServer:
    """Tiny local HTTP fixture; phase-3 tests must not use file:// URLs."""

    HTML = """<!doctype html>
<html><head><title>Browser Fixture</title></head>
<body>
  <button id="go" onclick="document.getElementById('out').textContent='clicked'">Go</button>
  <input id="query" aria-label="Query">
  <a id="dl" href="/download">Download</a>
  <input id="file" type="file" aria-label="Attachment">
  <div id="out"></div>
</body></html>"""

    def __init__(self) -> None:
        self._runner: web.AppRunner | None = None
        self.port = 0

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/next", self._next)
        app.router.add_get("/download", self._download)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = int(self._runner.addresses[0][1])

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _index(self, request: web.Request) -> web.Response:
        return web.Response(text=self.HTML, content_type="text/html")

    async def _next(self, request: web.Request) -> web.Response:
        return web.Response(text="<title>Next</title><p>next page</p>", content_type="text/html")

    async def _download(self, request: web.Request) -> web.Response:
        return web.Response(
            body=b"fixture-download-bytes",
            content_type="application/octet-stream",
            headers={"Content-Disposition": 'attachment; filename="fixture.bin"'},
        )


@unittest.skipUnless(
    os.environ.get("HERMES_RELAY_TEST_LIVE_CHROMIUM") == "1",
    "set HERMES_RELAY_TEST_LIVE_CHROMIUM=1 to run the live Chromium smoke",
)
class LiveChromiumSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_headless_inspect_click_type_screenshot(self) -> None:
        fixture = FixtureServer()
        await fixture.start()
        self.addAsyncCleanup(fixture.stop)

        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        env = dict(os.environ)
        env["HERMES_HOME"] = tmp.name
        env["HERMES_RELAY_BROWSER_PROFILE"] = str(Path(tmp.name) / "browser-profile")
        env["HERMES_RELAY_BROWSER_HEADLESS"] = "1"
        launcher = BrowserLauncher(env=env, launch_timeout_ms=30_000)
        if await launcher.binary() is None:
            self.skipTest("no Chrome/Chromium binary installed")

        controller = BrowserController(launcher, timeout_ms=20_000)
        try:
            opened = await controller.open(f"http://127.0.0.1:{fixture.port}/")
            self.assertEqual(opened["title"], "Browser Fixture")

            inspected = await controller.inspect(max_elements=20, include_text=True)
            button = next(
                element for element in inspected["elements"] if element["tag"] == "button"
            )
            self.assertEqual(button["name"], "Go")
            field = next(
                element for element in inspected["elements"] if element["tag"] == "input"
            )
            self.assertEqual(field["name"], "Query")

            clicked = await controller.click(ref=button["ref"])
            self.assertTrue(clicked["ok"])
            waited = await controller.wait_for(text="clicked", timeout_ms=5_000)
            self.assertTrue(waited["ok"], waited)

            typed = await controller.type(ref=field["ref"], text="hello", clear=True)
            self.assertEqual(typed["characters"], 5)
            after = await controller.inspect(max_elements=20)
            field_after = next(
                element for element in after["elements"] if element["tag"] == "input"
            )
            self.assertEqual(field_after["value"], "hello")

            await controller.type(selector="#query", text="value-mode", mode="value")
            after_value = await controller.inspect(max_elements=20)
            field_value = next(
                element
                for element in after_value["elements"]
                if element["tag"] == "input" and element.get("type") != "file"
            )
            self.assertEqual(field_value["value"], "value-mode")

            upload = Path(tmp.name) / "upload.txt"
            upload.write_text("upload payload", encoding="utf-8")
            uploaded = await controller.upload(
                selector="#file", files=[str(upload)]
            )
            self.assertTrue(uploaded["ok"], uploaded)

            shot = await controller.screenshot(format="png")
            self.assertEqual(shot["format"], "png")
            self.assertGreaterEqual(shot["pixel_width"], 700)
            self.assertGreaterEqual(shot["pixel_height"], 400)
            self.assertGreater(shot["size_bytes"], 0)
            self.assertEqual(
                base64.b64decode(shot["bytes_base64"])[:8], b"\x89PNG\r\n\x1a\n"
            )

            full = await controller.screenshot(format="png", full_page=True)
            self.assertTrue(full["full_page"])
            self.assertGreaterEqual(full["pixel_height"], shot["pixel_height"])

            scrolled = await controller.scroll(delta_y=300)
            self.assertTrue(scrolled["ok"])

            downloaded = await controller.download(selector="#dl", timeout_ms=15_000)
            self.assertTrue(downloaded["ok"], downloaded)
            self.assertEqual(downloaded["download"]["size"], len(b"fixture-download-bytes"))
            self.assertTrue(Path(downloaded["download"]["path"]).is_file())
            self.assertEqual(
                downloaded["download_dir"], str(Path(tmp.name) / "browser-profile" / "downloads")
            )

            await controller.open(f"http://127.0.0.1:{fixture.port}/next")
            with self.assertRaisesRegex(BrowserError, "references expire after navigation"):
                await controller.click(ref=button["ref"])
        finally:
            await controller.close()


if __name__ == "__main__":
    unittest.main()
