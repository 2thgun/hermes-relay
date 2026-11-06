// X11 adapter — xdotool + ImageMagick for non-Hyprland Linux sessions.
//
// This is the compatibility path (i3, XFCE, GNOME on Xorg, …). It uses the
// standard XTEST-backed xdotool surface, so it inherits X11's limits: no
// awareness of Wayland surfaces, and coordinates are physical pixels at the
// X server's scale. Hyprland sessions should use the Hyprland adapter even
// though DISPLAY may also be set there.

import { promises as fs } from 'node:fs'
import { homedir } from 'node:os'

import { defaultCommandRunner, whichBinary } from './command.js'
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
  type UiWindow
} from './types.js'

export interface X11AdapterDeps {
  runner?: UiCommandRunner
  env?: NodeJS.ProcessEnv
  platform?: UiPlatform
  which?: (name: string) => Promise<string | null>
  home?: string
}

const BUTTONS: Record<string, number> = { left: 1, middle: 2, right: 3 }

function integer(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? Math.round(value) : null
}

export class X11Adapter implements UiAdapter {
  readonly id = 'linux-x11'
  readonly platform: UiPlatform

  private readonly runner: UiCommandRunner
  private readonly env: NodeJS.ProcessEnv
  private readonly which: (name: string) => Promise<string | null>
  private readonly cache = new Map<string, string | null>()
  private home: string

  constructor(deps: X11AdapterDeps = {}) {
    this.platform = deps.platform ?? (process.platform as UiPlatform)
    this.runner = deps.runner ?? defaultCommandRunner
    this.env = deps.env ?? process.env
    this.which = deps.which ?? (name => whichBinary(name, this.env.PATH, this.env))
    this.home = deps.home ?? homedir()
  }

  private async binary(name: string): Promise<string | null> {
    const cached = this.cache.get(name)
    if (cached !== undefined) return cached
    const resolved = await this.which(name)
    this.cache.set(name, resolved)
    return resolved
  }

  private async xdotool(args: readonly string[], signal?: AbortSignal) {
    const binary = await this.binary('xdotool')
    if (!binary) throw new UiError('missing_dependency', 'xdotool is not installed.')
    const result = await this.runner.text(binary, args, {
      timeoutMs: 10_000,
      signal,
      env: this.env
    })
    if (!result.ran) throw new UiError('no_graphical_session', 'xdotool could not run; the X11 display is unavailable.')
    return result
  }

  async environment(): Promise<UiEnvironment> {
    const [xdotool, imagemagick, scrot] = await Promise.all([
      this.binary('xdotool'),
      this.binary('import'),
      this.binary('scrot')
    ])
    const display = this.env.DISPLAY?.trim()
    let graphical = false
    let reason: string | undefined
    if (this.platform !== 'linux') {
      reason = `X11 adapter requires Linux (platform=${this.platform}).`
    } else if (!display) {
      reason = 'DISPLAY is not set; this process is not inside an X11 session.'
    } else if (!xdotool) {
      reason = 'xdotool is not installed.'
    } else {
      const probe = await this.xdotool(['getdisplaygeometry']).catch(() => null)
      graphical = Boolean(probe && probe.exitCode === 0)
      if (!graphical) reason = probe?.stderr.trim() || 'X display is not reachable.'
    }
    return {
      platform: this.platform,
      session: display ? 'x11' : 'none',
      compositor: graphical ? 'x11' : undefined,
      desktop: this.env.XDG_CURRENT_DESKTOP,
      graphical,
      reason,
      dependencies: {
        xdotool: { available: Boolean(xdotool), path: xdotool ?? undefined },
        import: { available: Boolean(imagemagick), path: imagemagick ?? undefined },
        scrot: { available: Boolean(scrot), path: scrot ?? undefined }
      }
    }
  }

  async capabilities(): Promise<UiCapability[]> {
    const environment = await this.environment()
    const graphical = environment.graphical
    const has = (name: string) => environment.dependencies[name]?.available === true
    const reason = graphical ? undefined : environment.reason
    return [
      {
        id: 'screen.capture',
        available: graphical && (has('import') || has('scrot')),
        backend: has('import') ? 'imagemagick' : has('scrot') ? 'scrot' : undefined,
        reason: graphical ? (has('import') || has('scrot') ? undefined : 'needs imagemagick or scrot') : reason
      },
      { id: 'windows.list', available: graphical, backend: 'xdotool', reason },
      { id: 'windows.focus', available: graphical, backend: 'xdotool', reason },
      { id: 'pointer.move', available: graphical, backend: 'xdotool', reason },
      { id: 'pointer.click', available: graphical, backend: 'xdotool', reason },
      { id: 'pointer.drag', available: graphical, backend: 'xdotool', reason },
      { id: 'pointer.scroll', available: graphical, backend: 'xdotool', reason },
      { id: 'keyboard.type', available: graphical, backend: 'xdotool', reason },
      { id: 'keyboard.keys', available: graphical, backend: 'xdotool', reason }
    ]
  }

