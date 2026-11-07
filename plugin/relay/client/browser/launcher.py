"""Browser lifecycle for the Python relay client.

Ported from ``desktop/src/tools/browser/launcher.ts``.  The relay always owns a
dedicated automation profile (``$HERMES_HOME/browser-profile`` by default) so
cookies, localStorage, downloads and "close the browser" never touch the user's
signed-in browser.  ``HERMES_RELAY_BROWSER_PROFILE`` may relocate that profile
(for tests or a second automation instance) but :meth:`BrowserLauncher.ensure`
refuses a path that is a well-known Chrome/Chromium/Brave/Edge/Helium profile.

The browser is launched with ``--remote-debugging-port=0``; Chrome writes the
chosen port and DevTools path to ``<profile>/DevToolsActivePort``, which is the
only endpoint we trust.  A stale file is deleted before every launch so we can
never attach to another process.

``HERMES_RELAY_BROWSER`` overrides binary discovery, ``HERMES_RELAY_BROWSER_ARGS``
adds operator flags (chrome flags such as ``--no-sandbox`` in containers), and
``HERMES_RELAY_BROWSER_HEADLESS=1`` makes headless the default.  Profile and
debug-port flags in the extra args are stripped: isolation and endpoint
discovery stay under our control.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from ..platform.command import which_binary

logger = logging.getLogger(__name__)

DEVTOOLS_PORT_FILE = "DevToolsActivePort"
LAUNCH_TIMEOUT_MS = 15_000
POLL_INTERVAL_MS = 200
DOWNLOAD_SETTLE_TIMEOUT_MS = 30_000

_LINUX_NAMES = [
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "helium-browser",
    "helium",
    "brave-browser",
    "microsoft-edge",
    "microsoft-edge-stable",
]
_MAC_NAMES = ["Google Chrome", "Chromium", "Brave Browser", "Microsoft Edge"]
_WINDOWS_NAMES = ["chrome.exe", "msedge.exe", "brave.exe"]
_WINDOWS_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Chromium\Application\chrome.exe",
]
_MAC_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]

# Directories owned by a human's signed-in browser.  Never launch the
# automation browser against one of these, even through the env override.
_USER_PROFILE_DIRS = (
    ".config/google-chrome",
    ".config/google-chrome-beta",
    ".config/chromium",
    ".config/chromium-browser",
    ".config/brave-browser",
    ".config/microsoft-edge",
    ".config/helium",
    "snap/chromium/common/chromium",
    "snap/google-chrome/common/chrome",
    "Library/Application Support/Google/Chrome",
    "Library/Application Support/Chromium",
    "Library/Application Support/BraveSoftware/Brave-Browser",
    "AppData/Local/Google/Chrome/User Data",
    "AppData/Local/Microsoft/Edge/User Data",
)

_FORBIDDEN_FLAG = re.compile(
    r"^--(user-data-dir|remote-debugging-port|remote-debugging-pipe|profile-directory|headless)",
    re.IGNORECASE,
)

_UNSET = object()


class BrowserLaunchError(Exception):
    """Browser discovery/launch failed with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class BrowserStatus:
    available: bool
    running: bool
    profile_dir: str
    binary_path: str | None = None
    endpoint: str | None = None
    web_socket_url: str | None = None
    browser_version: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "available": self.available,
            "running": self.running,
            "profile_dir": self.profile_dir,
        }
        for key in ("binary_path", "endpoint", "web_socket_url", "browser_version", "reason"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        return payload


@dataclass
class BrowserLaunchResult:
    endpoint: str
    web_socket_url: str
    profile_dir: str
    binary_path: str
    launched: bool
    browser_version: str | None = None
    pid: int | None = None
    log_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "endpoint": self.endpoint,
            "web_socket_url": self.web_socket_url,
            "profile_dir": self.profile_dir,
            "binary_path": self.binary_path,
            "launched": self.launched,
        }
        if self.browser_version is not None:
            payload["browser_version"] = self.browser_version
        if self.pid is not None:
            payload["pid"] = self.pid
        if self.log_path is not None:
            payload["log_path"] = self.log_path
        return payload


