// Windows platform adapter.
//
// Window discovery/focus uses PowerShell + user32. Screen capture and input
// deliberately stay in the already-shipped `desktop_computer_*` path (CUA
// driver or the legacy user32 input bridge); `desktopUi.ts` routes those calls
// there so this fork does not create a second, untested Windows input stack.
// Everything here reports that split explicitly through capabilities.

import { defaultCommandRunner, parseJson, whichBinary } from './command.js'
import {
  UiError,
  type UiActionResult,
  type UiAdapter,
  type UiCapability,
  type UiCapture,
  type UiCaptureRequest,
  type UiCommandRunner,
  type UiDisplay,
  type UiEnvironment,
  type UiKeyboardRequest,
  type UiPointerRequest,
  type UiPlatform,
  type UiWindow
} from './types.js'

export interface WindowsAdapterDeps {
  runner?: UiCommandRunner
  env?: NodeJS.ProcessEnv
  platform?: UiPlatform
  which?: (name: string) => Promise<string | null>
}

interface WindowsWindowRow {
  id?: number
  app?: string
  title?: string
  x?: number
  y?: number
  width?: number
  height?: number
  focused?: boolean
}

const WINDOW_SCRIPT = `
$ErrorActionPreference = 'Stop'
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class HermesWin {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
}
"@
$foreground = [HermesWin]::GetForegroundWindow()
$rows = @()
foreach ($p in Get-Process | Where-Object { $_.MainWindowHandle -ne 0 }) {
  $rect = New-Object HermesWin+RECT
  if (-not [HermesWin]::GetWindowRect($p.MainWindowHandle, [ref]$rect)) { continue }
  $rows += [pscustomobject]@{
    id = $p.Id
    app = $p.ProcessName
    title = $p.MainWindowTitle
    x = $rect.Left
    y = $rect.Top
    width = $rect.Right - $rect.Left
    height = $rect.Bottom - $rect.Top
    focused = ($p.MainWindowHandle -eq $foreground)
  }
}
$rows | ConvertTo-Json -Compress -Depth 3
`.trim()

export class WindowsAdapter implements UiAdapter {
  readonly id = 'windows'
  readonly platform: UiPlatform

  private readonly runner: UiCommandRunner
  private readonly env: NodeJS.ProcessEnv
  private readonly which: (name: string) => Promise<string | null>
  private powershell: string | null | undefined

  constructor(deps: WindowsAdapterDeps = {}) {
    this.platform = deps.platform ?? (process.platform as UiPlatform)
    this.runner = deps.runner ?? defaultCommandRunner
    this.env = deps.env ?? process.env
    this.which = deps.which ?? (name => whichBinary(name, this.env.PATH, this.env))
  }

  private async shell(): Promise<string> {
    if (this.powershell !== undefined) {
      if (!this.powershell) throw new UiError('missing_dependency', 'PowerShell is not available.')
      return this.powershell
    }
    this.powershell = (await this.which('powershell')) ?? (await this.which('pwsh'))
    if (!this.powershell) throw new UiError('missing_dependency', 'PowerShell is not available.')
    return this.powershell
  }

  private async runScript(script: string, signal?: AbortSignal) {
    const shell = await this.shell()
    return this.runner.text(
      shell,
      ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
      { timeoutMs: 15_000, signal, env: this.env }
    )
  }

  async environment(): Promise<UiEnvironment> {
    const shell = await this.which('powershell').catch(() => null) ?? await this.which('pwsh').catch(() => null)
    const graphical = this.platform === 'win32' && Boolean(shell)
    return {
      platform: this.platform,
      session: this.platform === 'win32' ? 'windows' : 'none',
      compositor: this.platform === 'win32' ? 'win32' : undefined,
      desktop: this.env.SESSIONNAME,
      graphical,
      reason: this.platform !== 'win32'
        ? `Windows adapter requires win32 (platform=${this.platform}).`
        : graphical ? undefined : 'PowerShell is not available.',
      dependencies: {
        powershell: { available: Boolean(shell), path: shell ?? undefined }
      }
    }
  }