  async displays(): Promise<UiDisplay[]> {
    const xrandr = await this.binary('xrandr')
    if (xrandr) {
      const result = await this.runner.text(xrandr, ['--listmonitors'], { timeoutMs: 5_000, env: this.env })
      if (result.exitCode === 0) {
        const parsed = result.stdout
          .split('\n')
          .map(line => /^\s*\d+:\s+\+?\*?([^\s]+)\s+(\d+)\/\d+x(\d+)\/\d+\+(-?\d+)\+(-?\d+)/.exec(line))
          .filter((match): match is RegExpExecArray => match !== null)
          .map((match, index) => ({
            id: match[1],
            name: match[1],
            rect: { x: Number(match[4]), y: Number(match[5]), width: Number(match[2]), height: Number(match[3]) },
            scale: 1,
            primary: index === 0,
            focused: index === 0
          }))
        if (parsed.length > 0) return parsed
      }
    }
    const geometry = await this.xdotool(['getdisplaygeometry'])
    const match = /^\s*(\d+)\s+(\d+)/.exec(geometry.stdout)
    if (!match) throw new UiError('no_graphical_session', 'Could not read the X11 display geometry.')
    return [
      {
        id: 'root',
        name: 'X11',
        rect: { x: 0, y: 0, width: Number(match[1]), height: Number(match[2]) },
        scale: 1,
        primary: true,
        focused: true
      }
    ]
  }

  async windows(): Promise<UiWindow[]> {
    const search = await this.xdotool(['search', '--onlyvisible', '--name', '.*'])
    if (search.exitCode !== 0) return []
    const ids = search.stdout
      .split('\n')
      .map(line => line.trim())
      .filter(line => /^\d+$/.test(line))
      .slice(0, 80)
    const active = await this.activeWindowId()
    const displays = await this.displays().catch(() => [] as UiDisplay[])
    const windows: UiWindow[] = []
    for (const id of ids) {
      const name = await this.xdotool(['getwindowname', id]).catch(() => null)
      const geometry = await this.xdotool(['getwindowgeometry', '--shell', id]).catch(() => null)
      if (!geometry || geometry.exitCode !== 0) continue
      const values = new Map<string, string>()
      for (const line of geometry.stdout.split('\n')) {
        const [key, value] = line.split('=')
        if (key && value) values.set(key.trim(), value.trim())
      }
      const width = Number(values.get('WIDTH') ?? 0)
      const height = Number(values.get('HEIGHT') ?? 0)
      if (!width || !height) continue
      const rect = {
        x: Number(values.get('X') ?? 0),
        y: Number(values.get('Y') ?? 0),
        width,
        height
      }
      const pidResult = await this.xdotool(['getwindowpid', id]).catch(() => null)
      const app = (name?.stdout ?? '').trim() || 'unknown'
      windows.push({
        id,
        title: (name?.stdout ?? '').trim(),
        app,
        pid: pidResult && /^\d+$/.test(pidResult.stdout.trim()) ? Number(pidResult.stdout.trim()) : undefined,
        rect,
        display: displayAt(displays, rect.x + Math.floor(rect.width / 2), rect.y + Math.floor(rect.height / 2))?.name,
        focused: active === id,
        visible: true
      })
    }
    return windows
  }

  private async activeWindowId(): Promise<string | null> {
    try {
      const result = await this.xdotool(['getactivewindow'])
      const id = result.stdout.trim()
      return /^\d+$/.test(id) ? id : null
    } catch {
      return null
    }
  }

  private async activeWindow(): Promise<{ id: string; app: string; title: string } | null> {
    const id = await this.activeWindowId()
    if (!id) return null
    const name = await this.xdotool(['getwindowname', id]).catch(() => null)
    return { id, app: (name?.stdout ?? '').trim() || 'unknown', title: (name?.stdout ?? '').trim() }
  }

