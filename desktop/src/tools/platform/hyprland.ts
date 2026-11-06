// Hyprland platform adapter.
//
// Division of labour (validated against a wlroots compositor and Hyprland's
// own protocol source):
//
//   state / focus      hyprctl -j (monitors, clients, activewindow, cursorpos)
//   absolute pointer   hyprctl dispatch movecursor  (global logical coords)
//   pointer buttons    wl-input (zwlr_virtual_pointer_v1) — absolute move,
//                      button, discrete scroll and real drag in one process
//   keyboard           wtype (zwp_virtual_keyboard_v1, XKB layouts, unicode)
//   capture            grim (wlr-screencopy)
//
// Why not wlrctl alone: it has relative motion only and no button down/up, so
// drag is impossible. Why not ydotool: it needs /dev/uinput, a daemon and
// root/udev rules, and types physical US scancodes. Why not libei/portal:
// xdg-desktop-portal-hyprland does not implement the RemoteDesktop portal.
//
// Known compositor quirks are surfaced as warnings, never hidden:
//   - hyprwm/Hyprland#6749: absolute virtual-pointer motion on multi-monitor
//     layouts. We keep coordinates global and verify the cursor afterwards.
//   - hyprwm/Hyprland#4167: a button sent right after an external cursor warp
//     can land at the previous position. wl-input moves and clicks in one
//     connection; the wlrctl fallback is verified and reported.

import { createHash } from 'node:crypto'
import { promises as fs } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'

import { defaultCommandRunner, parseCursorPos, parseJson, whichBinary } from './command.js'
import {
  UiError,
  displayAt,
  imageSize,
  layoutBounds,
  normalizeRect,
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
  type UiRect,
  type UiVerification,
  type UiWindow
} from './types.js'

interface HyprlandMonitor {
  id?: number
  name?: string
  width?: number
  height?: number
  x?: number
  y?: number
  scale?: number
  transform?: number
  focused?: boolean
  disabled?: boolean
}

interface HyprlandClient {
  address?: string
  mapped?: boolean
  hidden?: boolean
  at?: [number, number]
  size?: [number, number]
  workspace?: { id?: number; name?: string }
  monitor?: number
  class?: string
  initialClass?: string
  title?: string
  pid?: number
  focusHistoryID?: number
  floating?: boolean
  xwayland?: boolean
}

export interface HyprlandAdapterDeps {
  runner?: UiCommandRunner
  env?: NodeJS.ProcessEnv
  platform?: UiPlatform
  which?: (name: string) => Promise<string | null>
  home?: string
}

const PROBE_TTL_MS = 20_000
const MAX_TEXT_CHARS = 2_000

const KEY_ALIASES: Record<string, string> = {
  enter: 'Return',
  return: 'Return',
  tab: 'Tab',
  esc: 'Escape',
  escape: 'Escape',
  space: 'space',
  backspace: 'BackSpace',
  delete: 'Delete',
  del: 'Delete',
  insert: 'Insert',
  up: 'Up',
  down: 'Down',
  left: 'Left',
  right: 'Right',
  home: 'Home',
  end: 'End',
  pageup: 'Prior',
  pagedown: 'Next',
  printscreen: 'Print',
  menu: 'Menu'
}