  async capabilities(): Promise<UiCapability[]> {
    const environment = await this.environment()
    const graphical = environment.graphical
    const reason = graphical ? undefined : environment.reason
    return [
      { id: 'screen.capture', available: graphical, backend: 'desktop_computer_screenshot', reason },
      { id: 'windows.list', available: graphical, backend: 'powershell+user32', reason },
      { id: 'windows.focus', available: graphical, backend: 'powershell+user32', reason },
      { id: 'pointer.move', available: graphical, backend: 'desktop_computer_action', reason },
      { id: 'pointer.click', available: graphical, backend: 'desktop_computer_action', reason },
      {
        id: 'pointer.drag',
        available: false,
        reason: 'Windows drag is not exposed by the compatibility input bridge; use a structured CUA action.'
      },
      { id: 'pointer.scroll', available: graphical, backend: 'desktop_computer_action', reason },
      { id: 'keyboard.type', available: graphical, backend: 'desktop_computer_action', reason },
      { id: 'keyboard.keys', available: graphical, backend: 'desktop_computer_action', reason }
    ]
  }

  async displays(): Promise<UiDisplay[]> {
    const script = `
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Screen]::AllScreens | ForEach-Object {
  [pscustomobject]@{ name = $_.DeviceName; primary = $_.Primary; x = $_.Bounds.X; y = $_.Bounds.Y; width = $_.Bounds.Width; height = $_.Bounds.Height }
} | ConvertTo-Json -Compress
`.trim()
    const result = await this.runScript(script)
    const parsed = parseJson<Array<{ name?: string; primary?: boolean; x?: number; y?: number; width?: number; height?: number }> | { name?: string; primary?: boolean; x?: number; y?: number; width?: number; height?: number }>(result.stdout)
    if (!parsed) throw new UiError('compositor_error', 'Could not read Windows display geometry.')
    const rows = Array.isArray(parsed) ? parsed : [parsed]
    return rows.map((row, index) => ({
      id: row.name ?? `display-${index}`,
      name: row.name ?? `display-${index}`,
      rect: {
        x: Math.round(row.x ?? 0),
        y: Math.round(row.y ?? 0),
        width: Math.round(row.width ?? 0),
        height: Math.round(row.height ?? 0)
      },
      scale: 1,
      primary: row.primary === true,
      focused: row.primary === true
    }))
  }

  async windows(): Promise<UiWindow[]> {
    const result = await this.runScript(WINDOW_SCRIPT)
    if (result.exitCode !== 0) {
      throw new UiError('compositor_error', `Window enumeration failed: ${result.stderr.trim().slice(0, 300) || result.exitCode}`)
    }
    const parsed = parseJson<WindowsWindowRow[] | WindowsWindowRow>(result.stdout)
    if (!parsed) return []
    const rows = Array.isArray(parsed) ? parsed : [parsed]
    return rows
      .filter(row => typeof row.id === 'number' && (row.width ?? 0) > 0 && (row.height ?? 0) > 0)
      .map(row => ({
        id: String(row.id),
        title: row.title ?? '',
        app: row.app ?? 'unknown',
        pid: row.id,
        rect: {
          x: Math.round(row.x ?? 0),
          y: Math.round(row.y ?? 0),
          width: Math.round(row.width ?? 0),
          height: Math.round(row.height ?? 0)
        },
        focused: row.focused === true,
        visible: true
      }))
  }

  async focusWindow(windowId: string): Promise<UiActionResult> {
    const started = Date.now()
    if (!/^\d+$/.test(windowId)) throw new UiError('invalid_request', 'Windows window ids are process ids from desktop_ui_windows.')
    const script = `$null = (New-Object -ComObject WScript.Shell).AppActivate(${Number(windowId)}); 'ok'`
    const result = await this.runScript(script)
    if (result.exitCode !== 0 || !/ok/i.test(result.stdout)) {
      throw new UiError('input_failed', `AppActivate failed: ${result.stderr.trim().slice(0, 300) || 'window not found'}`)
    }
    const windows = await this.windows()
    const active = windows.find(window => window.id === windowId)
    return {
      ok: true,
      backend: 'powershell+user32',
      action: 'focus_window',
      performed_at: new Date().toISOString(),
      duration_ms: Date.now() - started,
      verification: {
        method: 'window_state',
        ok: active?.focused === true,
        active_window: active ? { id: active.id, app: active.app, title: active.title } : undefined
      }
    }
  }

  // Capture and input are routed to desktop_computer_* by the tool layer.
  async capture(_request: UiCaptureRequest): Promise<UiCapture> {
    throw new UiError('unsupported_operation', 'Use desktop_computer_screenshot on Windows.')
  }

  async pointer(_request: UiPointerRequest): Promise<UiActionResult> {
    throw new UiError('unsupported_operation', 'Use desktop_computer_action on Windows.')
  }

  async keyboard(_request: UiKeyboardRequest): Promise<UiActionResult> {
    throw new UiError('unsupported_operation', 'Use desktop_computer_action on Windows.')
  }
}
