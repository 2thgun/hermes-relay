"""Windows platform adapter -- PowerShell, user32, and System.Drawing.

Windows has no shell-friendly compositor IPC: window discovery is a user32
walk, focus is a thread-attach dance, and capture is GDI.  Every observation
and input therefore goes through PowerShell, and every result reports exactly
what the adapter could verify.

The details that matter on a real 150% panel:

* ``Screen.Bounds`` is the *logical* desktop rectangle while
  ``Win32_VideoController`` reports the current mode's physical pixels.
  ``dpi_scale`` is derived from physical/logical and falls back to
  ``GetDpiForMonitor`` (``AppliedDPI``) so a 2880x1800 panel driven at
  1920x1200 logical is 1.5, never 1.0.
* Display capture is physical-pixel capture: the script opts into DPI
  awareness, the logical display rect is scaled to physical pixels, and the
  result follows the shared ``global_logical = origin + pixel / scale``
  contract.  Mixed-DPI multi-monitor capture is approximate -- Windows does not
  expose per-monitor physical rects through this path, so a warning is attached
  when the scales differ.
* Focus is verified by re-reading the foreground window.  A background process
  cannot normally call ``SetForegroundWindow``; ``AttachThreadInput`` +
  ``BringWindowToTop`` + topmost + ``SetForegroundWindow`` + topmost reset is
  the sequence Windows can honour.  If the foreground did not actually move,
  the call raises ``input_failed`` with the real foreground -- it never returns
  ``ok: true`` on an unverified focus.

Explicitly unavailable in phase 4: window/region/virtual-desktop capture and
pointer drag.  CUA / ``desktop_computer_*`` stays a documented phase-5 seam.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
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
    UiKeyboardRequest,
    UiPoint,
    UiPointerRequest,
    UiRect,
    UiVerification,
    UiWindow,
    coerce_capture_request,
    coerce_keyboard_request,
    coerce_pointer_request,
    display_at,
    image_size,
    iso_now,
    layout_bounds,
    normalize_rect,
)
from .command import CommandRunner, SubprocessRunner, parse_json, which_binary

WINDOW_ID_RE = re.compile(r"^\d+$")
POINTER_ACTIONS = frozenset(
    {"move", "click", "double_click", "right_click", "middle_click", "scroll", "drag"}
)
BUTTON_ACTIONS = {
    "left": "left_click",
    "right": "right_click",
    "middle": "middle_click",
}
MAX_TEXT_CHARS = 2_000
MAX_WHEEL_NOTCHES = 5_000
CAPTURE_TIMEOUT_MS = 20_000

DRAG_REASON = (
    "Windows drag is not exposed by the legacy user32 input bridge; "
    "use a structured CUA action (desktop_computer_action)."
)
WINDOW_CAPTURE_REASON = (
    "Windows window capture is not implemented; only full-display capture is "
    "available through System.Drawing. Use desktop_computer_screenshot for "
    "window capture."
)
REGION_CAPTURE_REASON = (
    "Windows region capture is not implemented; only full-display capture is "
    "available through System.Drawing. Use desktop_computer_screenshot for "
    "region capture, or capture the display and crop it."
)
ALL_CAPTURE_REASON = (
    "Windows virtual-desktop capture is not implemented; request a specific "
    "display instead."
)

# SendKeys names accepted by the legacy bridge.  Keys are case-insensitive and
# whitespace is ignored, matching the TypeScript implementation.
KEY_ALIASES: dict[str, str] = {
    "enter": "{ENTER}",
    "return": "{ENTER}",
    "tab": "{TAB}",
    "escape": "{ESC}",
    "esc": "{ESC}",
    "backspace": "{BACKSPACE}",
    "delete": "{DELETE}",
    "del": "{DELETE}",
    "insert": "{INSERT}",
    "ins": "{INSERT}",
    "up": "{UP}",
    "down": "{DOWN}",
    "left": "{LEFT}",
    "right": "{RIGHT}",
    "home": "{HOME}",
    "end": "{END}",
    "pageup": "{PGUP}",
    "pgup": "{PGUP}",
    "pagedown": "{PGDN}",
    "pgdn": "{PGDN}",
    "space": " ",
    "printscreen": "{PRTSC}",
    "ctrlc": "^c",
    "ctrl+c": "^c",
    "ctrlv": "^v",
    "ctrl+v": "^v",
    "ctrla": "^a",
    "ctrl+a": "^a",
}

_MODIFIERS = {
    "ctrl": "^",
    "control": "^",
    "shift": "+",
    "alt": "%",
}
_SENDKEYS_SPECIAL = re.compile(r"([+^%~(){}\[\]])")
_FUNCTION_KEY = re.compile(r"^f([1-9]|1[0-9]|2[0-4])$")


def escape_send_keys_text(text: str) -> str:
    """Escape User32 ``SendKeys`` metacharacters in literal text."""

    return _SENDKEYS_SPECIAL.sub(r"{\1}", text)


def normalize_keys(value: str) -> str | None:
    """Translate ``Enter``/``ctrl+shift+s`` into ``SendKeys`` notation."""

    compact = "".join(str(value or "").split())
    if not compact:
        return None
    lowered = compact.lower()
    if lowered in KEY_ALIASES:
        return KEY_ALIASES[lowered]

    parts = [part for part in lowered.split("+") if part]
    if len(parts) >= 2:
        *modifiers, key = parts
        prefix = ""
        for modifier in modifiers:
            if modifier not in _MODIFIERS:
                return None
            prefix += _MODIFIERS[modifier]
        token = KEY_ALIASES.get(key)
        if token is None and _FUNCTION_KEY.fullmatch(key):
            token = "{" + key.upper() + "}"
        if token is None and len(key) == 1 and key.isalnum():
            token = key
        if token is None:
            return None
        return prefix + token

    if _FUNCTION_KEY.fullmatch(lowered):
        return "{" + lowered.upper() + "}"
    if len(compact) == 1:
        return escape_send_keys_text(compact)
    return None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _int_or(value: Any, fallback: int = 0) -> int:
    if _is_number(value):
        return round(value)
    if isinstance(value, str):
        try:
            return round(float(value.strip()))
        except (TypeError, ValueError):
            return fallback
    return fallback


def _rows(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, Mapping):
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    return []


def _dpi_scale(row: Mapping[str, Any]) -> float:
    """Derive the OS scale factor from physical/logical pixels or AppliedDPI.

    A DPI-unaware PowerShell reports ``Screen.Bounds`` in logical pixels, so
    physical/logical is the scale (2880/1920 = 1.5).  A DPI-aware host may
    report physical bounds too; then the ratio is 1.0 and ``GetDpiForMonitor``
    is the only honest source.  Never collapse a 150% panel to 1.0.
    """

    logical_width = _int_or(row.get("width"))
    logical_height = _int_or(row.get("height"))
    physical_width = _int_or(row.get("physical_width"))
    physical_height = _int_or(row.get("physical_height"))
    ratio: float | None = None
    if logical_width > 0 and physical_width > 0:
        ratio = physical_width / logical_width
        if logical_height > 0 and physical_height > 0:
            vertical = physical_height / logical_height
            if abs(vertical - ratio) <= 0.05:
                ratio = (ratio + vertical) / 2

    dpi = _int_or(row.get("dpi"))
    dpi_ratio = dpi / 96 if dpi > 0 else None

    if ratio is not None and ratio >= 1.02:
        scale = ratio
    elif dpi_ratio is not None:
        scale = dpi_ratio
    elif ratio is not None and ratio > 0:
        scale = ratio
    else:
        scale = 1.0
    return round(scale, 2)


# ── PowerShell scripts ──────────────────────────────────────────────────────
#
# Dynamic values travel two ways:
#   * structured payloads as base64-encoded JSON (never interpolated), so text,
#     titles, and coordinates cannot break the command;
#   * the capture output path as a single-quoted PowerShell literal produced by
#     ``_ps_literal`` (``'`` doubled), because a path is the one value the
#     script writes to disk itself.

_DISPLAY_SCRIPT = r"""
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class RelayWindowsDisplay {
  [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X; public int Y; }
  [DllImport("user32.dll")] public static extern IntPtr GetDesktopWindow();
  [DllImport("user32.dll")] public static extern IntPtr MonitorFromWindow(IntPtr hWnd, uint flags);
  [DllImport("user32.dll")] public static extern IntPtr MonitorFromPoint(POINT point, uint flags);
  [DllImport("Shcore.dll")] public static extern int GetDpiForMonitor(IntPtr hWnd, int dpiType, out uint dpiX, out uint dpiY);
}
"@
[uint32]$dpi = 96
try {
  $monitor = [RelayWindowsDisplay]::MonitorFromWindow([RelayWindowsDisplay]::GetDesktopWindow(), 1)
  [uint32]$dx = 0
  [uint32]$dy = 0
  if ([RelayWindowsDisplay]::GetDpiForMonitor($monitor, 0, [ref]$dx, [ref]$dy) -eq 0 -and $dx -gt 0) {
    $dpi = $dx
  }
} catch {}
$controller = $null
try {
  $controller = Get-CimInstance Win32_VideoController -ErrorAction Stop |
    Where-Object { $_.CurrentHorizontalResolution -gt 0 } |
    Select-Object -First 1
} catch { $controller = $null }
$physicalWidth = 0
$physicalHeight = 0
if ($controller) {
  $physicalWidth = [int]$controller.CurrentHorizontalResolution
  $physicalHeight = [int]$controller.CurrentVerticalResolution
}
$rows = @()
foreach ($screen in [System.Windows.Forms.Screen]::AllScreens) {
  $screenDpi = $dpi
  try {
    $point = [RelayWindowsDisplay+POINT]::new()
    $point.X = [int]($screen.Bounds.X + [Math]::Floor($screen.Bounds.Width / 2))
    $point.Y = [int]($screen.Bounds.Y + [Math]::Floor($screen.Bounds.Height / 2))
    $screenMonitor = [RelayWindowsDisplay]::MonitorFromPoint($point, 2)
    [uint32]$sx = 0
    [uint32]$sy = 0
    if ([RelayWindowsDisplay]::GetDpiForMonitor($screenMonitor, 0, [ref]$sx, [ref]$sy) -eq 0 -and $sx -gt 0) {
      $screenDpi = $sx
    }
  } catch {}
  $rows += [pscustomobject]@{
    name = [string]$screen.DeviceName
    primary = [bool]$screen.Primary
    x = [int]$screen.Bounds.X
    y = [int]$screen.Bounds.Y
    width = [int]$screen.Bounds.Width
    height = [int]$screen.Bounds.Height
    # Win32_VideoController describes the primary adapter's current mode; it
    # is not per-monitor metadata, so only trust it for the primary screen.
    physical_width = $(if ($screen.Primary) { $physicalWidth } else { 0 })
    physical_height = $(if ($screen.Primary) { $physicalHeight } else { 0 })
    dpi = [int]$screenDpi
  }
}
ConvertTo-Json -InputObject @($rows) -Compress -Depth 3
""".strip()

_WINDOW_SCRIPT = r"""
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$ErrorActionPreference = 'Stop'
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class RelayWindowsList {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
}
"@
$foreground = [RelayWindowsList]::GetForegroundWindow()
$rows = @()
foreach ($process in @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 })) {
  try {
    $handle = $process.MainWindowHandle
    if (-not [RelayWindowsList]::IsWindowVisible($handle)) { continue }
    $rect = [RelayWindowsList+RECT]::new()
    if (-not [RelayWindowsList]::GetWindowRect($handle, [ref]$rect)) { continue }
    $width = [int]($rect.Right - $rect.Left)
    $height = [int]($rect.Bottom - $rect.Top)
    if ($width -le 0 -or $height -le 0) { continue }
    $rows += [pscustomobject]@{
      id = [int]$process.Id
      app = [string]$process.ProcessName
      title = [string]$process.MainWindowTitle
      x = [int]$rect.Left
      y = [int]$rect.Top
      width = $width
      height = $height
      focused = [bool]($handle.ToInt64() -eq $foreground.ToInt64())
      visible = $true
    }
  } catch { continue }
}
ConvertTo-Json -InputObject @($rows) -Compress -Depth 3
""".strip()

_FOCUS_SCRIPT = r"""
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$ErrorActionPreference = 'Stop'
$payloadJson = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('__PAYLOAD__'))
$payload = $payloadJson | ConvertFrom-Json
$process = Get-Process -Id ([int]$payload.pid) -ErrorAction Stop
$handle = $process.MainWindowHandle
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class RelayWindowsFocus {
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int command);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr after, int x, int y, int width, int height, uint flags);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint attach, uint attachTo, bool doAttach);
}
"@
$activated = $false
if ($handle.ToInt64() -ne 0) {
  [uint32]$foregroundPid = 0
  $foregroundThread = [RelayWindowsFocus]::GetWindowThreadProcessId([RelayWindowsFocus]::GetForegroundWindow(), [ref]$foregroundPid)
  [uint32]$targetPid = 0
  $targetThread = [RelayWindowsFocus]::GetWindowThreadProcessId($handle, [ref]$targetPid)
  [void][RelayWindowsFocus]::ShowWindow($handle, 9)
  [void][RelayWindowsFocus]::AttachThreadInput($foregroundThread, $targetThread, $true)
  [void][RelayWindowsFocus]::BringWindowToTop($handle)
  [void][RelayWindowsFocus]::SetWindowPos($handle, [IntPtr](-1), 0, 0, 0, 0, 0x0001 -bor 0x0002 -bor 0x0040)
  $activated = [RelayWindowsFocus]::SetForegroundWindow($handle)
  [void][RelayWindowsFocus]::AttachThreadInput($foregroundThread, $targetThread, $false)
  Start-Sleep -Milliseconds 150
  [void][RelayWindowsFocus]::SetWindowPos($handle, [IntPtr](-2), 0, 0, 0, 0, 0x0001 -bor 0x0002)
}
if (-not $activated) {
  try {
    $shell = New-Object -ComObject WScript.Shell
    $activated = [bool]$shell.AppActivate([int]$payload.pid)
  } catch { $activated = $false }
  Start-Sleep -Milliseconds 75
}
$after = [RelayWindowsFocus]::GetForegroundWindow()
[uint32]$afterPid = 0
[void][RelayWindowsFocus]::GetWindowThreadProcessId($after, [ref]$afterPid)
[pscustomobject]@{
  activated = [bool]$activated
  foreground_pid = [int]$afterPid
  foreground_handle = $after.ToInt64()
} | ConvertTo-Json -Compress
""".strip()

_CAPTURE_SCRIPT = r"""
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class RelayWindowsCapture {
  [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr context);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
}
"@
$dpiAware = $false
try { $dpiAware = [RelayWindowsCapture]::SetProcessDpiAwarenessContext([IntPtr](-4)) } catch {}
if (-not $dpiAware) { try { [void][RelayWindowsCapture]::SetProcessDPIAware() } catch {} }
$outPath = __PATH__
$bitmap = [System.Drawing.Bitmap]::new([int]__WIDTH__, [int]__HEIGHT__)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
try {
  $graphics.CopyFromScreen([int]__X__, [int]__Y__, 0, 0, $bitmap.Size)
} finally {
  $graphics.Dispose()
}
try {
__SAVE__
} finally {
  $bitmap.Dispose()
}
[pscustomobject]@{
  ok = $true
  path = $outPath
  width = [int]__WIDTH__
  height = [int]__HEIGHT__
} | ConvertTo-Json -Compress
""".strip()

_POINTER_SCRIPT = r"""
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$ErrorActionPreference = 'Stop'
$payloadJson = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('__PAYLOAD__'))
$payload = $payloadJson | ConvertFrom-Json
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class RelayWindowsPointer {
  [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X; public int Y; }
  [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr context);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT point);
  [DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint dx, uint dy, int data, UIntPtr extraInfo);
}
"@
$dpiAware = $false
try { $dpiAware = [RelayWindowsPointer]::SetProcessDpiAwarenessContext([IntPtr](-4)) } catch {}
if (-not $dpiAware) { try { [void][RelayWindowsPointer]::SetProcessDPIAware() } catch {} }
$moved = [RelayWindowsPointer]::SetCursorPos([int]$payload.x, [int]$payload.y)
if (-not $moved) {
  [pscustomobject]@{ ok = $false; error = 'SetCursorPos failed' } | ConvertTo-Json -Compress
  exit 0
}
Start-Sleep -Milliseconds 40
$LEFTDOWN = 0x0002
$LEFTUP = 0x0004
$RIGHTDOWN = 0x0008
$RIGHTUP = 0x0010
$WHEEL = 0x0800
$HWHEEL = 0x01000
switch ([string]$payload.action) {
  'move' {}
  'left_click' {
    for ($i = 0; $i -lt [int]$payload.count; $i++) {
      [RelayWindowsPointer]::mouse_event($LEFTDOWN, 0, 0, 0, [UIntPtr]::Zero)
      Start-Sleep -Milliseconds 40
      [RelayWindowsPointer]::mouse_event($LEFTUP, 0, 0, 0, [UIntPtr]::Zero)
      Start-Sleep -Milliseconds 30
    }
  }
  'right_click' {
    [RelayWindowsPointer]::mouse_event($RIGHTDOWN, 0, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 40
    [RelayWindowsPointer]::mouse_event($RIGHTUP, 0, 0, 0, [UIntPtr]::Zero)
  }
  'middle_click' {
    [RelayWindowsPointer]::mouse_event(0x0020, 0, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 40
    [RelayWindowsPointer]::mouse_event(0x0040, 0, 0, 0, [UIntPtr]::Zero)
  }
  'double_click' {
    for ($i = 0; $i -lt 2; $i++) {
      [RelayWindowsPointer]::mouse_event($LEFTDOWN, 0, 0, 0, [UIntPtr]::Zero)
      Start-Sleep -Milliseconds 35
      [RelayWindowsPointer]::mouse_event($LEFTUP, 0, 0, 0, [UIntPtr]::Zero)
      Start-Sleep -Milliseconds 80
    }
  }
  'scroll' {
    if ([int]$payload.scroll_y -ne 0) {
      [RelayWindowsPointer]::mouse_event($WHEEL, 0, 0, [int]$payload.scroll_y, [UIntPtr]::Zero)
    }
    if ([int]$payload.scroll_x -ne 0) {
      [RelayWindowsPointer]::mouse_event($HWHEEL, 0, 0, [int]$payload.scroll_x, [UIntPtr]::Zero)
    }
  }
  default { throw "unsupported pointer action: $($payload.action)" }
}
Start-Sleep -Milliseconds 30
$point = [RelayWindowsPointer+POINT]::new()
[void][RelayWindowsPointer]::GetCursorPos([ref]$point)
[pscustomobject]@{
  ok = $true
  cursor_x = [int]$point.X
  cursor_y = [int]$point.Y
} | ConvertTo-Json -Compress
""".strip()

_KEYBOARD_SCRIPT = r"""
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$ErrorActionPreference = 'Stop'
$payloadJson = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('__PAYLOAD__'))
$payload = $payloadJson | ConvertFrom-Json
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class RelayWindowsKeyboard {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
}
"@
$foreground = [RelayWindowsKeyboard]::GetForegroundWindow()
[uint32]$foregroundPid = 0
[void][RelayWindowsKeyboard]::GetWindowThreadProcessId($foreground, [ref]$foregroundPid)
if ($foreground.ToInt64() -eq 0 -or $foregroundPid -eq 0) {
  [pscustomobject]@{ ok = $false; code = 'no_foreground'; error = 'No foreground window is available.' } | ConvertTo-Json -Compress
  exit 0
}
if ($null -ne $payload.expected_pid -and [int]$foregroundPid -ne [int]$payload.expected_pid) {
  [pscustomobject]@{
    ok = $false
    code = 'focus_lost'
    foreground_pid = [int]$foregroundPid
    error = 'The foreground window changed before typing.'
  } | ConvertTo-Json -Compress
  exit 0
}
$foregroundProcess = Get-Process -Id ([int]$foregroundPid) -ErrorAction SilentlyContinue
$foregroundApp = 'unknown'
$foregroundTitle = ''
if ($foregroundProcess) {
  try {
    $foregroundApp = [string]$foregroundProcess.ProcessName
    $foregroundTitle = [string]$foregroundProcess.MainWindowTitle
  } catch {}
}
if ([string]$payload.text) { [System.Windows.Forms.SendKeys]::SendWait([string]$payload.text) }
if ([string]$payload.keys) { [System.Windows.Forms.SendKeys]::SendWait([string]$payload.keys) }
[uint32]$afterPid = 0
[void][RelayWindowsKeyboard]::GetWindowThreadProcessId([RelayWindowsKeyboard]::GetForegroundWindow(), [ref]$afterPid)
[pscustomobject]@{
  ok = $true
  foreground_pid = [int]$foregroundPid
  foreground_app = $foregroundApp
  foreground_title = $foregroundTitle
  after_pid = [int]$afterPid
} | ConvertTo-Json -Compress
""".strip()


class WindowsAdapter(PlatformAdapter):
    """Windows desktop adapter backed by PowerShell + user32."""

    id = "windows"
    platform = "win32"

    def __init__(
        self,
        runner: CommandRunner | None = None,
        env: Mapping[str, str] | None = None,
        platform: str = "win32",
        which: Callable[[str], str | None] | None = None,
        sleep: Callable[[float], Any] | None = None,
        temp_dir: str | None = None,
    ) -> None:
        self.runner = runner or SubprocessRunner()
        self.env = dict(env if env is not None else os.environ)
        self.platform = platform
        self._which = which or (
            lambda name: which_binary(
                name,
                self.env.get("PATH"),
                self.env.get("USERPROFILE") or self.env.get("HOME") or os.path.expanduser("~"),
                self.env,
            )
        )
        self._sleep = sleep or asyncio.sleep
        self._temp_dir = temp_dir
        self._binary_cache: dict[str, str | None] = {}
        self._powershell: str | None = None
        self._powershell_checked = False

    # ── Discovery ────────────────────────────────────────────────────────

    def _binary(self, name: str) -> str | None:
        if name not in self._binary_cache:
            self._binary_cache[name] = self._which(name)
        return self._binary_cache[name]

    def _shell_path(self) -> str | None:
        if not self._powershell_checked:
            self._powershell_checked = True
            candidates = [self._binary("powershell"), self._binary("pwsh")]
            # A service often starts with a minimal PATH; Windows PowerShell
            # always lives under SystemRoot, so probe it directly.
            system_root = (
                self.env.get("SystemRoot")
                or self.env.get("WINDIR")
                or r"C:\Windows"
            )
            fallback = os.path.join(
                system_root,
                "System32",
                "WindowsPowerShell",
                "v1.0",
                "powershell.exe",
            )
            if os.path.isfile(fallback):
                candidates.append(fallback)
            self._powershell = next(
                (candidate for candidate in candidates if candidate), None
            )
        return self._powershell

    def _shell(self) -> str:
        shell = self._shell_path()
        if not shell:
            raise UiError(
                "missing_dependency",
                "PowerShell is not available (powershell.exe or pwsh not found).",
            )
        return shell

    async def _run_script(self, script: str, *, timeout_ms: int = 15_000):
        result = await self.runner.text(
            self._shell(),
            [
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            timeout_ms=timeout_ms,
            env=self.env,
        )
        if not result.ran:
            raise UiError(
                "missing_dependency", "PowerShell could not be started."
            )
        return result

    @staticmethod
    def _json(result: Any) -> Any:
        return parse_json(result.text().lstrip("\ufeff").strip())

    @staticmethod
    def _ps_literal(value: str) -> str:
        """Single-quote a PowerShell literal; ``'`` is escaped by doubling."""

        return "'" + str(value).replace("'", "''") + "'"

    @staticmethod
    def _payload(value: Mapping[str, Any]) -> str:
        raw = json.dumps(
            dict(value), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    # ── Environment and capabilities ─────────────────────────────────────

    async def environment(self) -> UiEnvironment:
        shell = self._shell_path()
        graphical = self.platform == "win32" and bool(shell)
        reason: str | None = None
        if self.platform != "win32":
            reason = f"Windows adapter requires win32 (platform={self.platform})."
        elif not shell:
            reason = "PowerShell is not available."
        session_name = (self.env.get("SESSIONNAME") or "").strip()
        warnings: list[str] = []
        if self.platform == "win32" and session_name.lower() == "services":
            warnings.append(
                "SESSIONNAME=Services: the process is in session 0. user32 "
                "capture and input need an interactive user session; run the "
                "client as a per-user login service."
            )
        return UiEnvironment(
            platform=self.platform,
            session="windows" if self.platform == "win32" else "none",
            compositor="win32" if self.platform == "win32" else None,
            desktop=self.env.get("SESSIONNAME"),
            graphical=graphical,
            reason=reason,
            dependencies={
                "powershell": {"available": bool(shell), "path": shell},
            },
            warnings=warnings,
        )

    async def capabilities(self) -> list[UiCapability]:
        environment = await self.environment()
        graphical = environment.graphical
        reason = None if graphical else environment.reason
        return [
            UiCapability(
                "screen.capture", graphical, "powershell+system.drawing", reason
            ),
            UiCapability("windows.list", graphical, "powershell+user32", reason),
            UiCapability("windows.focus", graphical, "powershell+user32", reason),
            UiCapability("pointer.move", graphical, "powershell+user32", reason),
            UiCapability("pointer.click", graphical, "powershell+user32", reason),
            UiCapability("pointer.drag", False, None, DRAG_REASON),
            UiCapability("pointer.scroll", graphical, "powershell+user32", reason),
            UiCapability("keyboard.type", graphical, "powershell+sendkeys", reason),
            UiCapability("keyboard.keys", graphical, "powershell+sendkeys", reason),
        ]

    # ── Displays ─────────────────────────────────────────────────────────

    async def displays(self) -> list[UiDisplay]:
        result = await self._run_script(_DISPLAY_SCRIPT)
        if result.exit_code != 0:
            raise UiError(
                "compositor_error",
                "Display enumeration failed: "
                f"{result.stderr.strip()[:300] or result.exit_code}",
            )
        parsed = self._json(result)
        if parsed is None:
            raise UiError(
                "compositor_error",
                "Could not read Windows display geometry.",
            )
        rows = _rows(parsed)
        primary_row = next(
            (row for row in rows if row.get("primary") is True),
            rows[0] if rows else None,
        )
        # A DPI-aware PowerShell host reports Screen.Bounds in physical pixels:
        # physical/logical is 1.0 while AppliedDPI still says 1.5.  Normalize
        # those rects back to logical coordinates so the pointer/capture
        # contract (global_logical = origin + pixel / scale) stays true.
        aware_bounds = False
        primary_scale = 1.0
        if primary_row is not None:
            primary_logical = _int_or(primary_row.get("width"))
            primary_physical = _int_or(primary_row.get("physical_width"))
            primary_scale = _dpi_scale(primary_row)
            aware_bounds = (
                primary_logical > 0
                and primary_physical > 0
                and abs(primary_physical / primary_logical - 1.0) <= 0.02
                and primary_scale > 1.02
            )

        displays: list[UiDisplay] = []
        for index, row in enumerate(rows):
            name = str(row.get("name") or f"display-{index}")
            scale = _dpi_scale(row)
            width = max(0, _int_or(row.get("width")))
            height = max(0, _int_or(row.get("height")))
            x = _int_or(row.get("x"))
            y = _int_or(row.get("y"))
            if aware_bounds and primary_scale > 1.02:
                width = round(width / scale) if scale > 1.02 else width
                height = round(height / scale) if scale > 1.02 else height
                x = round(x / primary_scale)
                y = round(y / primary_scale)
            physical_width = _int_or(row.get("physical_width"))
            physical_height = _int_or(row.get("physical_height"))
            primary = row.get("primary") is True
            displays.append(
                UiDisplay(
                    id=name,
                    name=name,
                    rect=UiRect(
                        x,
                        y,
                        width,
                        height,
                    ),
                    scale=scale,
                    dpi_scale=scale,
                    physical_width=physical_width if physical_width > 0 else None,
                    physical_height=physical_height if physical_height > 0 else None,
                    primary=primary,
                    focused=primary,
                )
            )
        return displays

    # ── Windows ──────────────────────────────────────────────────────────

    async def windows(self) -> list[UiWindow]:
        result = await self._run_script(_WINDOW_SCRIPT)
        if result.exit_code != 0:
            raise UiError(
                "compositor_error",
                "Window enumeration failed: "
                f"{result.stderr.strip()[:300] or result.exit_code}",
            )
        parsed = self._json(result)
        if parsed is None:
            if not result.text().strip():
                return []
            raise UiError(
                "compositor_error",
                "Window enumeration returned unparseable output.",
            )
        windows: list[UiWindow] = []
        for row in _rows(parsed):
            pid = row.get("id")
            if not isinstance(pid, int) or isinstance(pid, bool):
                continue
            width = _int_or(row.get("width"))
            height = _int_or(row.get("height"))
            if width <= 0 or height <= 0:
                continue
            if row.get("visible") is False:
                continue
            windows.append(
                UiWindow(
                    id=str(pid),
                    title=str(row.get("title") or ""),
                    app=str(row.get("app") or "unknown"),
                    pid=pid,
                    rect=UiRect(
                        _int_or(row.get("x")),
                        _int_or(row.get("y")),
                        width,
                        height,
                    ),
                    focused=row.get("focused") is True,
                    visible=True,
                )
            )
        return windows

    async def focus_window(self, window_id: str) -> UiActionResult:
        started = time.monotonic()
        normalized_id = str(window_id or "")
        if not WINDOW_ID_RE.fullmatch(normalized_id):
            raise UiError(
                "invalid_request",
                "Windows window ids are process ids from desktop_ui_windows.",
            )
        target = next(
            (
                window
                for window in await self.windows()
                if window.id == normalized_id
            ),
            None,
        )
        if target is None:
            raise UiError(
                "window_not_found",
                f"No window with process id {normalized_id}.",
            )

        script = _FOCUS_SCRIPT.replace(
            "__PAYLOAD__", self._payload({"pid": int(normalized_id)})
        )
        result = await self._run_script(script, timeout_ms=20_000)
        if result.exit_code != 0:
            raise UiError(
                "input_failed",
                f"AppActivate failed for {target.app}: "
                f"{result.stderr.strip()[:300] or result.exit_code}",
            )
        payload = self._json(result)
        activated = (
            isinstance(payload, Mapping) and payload.get("activated") is True
        )
        detail = (
            "window not found"
            if not isinstance(payload, Mapping)
            else str(payload.get("error") or "activation was refused")
        )

        await self._sleep(0.25)
        windows = await self.windows()
        active = next(
            (window for window in windows if window.id == normalized_id), None
        )
        if active is not None and active.focused:
            return UiActionResult(
                ok=True,
                backend="powershell+user32",
                action="focus_window",
                performed_at=iso_now(),
                duration_ms=int((time.monotonic() - started) * 1000),
                verification=UiVerification(
                    method="active_window",
                    ok=True,
                    active_window={
                        "id": active.id,
                        "app": active.app,
                        "title": active.title,
                    },
                ),
            )

        if not activated:
            # SetForegroundWindow/AppActivate both refused.  The re-read above
            # proved the target is not the foreground, so this is a real
            # failure rather than a false negative.
            raise UiError(
                "input_failed",
                f"AppActivate failed for {target.app}: {detail}",
            )

        foreground = next(
            (window for window in windows if window.focused), None
        )
        description = (
            f"{foreground.app} ({foreground.title})"
            if foreground is not None
            else "unknown"
        )
        raise UiError(
            "input_failed",
            f"Focus did not move to {target.app} ({target.title}); "
            f"foreground is {description}. Windows may be blocking a "
            "background process from changing the foreground.",
        )

    # ── Capture ──────────────────────────────────────────────────────────

    async def capture(
        self, request: UiCaptureRequest | Mapping[str, Any]
    ) -> UiCapture:
        request = coerce_capture_request(request)
        target = request.target or "display"
        if target not in ("display", "window", "region", "all"):
            raise UiError(
                "invalid_request",
                f"Unknown capture target {target!r}; use display, window, "
                "region, or all.",
            )
        fmt = request.format if request.format in ("png", "jpeg") else None
        if fmt is None:
            raise UiError(
                "invalid_request", f"Unknown capture format {request.format!r}."
            )

        if target == "window":
            if not request.window_id:
                raise UiError(
                    "invalid_request", "target=window requires window_id."
                )
            raise UiError("capability_unavailable", WINDOW_CAPTURE_REASON)
        if target == "region":
            if normalize_rect(request.region) is None:
                raise UiError(
                    "invalid_request",
                    "target=region requires {x,y,width,height} with positive "
                    "width and height.",
                )
            raise UiError("capability_unavailable", REGION_CAPTURE_REASON)
        if target == "all":
            raise UiError("capability_unavailable", ALL_CAPTURE_REASON)

        displays = await self.displays()
        if not displays:
            raise UiError(
                "no_graphical_session", "No Windows displays are available."
            )
        if request.display:
            display = next(
                (
                    item
                    for item in displays
                    if item.name == request.display or item.id == request.display
                ),
                None,
            )
            if display is None:
                raise UiError(
                    "invalid_request", f"Unknown display {request.display}."
                )
        else:
            display = next(
                (item for item in displays if item.primary), displays[0]
            )

        scale = float(display.dpi_scale or display.scale or 1.0)
        # Capture in physical pixels: origin + pixel / scale == logical point.
        physical_x = round(display.rect.x * scale)
        physical_y = round(display.rect.y * scale)
        physical_width = display.physical_width or round(display.rect.width * scale)
        physical_height = (
            display.physical_height or round(display.rect.height * scale)
        )
        if physical_width <= 0 or physical_height <= 0:
            raise UiError(
                "capture_failed",
                f"Display {display.name} has no capturable size "
                f"({physical_width}x{physical_height}).",
            )

        warnings: list[str] = []
        scales = {float(item.dpi_scale or item.scale or 1.0) for item in displays}
        if len(scales) > 1:
            warnings.append(
                "Mixed-DPI displays are present; capture coordinates use the "
                f"target display's {scale:g}x scale and may be offset on other "
                "monitors."
            )

        temp_dir = self._temp_dir or tempfile.gettempdir()
        try:
            os.makedirs(temp_dir, exist_ok=True)
        except OSError as exc:
            raise UiError(
                "capture_failed", f"Could not create {temp_dir}: {exc}"
            ) from exc
        extension = "jpg" if fmt == "jpeg" else "png"
        path = os.path.join(
            temp_dir, f"hermes-relay-capture-{uuid.uuid4().hex}.{extension}"
        )
        if "\x00" in path:
            raise UiError("invalid_request", "The capture path contains a NUL byte.")

        save_block = self._save_block(fmt, request.quality)
        script = (
            _CAPTURE_SCRIPT.replace("__PATH__", self._ps_literal(path))
            .replace("__X__", str(physical_x))
            .replace("__Y__", str(physical_y))
            .replace("__WIDTH__", str(int(physical_width)))
            .replace("__HEIGHT__", str(int(physical_height)))
            .replace("__SAVE__", save_block)
        )

        payload_bytes: bytes | None = None
        try:
            result = await self._run_script(
                script, timeout_ms=CAPTURE_TIMEOUT_MS
            )
            if result.exit_code != 0:
                raise UiError(
                    "capture_failed",
                    "Windows display capture failed: "
                    f"{result.stderr.strip()[:300] or result.exit_code}",
                )
            metadata = self._json(result)
            if not isinstance(metadata, Mapping) or metadata.get("ok") is not True:
                detail = (
                    metadata.get("error")
                    if isinstance(metadata, Mapping)
                    else "unparseable capture metadata"
                )
                raise UiError(
                    "capture_failed",
                    f"Windows display capture failed: {detail}",
                )
            try:
                payload_bytes = Path(path).read_bytes()
            except OSError as exc:
                raise UiError(
                    "capture_failed",
                    f"PowerShell did not write the capture file: {exc}",
                ) from exc
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

        if not payload_bytes:
            raise UiError(
                "capture_failed", "Windows display capture returned no bytes."
            )
        size = image_size(fmt, payload_bytes)
        if size is None:
            raise UiError(
                "capture_failed",
                "Capture did not contain a readable image header.",
            )
        if size != (int(physical_width), int(physical_height)):
            warnings.append(
                f"Captured {size[0]}x{size[1]} but the display reports "
                f"{physical_width}x{physical_height} at dpi_scale {scale:g}; "
                "Windows may be virtualizing this process's DPI."
            )

        saved_path: str | None = None
        if request.save_to:
            saved_path = request.save_to
            if "\x00" in saved_path:
                raise UiError(
                    "invalid_request", "save_to contains a NUL byte."
                )
            try:
                parent = os.path.dirname(saved_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                Path(saved_path).write_bytes(payload_bytes)
            except OSError as exc:
                raise UiError(
                    "capture_failed", f"could not write {saved_path}: {exc}"
                ) from exc

        return UiCapture(
            format=fmt,
            bytes_base64=(
                None
                if saved_path
                else base64.b64encode(payload_bytes).decode("ascii")
            ),
            saved_path=saved_path,
            size_bytes=len(payload_bytes),
            pixel_width=size[0],
            pixel_height=size[1],
            origin=UiPoint(display.rect.x, display.rect.y),
            scale=scale,
            dpi_scale=scale,
            physical_width=int(physical_width),
            physical_height=int(physical_height),
            display=display.name,
            target="display",
            warnings=warnings,
        )

    @staticmethod
    def _save_block(fmt: str, quality: int | None) -> str:
        if fmt == "png":
            return (
                "  $bitmap.Save($outPath, "
                "[System.Drawing.Imaging.ImageFormat]::Png)"
            )
        level = min(100, max(1, int(quality or 90)))
        return "\n".join(
            [
                "  $codec = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() |",
                "    Where-Object { $_.MimeType -eq 'image/jpeg' } |",
                "    Select-Object -First 1",
                "  $encoderParameters = [System.Drawing.Imaging.EncoderParameters]::new(1)",
                (
                    "  $encoderParameters.Param[0] = "
                    "[System.Drawing.Imaging.EncoderParameter]::new("
                    "[System.Drawing.Imaging.Encoder]::Quality, [long]"
                    f"{level})"
                ),
                "  if ($codec) {",
                "    $bitmap.Save($outPath, $codec, $encoderParameters)",
                "  } else {",
                (
                    "    $bitmap.Save($outPath, "
                    "[System.Drawing.Imaging.ImageFormat]::Jpeg)"
                ),
                "  }",
            ]
        )

    # ── Pointer ──────────────────────────────────────────────────────────

    async def _layout(self) -> tuple[list[UiDisplay], UiRect | None]:
        displays = await self.displays()
        return displays, layout_bounds(displays)

    @staticmethod
    def _physical_point(display: UiDisplay, x: int, y: int) -> tuple[int, int]:
        scale = float(display.dpi_scale or display.scale or 1.0)
        return (
            round(display.rect.x + (x - display.rect.x) * scale),
            round(display.rect.y + (y - display.rect.y) * scale),
        )

    @staticmethod
    def _logical_point(display: UiDisplay, x: int, y: int) -> tuple[int, int]:
        scale = float(display.dpi_scale or display.scale or 1.0) or 1.0
        return (
            round(display.rect.x + (x - display.rect.x) / scale),
            round(display.rect.y + (y - display.rect.y) / scale),
        )

    async def pointer(
        self, request: UiPointerRequest | Mapping[str, Any]
    ) -> UiActionResult:
        request = coerce_pointer_request(request)
        started = time.monotonic()
        if request.action not in POINTER_ACTIONS:
            raise UiError(
                "invalid_request",
                f"Unknown pointer action {request.action!r}.",
            )
        if request.action == "drag":
            raise UiError("capability_unavailable", DRAG_REASON)
        if request.x is None or request.y is None:
            raise UiError(
                "invalid_request",
                "Pointer actions require integer x and y coordinates.",
            )
        x = int(request.x)
        y = int(request.y)

        displays, bounds = await self._layout()
        if bounds is None:
            raise UiError(
                "no_graphical_session", "No Windows displays are available."
            )
        if not bounds.contains(x, y):
            raise UiError(
                "invalid_request",
                f"Point {x},{y} is outside the desktop layout "
                f"(x {bounds.x}..{bounds.x + bounds.width - 1}, "
                f"y {bounds.y}..{bounds.y + bounds.height - 1}).",
            )
        display = display_at(displays, x, y)
        if display is None:
            display = next(
                (item for item in displays if item.primary), displays[0]
            )
        scale = float(display.dpi_scale or display.scale or 1.0)
        scales = {
            float(item.dpi_scale or item.scale or 1.0) for item in displays
        }
        uniform_scale = len(scales) <= 1
        warnings: list[str] = []
        if uniform_scale:
            # A DPI-unaware process sees one uniformly virtualized desktop, so
            # global physical = global logical * scale for every monitor.
            physical_x = round(x * scale)
            physical_y = round(y * scale)
        else:
            # Mixed DPI: Windows exposes no per-monitor logical/physical origin,
            # so keep the target display's origin and report the approximation.
            physical_x, physical_y = self._physical_point(display, x, y)
            warnings.append(
                "Mixed-DPI multi-display layout: pointer coordinates use the "
                "target display's scale and may be offset on other monitors."
            )

        scroll_x = 0
        scroll_y = 0
        count = 1
        if request.action == "scroll":
            scroll_x = int(request.delta_x or 0)
            scroll_y = int(request.delta_y or 0)
            if not scroll_x and not scroll_y:
                raise UiError(
                    "invalid_request",
                    "scroll needs delta_x or delta_y (wheel notches).",
                )
            if (
                abs(scroll_x) > MAX_WHEEL_NOTCHES
                or abs(scroll_y) > MAX_WHEEL_NOTCHES
            ):
                raise UiError(
                    "invalid_request",
                    f"scroll deltas are limited to +/-{MAX_WHEEL_NOTCHES} notches.",
                )
            scroll_x *= 120
            scroll_y *= 120
            action = "scroll"
        elif request.action == "move":
            action = "move"
        elif request.action == "double_click":
            action = "double_click"
            count = 2
        elif request.action == "right_click":
            action = "right_click"
        elif request.action == "middle_click":
            action = "middle_click"
        else:
            button = request.button or "left"
            if button not in BUTTON_ACTIONS:
                raise UiError(
                    "invalid_request",
                    f"Unknown mouse button {button!r}; use left, right, or middle.",
                )
            action = BUTTON_ACTIONS[button]
            count = min(3, max(1, int(request.count or 1)))

        payload = {
            "action": action,
            "x": physical_x,
            "y": physical_y,
            "count": count,
            "scroll_x": scroll_x,
            "scroll_y": scroll_y,
        }
        result = await self._run_script(
            _POINTER_SCRIPT.replace("__PAYLOAD__", self._payload(payload)),
            timeout_ms=15_000,
        )
        if result.exit_code != 0:
            raise UiError(
                "input_failed",
                "Windows pointer input failed: "
                f"{result.stderr.strip()[:300] or result.exit_code}",
            )
        response = self._json(result)
        if not isinstance(response, Mapping) or response.get("ok") is not True:
            detail = (
                response.get("error")
                if isinstance(response, Mapping)
                else "unparseable pointer result"
            )
            raise UiError(
                "input_failed", f"Windows pointer input failed: {detail}"
            )

        cursor_x = response.get("cursor_x")
        cursor_y = response.get("cursor_y")
        if (
            isinstance(cursor_x, (int, float))
            and not isinstance(cursor_x, bool)
            and isinstance(cursor_y, (int, float))
            and not isinstance(cursor_y, bool)
        ):
            if uniform_scale:
                logical_x = round(round(cursor_x) / scale)
                logical_y = round(round(cursor_y) / scale)
            else:
                logical_x, logical_y = self._logical_point(
                    display, round(cursor_x), round(cursor_y)
                )
            cursor = UiPoint(logical_x, logical_y)
            cursor_ok = abs(logical_x - x) <= 2 and abs(logical_y - y) <= 2
            verification = UiVerification(
                method="cursor",
                ok=cursor_ok,
                cursor=cursor,
                detail=(
                    f"cursor at {logical_x},{logical_y}"
                    if cursor_ok
                    else f"cursor at {logical_x},{logical_y}, expected {x},{y}"
                ),
            )
        else:
            cursor_ok = None
            verification = UiVerification(
                method="not_available",
                ok=None,
                detail="GetCursorPos readback was not available.",
            )

        return UiActionResult(
            ok=cursor_ok is not False,
            backend="powershell+user32",
            action=request.action,
            performed_at=iso_now(),
            duration_ms=int((time.monotonic() - started) * 1000),
            verification=verification,
            warnings=warnings,
        )

    # ── Keyboard ─────────────────────────────────────────────────────────

    async def keyboard(
        self, request: UiKeyboardRequest | Mapping[str, Any]
    ) -> UiActionResult:
        request = coerce_keyboard_request(request)
        started = time.monotonic()
        warnings: list[str] = []
        if not request.text and not request.keys:
            raise UiError("invalid_request", "keyboard requires text or keys.")
        if request.text and len(request.text) > MAX_TEXT_CHARS:
            raise UiError(
                "invalid_request",
                f"text is limited to {MAX_TEXT_CHARS} characters per call.",
            )
        normalized_keys: str | None = None
        if request.keys:
            normalized_keys = normalize_keys(request.keys)
            if normalized_keys is None:
                raise UiError(
                    "invalid_request",
                    f"Unsupported key sequence {request.keys!r}; use a key "
                    "such as Enter, Tab, Escape, Backspace, arrows, F1-F24, "
                    "PrintScreen, or a modifier combination such as Ctrl+C.",
                )
        if request.delay_ms:
            warnings.append(
                "SendKeys has no per-character delay; delay_ms was ignored."
            )
        if request.text and any(ord(character) > 126 for character in request.text):
            warnings.append(
                "SendKeys is keyboard-layout dependent; non-ASCII text may not "
                "type correctly."
            )

        expected_pid: int | None = None
        if request.window_id:
            # Raises input_failed when the foreground did not actually move:
            # never type into whatever happened to be focused instead.
            await self.focus_window(request.window_id)
            expected_pid = int(request.window_id)
            await self._sleep(0.12)

        payload = {
            "text": (
                escape_send_keys_text(request.text)
                if request.text is not None
                else ""
            ),
            "keys": normalized_keys or "",
            "expected_pid": expected_pid,
        }
        result = await self._run_script(
            _KEYBOARD_SCRIPT.replace("__PAYLOAD__", self._payload(payload)),
            timeout_ms=20_000,
        )
        if result.exit_code != 0:
            raise UiError(
                "input_failed",
                "Windows keyboard input failed: "
                f"{result.stderr.strip()[:300] or result.exit_code}",
            )
        response = self._json(result)
        if not isinstance(response, Mapping) or response.get("ok") is not True:
            code = (
                response.get("code") if isinstance(response, Mapping) else None
            )
            if code == "focus_lost":
                raise UiError(
                    "input_failed",
                    f"Focus moved to pid {response.get('foreground_pid')} "
                    f"before typing into window {request.window_id}; "
                    "refusing to type into the wrong window.",
                )
            if code == "no_foreground":
                raise UiError(
                    "input_failed",
                    "No foreground window is available; Windows input targets "
                    "the foreground window.",
                )
            detail = (
                response.get("error")
                if isinstance(response, Mapping)
                else "unparseable keyboard result"
            )
            raise UiError(
                "input_failed", f"Windows keyboard input failed: {detail}"
            )

        foreground_pid = _int_or(response.get("foreground_pid"))
        after_pid = _int_or(response.get("after_pid"))
        if expected_pid is not None and foreground_pid != expected_pid:
            raise UiError(
                "input_failed",
                f"Focus verification failed: expected pid {expected_pid}, "
                f"foreground was pid {foreground_pid}.",
            )
        if after_pid and foreground_pid and after_pid != foreground_pid:
            warnings.append(
                "The foreground window changed while the input was being "
                "sent; the keystrokes may have landed in the new window."
            )

        active_window = {
            "id": str(foreground_pid),
            "app": str(response.get("foreground_app") or "unknown"),
            "title": str(response.get("foreground_title") or ""),
        }
        return UiActionResult(
            ok=True,
            backend="powershell+sendkeys",
            action="type" if request.text is not None else "keys",
            performed_at=iso_now(),
            duration_ms=int((time.monotonic() - started) * 1000),
            verification=UiVerification(
                method="foreground_window",
                ok=True,
                active_window=active_window,
            ),
            warnings=warnings,
        )

    async def type_text(self, text: str, **kwargs: Any) -> UiActionResult:
        return await self.keyboard(
            UiKeyboardRequest(
                text=text,
                window_id=kwargs.get("window_id"),
                delay_ms=kwargs.get("delay_ms"),
                verify=kwargs.get("verify") is True,
            )
        )

    async def keys(self, keys: str, **kwargs: Any) -> UiActionResult:
        return await self.keyboard(
            UiKeyboardRequest(
                keys=keys,
                window_id=kwargs.get("window_id"),
                verify=kwargs.get("verify") is True,
            )
        )


__all__ = [
    "ALL_CAPTURE_REASON",
    "DRAG_REASON",
    "REGION_CAPTURE_REASON",
    "WINDOW_CAPTURE_REASON",
    "WindowsAdapter",
    "escape_send_keys_text",
    "normalize_keys",
]