  private async cursor(): Promise<{ x: number; y: number } | null> {
    const result = await this.xdotool(['getmouselocation', '--shell']).catch(() => null)
    if (!result || result.exitCode !== 0) return null
    const values = new Map<string, string>()
    for (const line of result.stdout.split('\n')) {
      const [key, value] = line.split('=')
      if (key && value) values.set(key.trim(), value.trim())
    }
    const x = Number(values.get('X'))
    const y = Number(values.get('Y'))
    return Number.isFinite(x) && Number.isFinite(y) ? { x, y } : null
  }

  async focusWindow(windowId: string): Promise<UiActionResult> {
    const started = Date.now()
    if (!/^\d+$/.test(windowId)) throw new UiError('invalid_request', 'X11 window ids are decimal integers from desktop_ui_windows.')
    const result = await this.xdotool(['windowactivate', '--sync', windowId])
    if (result.exitCode !== 0) throw new UiError('input_failed', `windowactivate failed: ${result.stderr.trim() || result.exitCode}`)
    const active = await this.activeWindow()
    return {
      ok: true,
      backend: 'xdotool',
      action: 'focus_window',
      performed_at: new Date().toISOString(),
      duration_ms: Date.now() - started,
      verification: {
        method: 'active_window',
        ok: active?.id === windowId,
        active_window: active ?? undefined
      }
    }
  }

  async capture(request: UiCaptureRequest, signal?: AbortSignal): Promise<UiCapture> {
    const displays = await this.displays()
    const target = request.target ?? 'display'
    let region: { x: number; y: number; width: number; height: number }
    let displayName: string
    if (target === 'window') {
      if (!request.window_id) throw new UiError('invalid_request', 'target=window requires window_id.')
      const window = (await this.windows()).find(item => item.id === request.window_id)
      if (!window) throw new UiError('window_not_found', `No X11 window ${request.window_id}.`)
      region = window.rect
      displayName = window.display ?? displays[0]?.name ?? 'X11'
    } else if (target === 'region') {
      const parsed = normalizeRect(request.region)
      if (!parsed) throw new UiError('invalid_request', 'target=region requires {x,y,width,height}.')
      region = parsed
      displayName = displayAt(displays, region.x, region.y)?.name ?? 'X11'
    } else {
      const display = request.display
        ? displays.find(item => item.name === request.display || item.id === request.display)
        : displays.find(item => item.primary) ?? displays[0]
      if (!display) throw new UiError('invalid_request', `Unknown display ${request.display}.`)
      region = display.rect
      displayName = display.name
    }

    const format = request.format ?? 'png'
    const importBinary = await this.binary('import')
    const scrot = await this.binary('scrot')
    let buffer: Buffer | null = null
    if (importBinary) {
      const args = ['-window', 'root', '-crop', `${region.width}x${region.height}+${region.x}+${region.y}`, `${format}:-`]
      const result = await this.runner.buffer(importBinary, args, { timeoutMs: 15_000, signal, env: this.env })
      if (result.exitCode === 0 && result.stdout.length > 0) buffer = result.stdout
    }
    if (!buffer && scrot) {
      const path = `${this.home}/.hermes/cache/x11-capture-${Date.now()}-${process.pid}.${format}`
      await fs.mkdir(`${this.home}/.hermes/cache`, { recursive: true }).catch(() => undefined)
      const result = await this.runner.text(scrot, ['-o', '-a', `${region.x},${region.y},${region.width},${region.height}`, path], {
        timeoutMs: 15_000,
        signal,
        env: this.env
      })
      if (result.exitCode === 0) {
        buffer = await fs.readFile(path).catch(() => null)
        await fs.unlink(path).catch(() => undefined)
      }
    }
    if (!buffer) throw new UiError('capture_failed', 'No X11 capture backend succeeded (tried import, scrot).')

    if (request.save_to) await fs.writeFile(request.save_to, buffer)
    const size = imageSize(format, buffer)
    if (!size) throw new UiError('capture_failed', 'Capture did not contain a readable image header.')
    return {
      format,
      bytes_base64: request.save_to ? undefined : buffer.toString('base64'),
      saved_path: request.save_to,
      size_bytes: buffer.length,
      pixel_width: size.width,
      pixel_height: size.height,
      origin: { x: region.x, y: region.y },
      scale: 1,
      display: displayName,
      target
    }
  }