def _hermes_home(env: Mapping[str, str]) -> Path:
    raw = (env.get("HERMES_HOME") or "").strip()
    if raw:
        return Path(raw).expanduser()
    home = (env.get("HOME") or "").strip()
    return (Path(home) if home else Path.home()) / ".hermes"


async def _default_fetch_json(url: str, timeout_ms: int) -> Any:
    timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return None
                return await response.json(content_type=None)
    except Exception:  # noqa: BLE001 - an unreachable browser is just "not ready"
        return None


def _default_spawn(binary: str, args: Sequence[str], log_path: Path) -> int | None:
    """Start a detached browser whose stdout/stderr go to the profile log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        process = subprocess.Popen(  # noqa: S603 - binary is resolved, argv is fixed
            [binary, *args],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env=dict(os.environ),
        )
    return process.pid


class BrowserLauncher:
    """Locate, launch, and report one dedicated automation browser."""

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        home: str | Path | None = None,
        which: Callable[[str], str | None] | None = None,
        spawn: Callable[[str, Sequence[str], Path], int | None] | None = None,
        fetch_json: Callable[[str, int], Any] | None = None,
        platform: str | None = None,
        launch_timeout_ms: int = LAUNCH_TIMEOUT_MS,
        poll_interval_ms: int = POLL_INTERVAL_MS,
    ) -> None:
        self.env: Mapping[str, str] = env if env is not None else os.environ
        self.home = Path(
            home
            if home is not None
            else (self.env.get("HOME") or Path.home())
        ).expanduser()
        self._platform = platform or sys.platform
        self._binary_cache: str | None | object = _UNSET
        path_value = self.env.get("PATH", "")
        self._which = which or (
            lambda name: which_binary(name, path_value, str(self.home), self.env)
        )
        self._spawn = spawn or _default_spawn
        self._fetch_json = fetch_json or _default_fetch_json
        self.launch_timeout_ms = launch_timeout_ms
        self.poll_interval_ms = poll_interval_ms

    # ── Paths ───────────────────────────────────────────────────────────

    @property
    def profile_dir(self) -> Path:
        override = (self.env.get("HERMES_RELAY_BROWSER_PROFILE") or "").strip()
        if override:
            return Path(override).expanduser()
        return _hermes_home(self.env) / "browser-profile"

    @property
    def downloads_dir(self) -> Path:
        return self.profile_dir / "downloads"

    @property
    def log_path(self) -> Path:
        return self.profile_dir / "chromium.log"

    def _assert_dedicated_profile(self) -> None:
        resolved = self.profile_dir.expanduser().resolve(strict=False)
        if resolved == self.home.resolve(strict=False):
            raise BrowserLaunchError(
                "unsafe_profile",
                f"Refusing to use {resolved} as the browser profile: it is the home directory.",
            )
        for relative in _USER_PROFILE_DIRS:
            candidate = (self.home / relative).resolve(strict=False)
            if resolved == candidate or candidate in resolved.parents:
                raise BrowserLaunchError(
                    "unsafe_profile",
                    "Refusing to use a signed-in browser profile "
                    f"({resolved}). Set HERMES_RELAY_BROWSER_PROFILE to a "
                    "dedicated directory such as $HERMES_HOME/browser-profile.",
                )

    # ── Discovery ───────────────────────────────────────────────────────

    async def binary(self) -> str | None:
        if self._binary_cache is not _UNSET:
            return self._binary_cache  # type: ignore[return-value]
        configured = (self.env.get("HERMES_RELAY_BROWSER") or "").strip()
        if configured:
            resolved = self._which(configured)
            if resolved is None and Path(configured).expanduser().is_file():
                resolved = str(Path(configured).expanduser())
            self._binary_cache = resolved
            return resolved

        if self._platform == "win32":
            names: list[str] = _WINDOWS_NAMES
            fixed: list[str] = []
        elif self._platform == "darwin":
            names = _MAC_NAMES
            fixed = _MAC_PATHS
        else:
            names = _LINUX_NAMES
            fixed = []
        for name in names:
            found = self._which(name)
            if found:
                self._binary_cache = found
                return found
        if self._platform == "win32":
            for candidate in _WINDOWS_PATHS:
                if Path(candidate).is_file():
                    self._binary_cache = candidate
                    return candidate
        for candidate in fixed:
            if Path(candidate).is_file():
                self._binary_cache = candidate
                return candidate
        self._binary_cache = None
        return None

    def _read_endpoint(self) -> tuple[int, str] | None:
        try:
            text = (self.profile_dir / DEVTOOLS_PORT_FILE).read_text(encoding="utf-8")
        except OSError:
            return None
        lines = text.strip().splitlines()
        if not lines:
            return None
        try:
            port = int(lines[0].strip())
        except (TypeError, ValueError):
            return None
        path = lines[1].strip() if len(lines) > 1 else ""
        if port <= 0 or port > 65535 or not path.startswith("/"):
            return None
        return port, path

    # ── Status ──────────────────────────────────────────────────────────

    async def status(self) -> BrowserStatus:
        binary = await self.binary()
        try:
            self._assert_dedicated_profile()
        except BrowserLaunchError as exc:
            return BrowserStatus(
                available=bool(binary),
                running=False,
                binary_path=binary,
                profile_dir=str(self.profile_dir),
                reason=str(exc),
            )
        endpoint = self._read_endpoint()
        endpoint_url = f"http://127.0.0.1:{endpoint[0]}" if endpoint else None
        if not binary:
            return BrowserStatus(
                available=False,
                running=False,
                profile_dir=str(self.profile_dir),
                endpoint=endpoint_url,
                reason=(
                    "No Chrome/Chromium/Edge/Brave binary found. Set "
                    "HERMES_RELAY_BROWSER to an absolute path."
                ),
            )
        if not endpoint:
            return BrowserStatus(
                available=True,
                running=False,
                binary_path=binary,
                profile_dir=str(self.profile_dir),
            )
        version = await self._fetch_json(f"{endpoint_url}/json/version", 2_000)
        if not isinstance(version, dict):
            return BrowserStatus(
                available=True,
                running=False,
                binary_path=binary,
                profile_dir=str(self.profile_dir),
                endpoint=endpoint_url,
                reason=(
                    "A DevToolsActivePort file exists but the browser is not "
                    "answering; it will be replaced on launch."
                ),
            )
        return BrowserStatus(
            available=True,
            running=True,
            binary_path=binary,
            profile_dir=str(self.profile_dir),
            endpoint=endpoint_url,
            web_socket_url=self._browser_websocket(version, endpoint),
            browser_version=version.get("Browser") if isinstance(version.get("Browser"), str) else None,
        )

    @staticmethod
    def _browser_websocket(version: Mapping[str, Any], endpoint: tuple[int, str]) -> str:
        value = version.get("webSocketDebuggerUrl")
        if isinstance(value, str) and value:
            return value
        return f"ws://127.0.0.1:{endpoint[0]}{endpoint[1]}"

    # ── Launch ──────────────────────────────────────────────────────────

    def build_args(self, *, headless: bool) -> list[str]:
        args = [
            "--remote-debugging-port=0",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate,MediaRouter",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-extensions",
            "--class=HermesRelayBrowser",
            "about:blank",
        ]
        if headless:
            args.insert(0, "--headless=new")
        extra = [
            part
            for part in (self.env.get("HERMES_RELAY_BROWSER_ARGS") or "").split()
            if part and not _FORBIDDEN_FLAG.match(part)
        ]
        # Chrome refuses to start as root with its sandbox; containers are the
        # common deployment target for the headless client.
        if hasattr(os, "geteuid") and os.geteuid() == 0 and "--no-sandbox" not in extra:
            extra.insert(0, "--no-sandbox")
        if extra:
            args = [*extra, *args]
        return args

    async def ensure(self, *, headless: bool | None = None) -> BrowserLaunchResult:
        self._assert_dedicated_profile()
        current = await self.status()
        if (
            current.running
            and current.endpoint
            and current.web_socket_url
            and current.binary_path
        ):
            return BrowserLaunchResult(
                endpoint=current.endpoint,
                web_socket_url=current.web_socket_url,
                profile_dir=current.profile_dir,
                binary_path=current.binary_path,
                launched=False,
                browser_version=current.browser_version,
            )

        binary = await self.binary()
        if not binary:
            raise BrowserLaunchError(
                "browser_not_found",
                "No Chrome/Chromium/Edge/Brave binary found. Install one or set "
                "HERMES_RELAY_BROWSER to its absolute path.",
            )
        profile = self.profile_dir
        try:
            profile.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.downloads_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise BrowserLaunchError(
                "profile_unavailable", f"Could not create the browser profile at {profile}: {exc}"
            ) from exc
        # A stale port file would make us connect to another process.
        try:
            (profile / DEVTOOLS_PORT_FILE).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.debug("could not remove stale %s", DEVTOOLS_PORT_FILE, exc_info=True)

        if headless is None:
            headless = (self.env.get("HERMES_RELAY_BROWSER_HEADLESS") or "") == "1"
        args = self.build_args(headless=headless)
        try:
            pid = self._spawn(binary, args, self.log_path)
        except OSError as exc:
            raise BrowserLaunchError(
                "launch_failed", f"Could not start the browser at {binary}: {exc}"
            ) from exc

        deadline = asyncio.get_running_loop().time() + self.launch_timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            endpoint = self._read_endpoint()
            if endpoint:
                endpoint_url = f"http://127.0.0.1:{endpoint[0]}"
                version = await self._fetch_json(f"{endpoint_url}/json/version", 1_500)
                if isinstance(version, dict):
                    return BrowserLaunchResult(
                        endpoint=endpoint_url,
                        web_socket_url=self._browser_websocket(version, endpoint),
                        profile_dir=str(profile),
                        binary_path=binary,
                        launched=True,
                        browser_version=version.get("Browser")
                        if isinstance(version.get("Browser"), str)
                        else None,
                        pid=pid if isinstance(pid, int) else None,
                        log_path=str(self.log_path),
                    )
            await asyncio.sleep(self.poll_interval_ms / 1000)
        raise BrowserLaunchError(
            "launch_timeout",
            f"Browser did not expose a DevTools endpoint within {self.launch_timeout_ms}ms.",
        )

    async def list_targets(self, endpoint: str) -> list[dict[str, Any]]:
        targets = await self._fetch_json(f"{endpoint}/json/list", 3_000)
        if not isinstance(targets, list):
            return []
        return [target for target in targets if isinstance(target, dict)]

    async def close_gracefully(self) -> None:
        try:
            self._assert_dedicated_profile()
        except BrowserLaunchError as exc:
            logger.warning("not closing browser: %s", exc)
            return
        status = await self.status()
        if not status.running or not status.endpoint or not status.web_socket_url:
            return
        from .cdp import open_cdp

        connection = None
        try:
            connection = await open_cdp(status.web_socket_url)
            await connection.send("Browser.close", timeout_ms=5_000)
        except Exception:  # noqa: BLE001 - browser may vanish first
            logger.debug("graceful browser close failed", exc_info=True)
        finally:
            if connection is not None:
                await connection.close()

    # ── Downloads ───────────────────────────────────────────────────────

    async def downloads_path(self) -> str:
        self._assert_dedicated_profile()
        try:
            self.downloads_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise BrowserLaunchError(
                "profile_unavailable",
                f"Could not create the download directory at {self.downloads_dir}: {exc}",
            ) from exc
        return str(self.downloads_dir)

    async def last_download(self) -> dict[str, Any] | None:
        try:
            names = list(self.downloads_dir.iterdir())
        except OSError:
            return None
        newest: dict[str, Any] | None = None
        for path in names:
            if path.name.endswith(".crdownload") or path.name.endswith(".part"):
                continue
            try:
                info = path.stat()
            except OSError:
                continue
            if not path.is_file():
                continue
            modified_ms = int(info.st_mtime * 1000)
            if newest is None or modified_ms > newest["modified_ms"]:
                newest = {"path": str(path), "size": info.st_size, "modified_ms": modified_ms}
        return newest


__all__ = [
    "BrowserLaunchError",
    "BrowserLaunchResult",
    "BrowserLauncher",
    "BrowserStatus",
    "DEVTOOLS_PORT_FILE",
    "LAUNCH_TIMEOUT_MS",
]