const MODIFIER_ALIASES: Record<string, string> = {
  ctrl: 'ctrl',
  control: 'ctrl',
  shift: 'shift',
  alt: 'alt',
  option: 'alt',
  super: 'logo',
  meta: 'logo',
  win: 'logo',
  logo: 'logo',
  altgr: 'altgr'
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function requirePoint(request: UiPointerRequest): { x: number; y: number } {
  if (!isFiniteNumber(request.x) || !isFiniteNumber(request.y)) {
    throw new UiError('invalid_request', 'Pointer actions require integer x and y coordinates.')
  }
  return { x: Math.round(request.x), y: Math.round(request.y) }
}

function sha256(buffer: Buffer): string {
  return createHash('sha256').update(buffer).digest('hex')
}

export class HyprlandAdapter implements UiAdapter {
  readonly id = 'linux-hyprland'
  readonly platform: UiPlatform

  private readonly runner: UiCommandRunner
  private readonly env: NodeJS.ProcessEnv
  private readonly which: (name: string) => Promise<string | null>
  private readonly home: string
  private readonly binaryCache = new Map<string, string | null>()
  private probe: { at: number; environment: UiEnvironment; capabilities: UiCapability[] } | null = null

  constructor(deps: HyprlandAdapterDeps = {}) {
    this.platform = deps.platform ?? (process.platform as UiPlatform)
    this.runner = deps.runner ?? defaultCommandRunner
    this.env = deps.env ?? process.env
    this.home = deps.home ?? homedir()
    const which = deps.which
    this.which = which ?? (name => whichBinary(name, this.env.PATH, this.env))
  }

  // ── Discovery ─────────────────────────────────────────────────────────────

  private async resolveBinary(name: string): Promise<string | null> {
    const cached = this.binaryCache.get(name)
    if (cached !== undefined) return cached
    let resolved: string | null = null
    if (name === 'wl-input') {
      const explicit = this.env.HERMES_RELAY_WL_INPUT?.trim()
      if (explicit) {
        resolved = (await this.which(explicit)) ?? explicit
      } else {
        const bundled = join(this.home, '.hermes', 'bin', 'wl-input')
        resolved = (await this.which(bundled)) ?? (await this.which('wl-input'))
      }
    } else {
      resolved = await this.which(name)
    }
    this.binaryCache.set(name, resolved)
    return resolved
  }

  private async hyprctl(args: readonly string[], signal?: AbortSignal) {
    const binary = await this.resolveBinary('hyprctl')
    if (!binary) throw new UiError('missing_dependency', 'hyprctl is not installed or not on PATH.')
    return this.runner.text(binary, args, {
      timeoutMs: 8_000,
      signal,
      env: this.env
    })
  }

  private async hyprctlJson<T>(args: readonly string[], signal?: AbortSignal): Promise<T> {
    const result = await this.hyprctl(args, signal)
    if (!result.ran) {
      throw new UiError('no_graphical_session', 'hyprctl could not run; the compositor session is unavailable.')
    }
    if (result.exitCode !== 0) {
      const detail = result.stderr.trim().slice(0, 400) || `exit ${result.exitCode}`
      throw new UiError('compositor_error', `hyprctl ${args.join(' ')} failed: ${detail}`)
    }
    const parsed = parseJson<T>(result.stdout)
    if (parsed === null) {
      throw new UiError('compositor_error', `hyprctl ${args.join(' ')} returned non-JSON output.`)
    }
    return parsed
  }

  async environment(): Promise<UiEnvironment> {
    const now = Date.now()
    if (this.probe && now - this.probe.at < PROBE_TTL_MS) return this.probe.environment

    const warnings: string[] = []
    const [hyprctl, grim, wtype, wlInput, wlrctl] = await Promise.all([
      this.resolveBinary('hyprctl'),
      this.resolveBinary('grim'),
      this.resolveBinary('wtype'),
      this.resolveBinary('wl-input'),
      this.resolveBinary('wlrctl')
    ])

    const waylandDisplay = this.env.WAYLAND_DISPLAY?.trim()
    const signature = this.env.HYPRLAND_INSTANCE_SIGNATURE?.trim()
    const session = waylandDisplay ? 'wayland' : this.env.DISPLAY?.trim() ? 'x11' : 'none'
    let graphical = false
    let reason: string | undefined
    let version: string | undefined

    if (this.platform !== 'linux') {
      reason = `Hyprland adapter requires Linux (platform=${this.platform}).`
    } else if (!waylandDisplay) {
      reason = 'WAYLAND_DISPLAY is not set; this process is not inside a Wayland session.'
    } else if (!hyprctl) {
      reason = 'hyprctl is not installed.'
    } else {
      try {
        const probe = await this.hyprctlJson<{ version?: string; tag?: string }>(['version', '-j'])
        graphical = true
        version = probe.version ?? probe.tag
      } catch (error) {
        reason = error instanceof Error ? error.message : String(error)
      }
    }

    if (graphical && !signature) {
      warnings.push(
        'HYPRLAND_INSTANCE_SIGNATURE is not set; using the only reachable Hyprland instance. Start the client inside the Hyprland session when several instances are possible.'
      )
    }
    if (graphical && !wlInput) {
      warnings.push(
        'wl-input helper not found (~/.hermes/bin/wl-input). Click/scroll fall back to wlrctl; drag is unavailable. Run `hermes-relay setup-input` on this machine.'
      )
    }
    if (graphical && !grim) {
      warnings.push('grim is not installed; screenshots and visual verification are unavailable.')
    }
    if (graphical && !wtype) {
      warnings.push('wtype is not installed; keyboard input is unavailable.')
    }

    const environment: UiEnvironment = {
      platform: this.platform,
      session,
      compositor: graphical ? 'hyprland' : undefined,
      desktop: this.env.XDG_CURRENT_DESKTOP,
      graphical,
      reason,
      dependencies: {
        hyprctl: { available: Boolean(hyprctl), path: hyprctl ?? undefined, version },
        grim: { available: Boolean(grim), path: grim ?? undefined },
        wtype: { available: Boolean(wtype), path: wtype ?? undefined },
        'wl-input': { available: Boolean(wlInput), path: wlInput ?? undefined },
        wlrctl: { available: Boolean(wlrctl), path: wlrctl ?? undefined }
      },
      warnings: warnings.length ? warnings : undefined
    }
    this.probe = { at: now, environment, capabilities: await this.buildCapabilities(environment) }
    return environment
  }

  private async buildCapabilities(environment: UiEnvironment): Promise<UiCapability[]> {
    const has = (name: string) => environment.dependencies[name]?.available === true
    const graphical = environment.graphical
    const pointerBackend = has('wl-input') ? 'wl-input' : has('wlrctl') ? 'wlrctl' : undefined
    return [
      {
        id: 'screen.capture',
        available: graphical && has('grim'),
        backend: 'grim',
        reason: graphical ? (has('grim') ? undefined : 'grim not installed') : environment.reason
      },
      {
        id: 'windows.list',
        available: graphical,
        backend: 'hyprctl',
        reason: graphical ? undefined : environment.reason
      },
      {
        id: 'windows.focus',
        available: graphical,
        backend: 'hyprctl',
        reason: graphical ? undefined : environment.reason
      },
      {
        id: 'pointer.move',
        available: graphical,
        backend: 'hyprctl movecursor',
        reason: graphical ? undefined : environment.reason
      },
      {
        id: 'pointer.click',
        available: graphical && Boolean(pointerBackend),
        backend: pointerBackend,
        reason: graphical ? (pointerBackend ? undefined : 'needs wl-input or wlrctl') : environment.reason
      },
      {
        id: 'pointer.drag',
        available: graphical && has('wl-input'),
        backend: has('wl-input') ? 'wl-input' : undefined,
        reason: graphical ? (has('wl-input') ? undefined : 'drag needs the wl-input helper') : environment.reason
      },
      {
        id: 'pointer.scroll',
        available: graphical && Boolean(pointerBackend),
        backend: pointerBackend,
        reason: graphical ? (pointerBackend ? undefined : 'needs wl-input or wlrctl') : environment.reason
      },
      {
        id: 'keyboard.type',
        available: graphical && has('wtype'),
        backend: 'wtype',
        reason: graphical ? (has('wtype') ? undefined : 'wtype not installed') : environment.reason
      },
      {
        id: 'keyboard.keys',
        available: graphical && has('wtype'),
        backend: 'wtype',
        reason: graphical ? (has('wtype') ? undefined : 'wtype not installed') : environment.reason
      }
    ]
  }

  async capabilities(): Promise<UiCapability[]> {
    await this.environment()
    return this.probe?.capabilities ?? []
  }

  async displays(): Promise<UiDisplay[]> {
    const monitors = await this.hyprctlJson<HyprlandMonitor[]>(['monitors', '-j'])
    return monitors
      .filter(monitor => monitor.disabled !== true)
      .map(monitor => {
        const scale = isFiniteNumber(monitor.scale) && monitor.scale > 0 ? monitor.scale : 1
        const rawWidth = isFiniteNumber(monitor.width) ? monitor.width : 0
        const rawHeight = isFiniteNumber(monitor.height) ? monitor.height : 0
        const rotated = [1, 3, 5, 7].includes(monitor.transform ?? 0)
        const logicalWidth = Math.round((rotated ? rawHeight : rawWidth) / scale)
        const logicalHeight = Math.round((rotated ? rawWidth : rawHeight) / scale)
        return {
          id: String(monitor.id ?? monitor.name ?? 'unknown'),
          name: monitor.name ?? `monitor-${monitor.id ?? 0}`,
          rect: {
            x: Math.round(monitor.x ?? 0),
            y: Math.round(monitor.y ?? 0),
            width: logicalWidth,
            height: logicalHeight
          },
          scale,
          primary: monitor.id === 0,
          focused: monitor.focused === true,
          transform: monitor.transform ?? 0
        }
      })
  }

  async windows(): Promise<UiWindow[]> {
    const [clients, displays] = await Promise.all([
      this.hyprctlJson<HyprlandClient[]>(['clients', '-j']),
      this.displays().catch(() => [] as UiDisplay[])
    ])
    return clients
      .filter(client => typeof client.address === 'string' && client.address.startsWith('0x'))
      .map(client => {
        const at = client.at ?? [0, 0]
        const size = client.size ?? [0, 0]
        const monitor = displays.find(display => display.id === String(client.monitor ?? ''))
        return {
          id: client.address as string,
          title: client.title ?? '',
          app: client.class ?? client.initialClass ?? 'unknown',
          pid: isFiniteNumber(client.pid) ? client.pid : undefined,
          rect: { x: at[0], y: at[1], width: size[0], height: size[1] },
          display: monitor?.name,
          workspace: client.workspace?.name ?? (client.workspace?.id !== undefined ? String(client.workspace.id) : undefined),
          focused: client.focusHistoryID === 0,
          visible: client.mapped !== false && client.hidden !== true,
          native: { floating: String(client.floating === true), xwayland: String(client.xwayland === true) }
        } satisfies UiWindow
      })
  }

  private async activeWindow(): Promise<{ id: string; app: string; title: string } | null> {
    try {
      const active = await this.hyprctlJson<HyprlandClient>(['activewindow', '-j'])
      if (!active.address) return null
      return {
        id: active.address,
        app: active.class ?? active.initialClass ?? 'unknown',
        title: active.title ?? ''
      }
    } catch {
      return null
    }
  }

  private async cursorPosition(): Promise<{ x: number; y: number } | null> {
    try {
      const binary = await this.resolveBinary('hyprctl')
      if (!binary) return null
      const result = await this.runner.text(binary, ['cursorpos'], { timeoutMs: 4_000 })
      return result.exitCode === 0 ? parseCursorPos(result.stdout) : null
    } catch {
      return null
    }
  }

  private async findWindow(windowId: string): Promise<UiWindow> {
    const normalized = windowId.trim().toLowerCase()
    if (!/^0x[0-9a-f]{1,16}$/.test(normalized)) {
      throw new UiError('invalid_request', `Hyprland window ids are hex addresses like 0x55a1c2d3; got ${windowId}`)
    }
    const windows = await this.windows()
    const match = windows.find(window => window.id.toLowerCase() === normalized)
    if (!match) {
      throw new UiError(
        'window_not_found',
        `No window ${normalized}. Open windows: ${windows.map(w => `${w.app} (${w.id})`).join(', ') || 'none'}`
      )
    }
    return match
  }

  // ── Actions ───────────────────────────────────────────────────────────────

  private async visualHash(display?: string): Promise<string | null> {
    try {
      const capture = await this.capture({
        target: display ? 'display' : 'all',
        display,
        format: 'png'
      })
      return capture.bytes_base64 ? sha256(Buffer.from(capture.bytes_base64, 'base64')) : null
    } catch {
      return null
    }
  }

  private async verify(before: string | null, expect?: { x?: number; y?: number }): Promise<UiVerification> {
    const [cursor, active] = await Promise.all([this.cursorPosition(), this.activeWindow()])
    const detail: string[] = []
    let ok: boolean | null = null
    if (expect && cursor) {
      const dx = Math.abs(cursor.x - (expect.x ?? cursor.x))
      const dy = Math.abs(cursor.y - (expect.y ?? cursor.y))
      ok = dx <= 2 && dy <= 2
      detail.push(ok ? `cursor at ${cursor.x},${cursor.y}` : `cursor at ${cursor.x},${cursor.y}, expected ${expect.x},${expect.y}`)
    }
    if (before) {
      const after = await this.visualHash()
      if (after) {
        const changed = after !== before
        detail.push(changed ? 'screen changed' : 'screen unchanged')
        if (ok === null) ok = changed
        else ok = ok || changed
      }
    }
    return {
      method: [expect ? 'cursor' : null, before ? 'pixels' : null, active ? 'active_window' : null]
        .filter(Boolean)
        .join('+'),
      ok,
      detail: detail.join('; ') || undefined,
      cursor: cursor ?? undefined,
      active_window: active ?? undefined
    }
  }

  async focusWindow(windowId: string): Promise<UiActionResult> {
    const started = Date.now()
    const window = await this.findWindow(windowId)
    if (!window.visible) {
      throw new UiError(
        'window_not_visible',
        `${window.app} (${window.id}) is on a hidden or special workspace; focus it from Hyprland before acting.`
      )
    }
    const result = await this.hyprctl(['dispatch', 'focuswindow', `address:${window.id}`])
    if (result.exitCode !== 0) {
      throw new UiError('input_failed', `focuswindow failed: ${result.stderr.trim() || result.exitCode}`)
    }
    const active = await this.activeWindow()
    return {
      ok: true,
      backend: 'hyprctl',
      action: 'focus_window',
      performed_at: new Date().toISOString(),
      duration_ms: Date.now() - started,
      verification: {
        method: 'active_window',
        ok: active?.id === window.id,
        active_window: active ?? undefined,
        detail: active?.id === window.id ? `focused ${window.app}` : `focus did not settle on ${window.id}`
      }
    }
  }

  async capture(request: UiCaptureRequest, signal?: AbortSignal): Promise<UiCapture> {
    const grim = await this.resolveBinary('grim')
    if (!grim) throw new UiError('missing_dependency', 'grim is not installed; cannot capture the screen.')
    const displays = await this.displays()
    if (displays.length === 0) throw new UiError('no_graphical_session', 'Hyprland reports no active monitors.')

    const format = request.format ?? 'png'
    const target = request.target ?? 'display'
    const warnings: string[] = []
    let args: string[] = []
    let origin = { x: 0, y: 0 }
    let scale = 1
    let displayName = 'all'
    let region: UiRect | null = null

    if (target === 'display' || target === 'window') {
      let display: UiDisplay | null
      let width: number
      let height: number
      if (target === 'window') {
        if (!request.window_id) throw new UiError('invalid_request', 'target=window requires window_id.')
        const window = await this.findWindow(request.window_id)
        display = displays.find(item => item.name === window.display) ?? displayAt(displays, window.rect.x, window.rect.y)
        region = window.rect
        width = window.rect.width
        height = window.rect.height
        if (!window.visible) warnings.push('Window is not currently visible; the capture may show another window.')
      } else {
        display = request.display
          ? displays.find(item => item.name === request.display || item.id === request.display) ?? null
          : displays.find(item => item.focused) ?? displays[0]
        if (!display) {
          throw new UiError('invalid_request', `Unknown display ${request.display}. Displays: ${displays.map(d => d.name).join(', ')}`)
        }
        width = display.rect.width
        height = display.rect.height
      }
      if (!display) throw new UiError('invalid_request', 'Could not determine the display for this capture.')
      origin = { x: display.rect.x, y: display.rect.y }
      scale = display.scale
      displayName = display.name
      if (region) {
        width = region.width
        height = region.height
        origin = { x: region.x, y: region.y }
      }
      if (request.region) warnings.push('region is ignored when target is display/window; use target=region.')
      args = ['-g', `${origin.x},${origin.y} ${width}x${height}`, '-t', format]
    } else if (target === 'region') {
      const bounds = normalizeRect(request.region)
      if (!bounds) throw new UiError('invalid_request', 'target=region requires {x,y,width,height}.')
      const display = displayAt(displays, bounds.x, bounds.y)
      if (!display) {
        throw new UiError('invalid_request', `Region origin ${bounds.x},${bounds.y} is outside the desktop layout.`)
      }
      origin = { x: bounds.x, y: bounds.y }
      scale = display.scale
      displayName = display.name
      args = ['-g', `${bounds.x},${bounds.y} ${bounds.width}x${bounds.height}`, '-t', format]
    } else {
      const bounds = layoutBounds(displays)
      if (!bounds) throw new UiError('no_graphical_session', 'No monitors are available to capture.')
      origin = { x: bounds.x, y: bounds.y }
      const scales = new Set(displays.map(display => display.scale))
      if (scales.size > 1) {
        warnings.push('Displays use different scales; capture a single display for exact pixel-to-coordinate mapping.')
      }
      scale = displays.find(display => display.focused)?.scale ?? 1
      displayName = 'all'
      args = ['-t', format]
    }

    if (format === 'jpeg') {
      const quality = Math.min(100, Math.max(1, Math.round(request.quality ?? 80)))
      args.push('-q', String(quality))
    }
    args.push('-')

    const result = await this.runner.buffer(grim, args, { timeoutMs: 20_000, signal })
    if (!result.ran) throw new UiError('missing_dependency', 'grim could not be executed.')
    if (result.exitCode !== 0 || result.stdout.length === 0) {
      const detail = result.stderr.trim().slice(0, 400) || `exit ${result.exitCode}`
      throw new UiError('capture_failed', `grim failed: ${detail}`)
    }
    if (request.save_to) {
      const path = request.save_to
      await fs.mkdir(dirname(path), { recursive: true }).catch(() => undefined)
      await fs.writeFile(path, result.stdout)
    }

    let pixelWidth = 0
    let pixelHeight = 0
    const decoded = imageSize(format, result.stdout)
    if (decoded) {
      pixelWidth = decoded.width
      pixelHeight = decoded.height
    } else {
      const logicalWidth = request.region?.width ?? (target === 'window' ? 0 : 0)
      const logicalHeight = request.region?.height ?? 0
      if (logicalWidth && logicalHeight) {
        pixelWidth = Math.round(logicalWidth * scale)
        pixelHeight = Math.round(logicalHeight * scale)
      }
      warnings.push(`Could not read ${format.toUpperCase()} dimensions from the capture.`)
    }

    return {
      format,
      bytes_base64: request.save_to ? undefined : result.stdout.toString('base64'),
      saved_path: request.save_to,
      size_bytes: result.stdout.length,
      pixel_width: pixelWidth,
      pixel_height: pixelHeight,
      origin,
      scale,
      display: displayName,
      target,
      warnings: warnings.length ? warnings : undefined
    }
  }

  private async runPointerHelper(
    request: UiPointerRequest,
    point: { x: number; y: number }
  ): Promise<{ ok: boolean; backend: string; warnings: string[] }> {
    const helper = await this.resolveBinary('wl-input')
    const displays = await this.displays()
    const bounds = layoutBounds(displays)
    if (!bounds) throw new UiError('no_graphical_session', 'No monitors are available for pointer input.')
    const warnings: string[] = []
    let args: string[]

    if (helper) {
      args = ['--space', String(bounds.x), String(bounds.y), String(bounds.width), String(bounds.height)]
      if (request.action === 'move') {
        args.push('move', String(point.x), String(point.y))
      } else if (request.action === 'drag') {
        if (!isFiniteNumber(request.to_x) || !isFiniteNumber(request.to_y)) {
          throw new UiError('invalid_request', 'drag requires to_x and to_y.')
        }
        const duration = Math.min(5_000, Math.max(40, Math.round(request.duration_ms ?? 400)))
        args.push(
          'drag', String(point.x), String(point.y),
          String(Math.round(request.to_x)), String(Math.round(request.to_y)),
          request.button ?? 'left', String(duration)
        )
      } else if (request.action === 'scroll') {
        const dx = Math.round(request.delta_x ?? 0)
        const dy = Math.round(request.delta_y ?? 0)
        if (dx === 0 && dy === 0) throw new UiError('invalid_request', 'scroll needs delta_x or delta_y (wheel notches).')
        args.push('scroll', String(point.x), String(point.y), String(dx), String(dy))
      } else {
        const count = request.action === 'double_click' ? 2 : Math.min(3, Math.max(1, Math.round(request.count ?? 1)))
        args.push('click', String(point.x), String(point.y), request.button ?? 'left', String(count))
      }
      const result = await this.runner.text(helper, args, { timeoutMs: 15_000 })
      if (!result.ran) throw new UiError('missing_dependency', 'wl-input could not be executed.')
      if (result.exitCode !== 0) {
        const detail = result.stderr.trim().slice(0, 400) || `exit ${result.exitCode}`
        throw new UiError(result.exitCode === 3 ? 'compositor_unsupported' : 'input_failed', `wl-input failed: ${detail}`)
      }
      return { ok: true, backend: 'wl-input', warnings }
    }

    // Fallback: cursor warp through the compositor + wlrctl button/axis events.
    const wlrctl = await this.resolveBinary('wlrctl')
    if (!wlrctl) {
      throw new UiError(
        'missing_dependency',
        'Pointer buttons need the wl-input helper (recommended) or wlrctl. Run `hermes-relay setup-input` on this machine.'
      )
    }
    if (request.action === 'drag') {
      throw new UiError(
        'capability_unavailable',
        'Drag needs the wl-input helper; wlrctl has no button down/up requests. Run `hermes-relay setup-input` on this machine.'
      )
    }
    const hyprctl = await this.resolveBinary('hyprctl')
    if (!hyprctl) throw new UiError('missing_dependency', 'hyprctl is not installed.')
    const warp = await this.runner.text(hyprctl, ['dispatch', 'movecursor', String(point.x), String(point.y)], { timeoutMs: 6_000 })
    if (warp.exitCode !== 0) throw new UiError('input_failed', `movecursor failed: ${warp.stderr.trim() || warp.exitCode}`)
    await new Promise(resolve => setTimeout(resolve, 40))
    const button = request.button ?? 'left'
    let command: string[]
    if (request.action === 'scroll') {
      const dx = Math.round(request.delta_x ?? 0)
      const dy = Math.round(request.delta_y ?? 0)
      if (dx === 0 && dy === 0) throw new UiError('invalid_request', 'scroll needs delta_x or delta_y (wheel notches).')
      command = ['pointer', 'scroll', String(dy), String(dx)]
    } else {
      const count = request.action === 'double_click' ? 2 : Math.min(3, Math.max(1, Math.round(request.count ?? 1)))
      command = ['pointer', 'click', button]
      for (let index = 1; index < count; index += 1) {
        const repeat = await this.runner.text(wlrctl, command, { timeoutMs: 6_000 })
        if (repeat.exitCode !== 0) throw new UiError('input_failed', `wlrctl click failed: ${repeat.stderr.trim() || repeat.exitCode}`)
      }
    }
    const result = await this.runner.text(wlrctl, command, { timeoutMs: 6_000 })
    if (result.exitCode !== 0) throw new UiError('input_failed', `wlrctl failed: ${result.stderr.trim() || result.exitCode}`)
    warnings.push(
      'Used the wlrctl fallback: cursor warp and button events are separate clients, which has known Hyprland quirks (#4167, #6749). Install the wl-input helper for atomic input.'
    )
    return { ok: true, backend: 'wlrctl', warnings }
  }

  async pointer(request: UiPointerRequest, signal?: AbortSignal): Promise<UiActionResult> {
    const started = Date.now()
    const point = requirePoint(request)
    const displays = await this.displays()
    const bounds = layoutBounds(displays)
    if (!bounds) throw new UiError('no_graphical_session', 'No monitors are available for pointer input.')
    if (point.x < bounds.x || point.y < bounds.y ||
        point.x >= bounds.x + bounds.width || point.y >= bounds.y + bounds.height) {
      throw new UiError(
        'invalid_request',
        `Point ${point.x},${point.y} is outside the desktop layout ` +
        `(x ${bounds.x}..${bounds.x + bounds.width - 1}, y ${bounds.y}..${bounds.y + bounds.height - 1}). ` +
        `Displays: ${displays.map(d => `${d.name} ${d.rect.x},${d.rect.y} ${d.rect.width}x${d.rect.height}`).join('; ')}`
      )
    }

    const before = request.verify === true ? await this.visualHash(undefined) : null
    const result = await this.runPointerHelper(request, point)
    if (signal?.aborted) throw new UiError('aborted', 'aborted')

    const expect = request.action === 'drag'
      ? { x: Math.round(request.to_x ?? point.x), y: Math.round(request.to_y ?? point.y) }
      : point
    const verification = await this.verify(before, expect)
    if (request.action === 'drag') {
      verification.detail = [verification.detail, `dragged to ${expect.x},${expect.y}`]
        .filter(Boolean)
        .join('; ')
    }
    return {
      ok: result.ok,
      backend: result.backend,
      action: request.action,
      performed_at: new Date().toISOString(),
      duration_ms: Date.now() - started,
      verification,
      warnings: result.warnings.length ? result.warnings : undefined
    }
  }

  private buildKeyArgs(keys: string): string[] {
    const parts = keys
      .split('+')
      .map(part => part.trim().toLowerCase())
      .filter(Boolean)
    if (parts.length === 0) throw new UiError('invalid_request', 'keys is empty.')
    const keyPart = parts[parts.length - 1]
    const modifiers = parts.slice(0, -1).map(part => {
      const modifier = MODIFIER_ALIASES[part]
      if (!modifier) throw new UiError('invalid_request', `Unknown modifier ${part} in ${keys}.`)
      return modifier
    })
    let key: string
    if (/^[a-z0-9]$/.test(keyPart)) key = keyPart
    else if (/^f([1-9]|1[0-2])$/.test(keyPart)) key = keyPart.toUpperCase()
    else if (KEY_ALIASES[keyPart]) key = KEY_ALIASES[keyPart]
    else if (/^[a-z][a-z0-9_]*$/i.test(keyPart)) key = keyPart
    else throw new UiError('invalid_request', `Unsupported key ${keyPart}. Use XKB keysym names or common aliases (enter, tab, esc, arrows, f1-f12).`)

    const args: string[] = []
    for (const modifier of modifiers) args.push('-M', modifier)
    args.push('-k', key)
    for (const modifier of [...modifiers].reverse()) args.push('-m', modifier)
    return args
  }

  async keyboard(request: UiKeyboardRequest, signal?: AbortSignal): Promise<UiActionResult> {
    const started = Date.now()
    const wtype = await this.resolveBinary('wtype')
    if (!wtype) throw new UiError('missing_dependency', 'wtype is not installed; cannot type on Hyprland.')
    const warnings: string[] = []
    if (request.window_id) {
      const focus = await this.focusWindow(request.window_id)
      if (focus.verification?.ok === false) {
        warnings.push(focus.verification.detail ?? 'focus did not settle before typing')
      }
    }
    const text = request.text
    const keys = request.keys
    if (!text && !keys) throw new UiError('invalid_request', 'keyboard requires text or keys.')
    if (text && text.length > MAX_TEXT_CHARS) {
      throw new UiError('invalid_request', `text is limited to ${MAX_TEXT_CHARS} characters per call.`)
    }

    const before = request.verify === true ? await this.visualHash(undefined) : null
    let args: string[]
    let stdin: string | undefined
    if (text) {
      const delay = Math.min(200, Math.max(0, Math.round(request.delay_ms ?? 10)))
      args = ['-s', '30', '-d', String(delay), '-']
      stdin = text
    } else {
      args = this.buildKeyArgs(keys as string)
    }
    const result = await this.runner.text(wtype, args, { timeoutMs: 20_000, stdin, signal })
    if (!result.ran) throw new UiError('missing_dependency', 'wtype could not be executed.')
    if (result.exitCode !== 0) {
      throw new UiError('input_failed', `wtype failed: ${result.stderr.trim().slice(0, 400) || result.exitCode}`)
    }
    const verification = await this.verify(before)
    return {
      ok: true,
      backend: 'wtype',
      action: text ? 'type' : 'keys',
      performed_at: new Date().toISOString(),
      duration_ms: Date.now() - started,
      verification,
      warnings: warnings.length ? warnings : undefined
    }
  }
}