  async pointer(request: UiPointerRequest, signal?: AbortSignal): Promise<UiActionResult> {
    const started = Date.now()
    const x = integer(request.x)
    const y = integer(request.y)
    if (x === null || y === null) throw new UiError('invalid_request', 'Pointer actions require integer x and y coordinates.')
    const displays = await this.displays()
    const bounds = layoutBounds(displays)
    if (!bounds) throw new UiError('no_graphical_session', 'No X11 displays are available.')
    if (x < bounds.x || y < bounds.y || x >= bounds.x + bounds.width || y >= bounds.y + bounds.height) {
      throw new UiError(
        'invalid_request',
        `Point ${x},${y} is outside the desktop layout ` +
        `(x ${bounds.x}..${bounds.x + bounds.width - 1}, y ${bounds.y}..${bounds.y + bounds.height - 1}).`
      )
    }
    const button = BUTTONS[request.button ?? 'left'] ?? 1
    let warnings: string[] | undefined
    if (request.action === 'move') {
      await this.xdotool(['mousemove', '--sync', String(x), String(y)], signal)
    } else if (request.action === 'scroll') {
      const dy = Math.round(request.delta_y ?? 0)
      const dx = Math.round(request.delta_x ?? 0)
      if (!dx && !dy) throw new UiError('invalid_request', 'scroll needs delta_x or delta_y (wheel notches).')
      await this.xdotool(['mousemove', '--sync', String(x), String(y)], signal)
      const vertical = dy > 0 ? 5 : 4
      const horizontal = dx > 0 ? 7 : 6
      if (dy) await this.xdotool(['click', '--repeat', String(Math.abs(dy)), String(vertical)], signal)
      if (dx) await this.xdotool(['click', '--repeat', String(Math.abs(dx)), String(horizontal)], signal)
    } else if (request.action === 'drag') {
      const toX = integer(request.to_x)
      const toY = integer(request.to_y)
      if (toX === null || toY === null) throw new UiError('invalid_request', 'drag requires to_x and to_y.')
      await this.xdotool(['mousemove', '--sync', String(x), String(y)], signal)
      await this.xdotool(['mousedown', String(button)], signal)
      await new Promise(resolve => setTimeout(resolve, 80))
      await this.xdotool(['mousemove', '--sync', String(toX), String(toY)], signal)
      await new Promise(resolve => setTimeout(resolve, 80))
      await this.xdotool(['mouseup', String(button)], signal)
    } else {
      const count = request.action === 'double_click' ? 2 : Math.min(3, Math.max(1, Math.round(request.count ?? 1)))
      await this.xdotool(['mousemove', '--sync', String(x), String(y)], signal)
      await this.xdotool(['click', '--repeat', String(count), String(button)], signal)
    }
    const cursor = await this.cursor()
    const active = await this.activeWindow()
    return {
      ok: true,
      backend: 'xdotool',
      action: request.action,
      performed_at: new Date().toISOString(),
      duration_ms: Date.now() - started,
      verification: {
        method: 'cursor+active_window',
        ok: cursor ? Math.abs(cursor.x - x) <= 2 && Math.abs(cursor.y - y) <= 2 : null,
        cursor: cursor ?? undefined,
        active_window: active ?? undefined
      },
      warnings
    }
  }

  async keyboard(request: UiKeyboardRequest, signal?: AbortSignal): Promise<UiActionResult> {
    const started = Date.now()
    if (request.window_id) {
      await this.focusWindow(request.window_id)
      await new Promise(resolve => setTimeout(resolve, 120))
    }
    if (!request.text && !request.keys) throw new UiError('invalid_request', 'keyboard requires text or keys.')
    if (request.text && request.text.length > 2_000) {
      throw new UiError('invalid_request', 'text is limited to 2000 characters per call.')
    }
    if (request.text) {
      const delay = Math.min(200, Math.max(0, Math.round(request.delay_ms ?? 12)))
      const result = await this.xdotool(['type', '--clearmodifiers', '--delay', String(delay), '--', request.text], signal)
      if (result.exitCode !== 0) throw new UiError('input_failed', `xdotool type failed: ${result.stderr.trim() || result.exitCode}`)
    }
    if (request.keys) {
      const result = await this.xdotool(['key', '--clearmodifiers', request.keys], signal)
      if (result.exitCode !== 0) throw new UiError('input_failed', `xdotool key failed: ${result.stderr.trim() || result.exitCode}`)
    }
    const active = await this.activeWindow()
    return {
      ok: true,
      backend: 'xdotool',
      action: request.text ? 'type' : 'keys',
      performed_at: new Date().toISOString(),
      duration_ms: Date.now() - started,
      verification: {
        method: 'active_window',
        ok: null,
        active_window: active ?? undefined
      }
    }
  }
}
