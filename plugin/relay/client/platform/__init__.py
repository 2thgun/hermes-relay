"""Platform-adapter selection for the Python relay client.

Selection is driven by the *session environment*, never by the OS alone: a
Linux host can be running Wayland/Hyprland, a plain X11 desktop, or no
graphical session reachable from the service at all.  The explicit
``HERMES_RELAY_DESKTOP_BACKEND`` override (or the ``kind=`` argument) wins, as
in the TypeScript client.

The selection is cached per process because probing spawns ``hyprctl``.  Call
:func:`select_platform_adapter` with ``refresh=True`` after a session switch.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .base import (
    PlatformAdapter,
    UiActionResult,
    UiCapability,
    UiCapture,
    UiCaptureRequest,
    UiDisplay,
    UiEnvironment,
    UiError,
    UiPointerRequest,
    UiWindow,
)
from .command import CommandRunner
from .linux_hyprland import HyprlandAdapter
from .linux_x11 import X11Adapter

AdapterKind = str

_VALID_KINDS = frozenset({"auto", "hyprland", "x11", "windows"})


class UnsupportedAdapter(PlatformAdapter):
    """A descriptive adapter for sessions this phase cannot automate yet."""

    id = "unsupported"
    platform = "linux"

    def __init__(self, platform: str, reason: str, kind: str = "unsupported") -> None:
        self.platform = platform
        self.reason = reason
        self.kind = kind

    async def environment(self) -> UiEnvironment:
        return UiEnvironment(
            platform=self.platform,
            session="none",
            graphical=False,
            reason=self.reason,
            dependencies={},
        )

    async def capabilities(self) -> list[UiCapability]:
        return []

    async def displays(self) -> list[UiDisplay]:
        raise UiError("unsupported_platform", self.reason)

    async def windows(self) -> list[UiWindow]:
        raise UiError("unsupported_platform", self.reason)

    async def focus_window(self, window_id: str) -> UiActionResult:
        raise UiError("unsupported_platform", self.reason)

    async def capture(
        self, request: UiCaptureRequest | Mapping[str, Any]
    ) -> UiCapture:
        raise UiError("unsupported_platform", self.reason)

    async def pointer(
        self, request: UiPointerRequest | Mapping[str, Any]
    ) -> UiActionResult:
        raise UiError("unsupported_platform", self.reason)

    async def type_text(self, text: str, **kwargs: Any) -> UiActionResult:
        raise UiError("unsupported_platform", self.reason)

    async def keys(self, keys: str, **kwargs: Any) -> UiActionResult:
        raise UiError("unsupported_platform", self.reason)


@dataclass
class AdapterSelection:
    adapter: PlatformAdapter
    kind: str
    reason: str


_cached: AdapterSelection | None = None
_cache_key: tuple[str, ...] | None = None


def _make_hyprland(
    env: Mapping[str, str],
    platform: str,
    runner: CommandRunner | None,
    which: Callable[[str], str | None] | None,
    home: str | None,
) -> HyprlandAdapter:
    return HyprlandAdapter(
        runner=runner,
        env=env,
        platform=platform,
        which=which,
        home=home,
    )


def _make_x11(
    env: Mapping[str, str],
    platform: str,
    runner: CommandRunner | None,
    which: Callable[[str], str | None] | None,
    home: str | None,
) -> X11Adapter:
    return X11Adapter(
        runner=runner,
        env=env,
        platform=platform,
        which=which,
        home=home,
    )


async def select_platform_adapter(
    *,
    kind: AdapterKind = "auto",
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    runner: CommandRunner | None = None,
    which: Callable[[str], str | None] | None = None,
    home: str | None = None,
    refresh: bool = False,
) -> AdapterSelection:
    """Choose the adapter for the current session environment."""

    global _cached, _cache_key
    environment = dict(env if env is not None else os.environ)
    platform = platform or sys.platform
    override = (
        kind
        if kind and kind != "auto"
        else (environment.get("HERMES_RELAY_DESKTOP_BACKEND") or "auto")
    )
    override = override.strip().lower()
    if override not in _VALID_KINDS:
        override = "auto"

    signature = (environment.get("HYPRLAND_INSTANCE_SIGNATURE") or "").strip()
    wayland = (environment.get("WAYLAND_DISPLAY") or "").strip()
    display = (environment.get("DISPLAY") or "").strip()
    cacheable = (
        runner is None and which is None and home is None and env is None
    )
    key = (platform, override, signature, wayland, display)
    if cacheable and not refresh and _cached is not None and _cache_key == key:
        return _cached

    selection: AdapterSelection

    if override == "windows" or (override == "auto" and platform == "win32"):
        selection = AdapterSelection(
            UnsupportedAdapter(
                platform,
                "Windows desktop automation is not implemented in the Python "
                "client yet.",
                kind="windows",
            ),
            "windows",
            "windows session (unsupported in this Python phase)",
        )
    elif override == "x11":
        selection = AdapterSelection(
            _make_x11(environment, platform, runner, which, home),
            "x11",
            "explicit X11 selection",
        )
    elif override == "hyprland":
        selection = AdapterSelection(
            _make_hyprland(environment, platform, runner, which, home),
            "hyprland",
            "explicit Hyprland selection",
        )
    elif platform != "linux":
        selection = AdapterSelection(
            UnsupportedAdapter(platform, f"No desktop adapter for {platform} yet."),
            "unsupported",
            "unsupported platform",
        )
    elif signature:
        selection = AdapterSelection(
            _make_hyprland(environment, platform, runner, which, home),
            "hyprland",
            "HYPRLAND_INSTANCE_SIGNATURE",
        )
    elif wayland:
        hyprland = _make_hyprland(environment, platform, runner, which, home)
        report = await hyprland.environment()
        if report.graphical:
            selection = AdapterSelection(
                hyprland,
                "hyprland",
                "hyprctl responded on the Wayland session",
            )
        elif display:
            selection = AdapterSelection(
                _make_x11(environment, platform, runner, which, home),
                "x11",
                "Wayland session is not Hyprland "
                f"({report.reason or 'unknown'}); using X11",
            )
        else:
            selection = AdapterSelection(
                hyprland,
                "hyprland",
                "Wayland session without X11 fallback",
            )
    elif display:
        selection = AdapterSelection(
            _make_x11(environment, platform, runner, which, home),
            "x11",
            "DISPLAY is set",
        )
    else:
        selection = AdapterSelection(
            UnsupportedAdapter(
                platform,
                "No graphical session found (neither WAYLAND_DISPLAY nor "
                "DISPLAY is set). Desktop automation needs a logged-in user "
                "session; start the client as a login service, not as a "
                "system daemon.",
            ),
            "unsupported",
            "no graphical session",
        )

    if cacheable:
        _cached = selection
        _cache_key = key
    return selection


def reset_platform_adapter_for_tests() -> None:
    global _cached, _cache_key
    _cached = None
    _cache_key = None


__all__ = [
    "AdapterSelection",
    "UnsupportedAdapter",
    "reset_platform_adapter_for_tests",
    "select_platform_adapter",
]
