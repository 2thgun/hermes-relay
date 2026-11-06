// Platform adapter selection.
//
// Explicit override wins; otherwise the environment decides. Selection is
// cached per process because probing spawns hyprctl, but callers can force a
// refresh after a session switch (e.g. the daemon reconnecting after login).

import { HyprlandAdapter } from './hyprland.js'
import { WindowsAdapter } from './windows.js'
import { X11Adapter } from './x11.js'
import {
  UiError,
  type UiActionResult,
  type UiAdapter,
  type UiCapability,
  type UiCapture,
  type UiCaptureRequest,
  type UiDisplay,
  type UiEnvironment,
  type UiKeyboardRequest,
  type UiPointerRequest,
  type UiPlatform,
  type UiWindow
} from './types.js'

export type AdapterKind = 'auto' | 'hyprland' | 'x11' | 'windows'

export interface AdapterSelection {
  adapter: UiAdapter
  kind: Exclude<AdapterKind, 'auto'>
  reason: string
}

class UnsupportedAdapter implements UiAdapter {
  readonly id = 'unsupported'
  readonly platform: UiPlatform

  constructor(platform: UiPlatform, private readonly why: string) {
    this.platform = platform
  }

  async environment(): Promise<UiEnvironment> {
    return {
      platform: this.platform,
      session: 'none',
      graphical: false,
      reason: this.why,
      dependencies: {}
    }
  }

  async capabilities(): Promise<UiCapability[]> {
    return []
  }

  async displays(): Promise<UiDisplay[]> {
    throw new UiError('unsupported_platform', this.why)
  }

  async windows(): Promise<UiWindow[]> {
    throw new UiError('unsupported_platform', this.why)
  }

  async focusWindow(_windowId: string): Promise<UiActionResult> {
    throw new UiError('unsupported_platform', this.why)
  }

  async capture(_request: UiCaptureRequest): Promise<UiCapture> {
    throw new UiError('unsupported_platform', this.why)
  }

  async pointer(_request: UiPointerRequest): Promise<UiActionResult> {
    throw new UiError('unsupported_platform', this.why)
  }

  async keyboard(_request: UiKeyboardRequest): Promise<UiActionResult> {
    throw new UiError('unsupported_platform', this.why)
  }
}

let cached: AdapterSelection | null = null

export async function selectPlatformAdapter(options: {
  kind?: AdapterKind
  platform?: UiPlatform
  env?: NodeJS.ProcessEnv
  refresh?: boolean
} = {}): Promise<AdapterSelection> {
  const kind = options.kind ?? (process.env.HERMES_RELAY_DESKTOP_BACKEND as AdapterKind | undefined) ?? 'auto'
  const platform = options.platform ?? (process.platform as UiPlatform)
  const env = options.env ?? process.env

  if (!options.refresh && kind === 'auto' && cached) return cached

  if (kind === 'windows' || (kind === 'auto' && platform === 'win32')) {
    cached = { adapter: new WindowsAdapter({ platform, env }), kind: 'windows', reason: 'win32 session' }
    return cached
  }
  if (kind === 'x11') {
    cached = { adapter: new X11Adapter({ platform, env }), kind: 'x11', reason: 'explicit X11 selection' }
    return cached
  }
  if (kind === 'hyprland') {
    cached = { adapter: new HyprlandAdapter({ platform, env }), kind: 'hyprland', reason: 'explicit Hyprland selection' }
    return cached
  }
  if (platform !== 'linux') {
    cached = {
      adapter: new UnsupportedAdapter(platform, `No desktop adapter for ${platform} yet.`),
      kind: 'x11',
      reason: 'unsupported'
    }
    return cached
  }

  if (env.HYPRLAND_INSTANCE_SIGNATURE?.trim()) {
    cached = { adapter: new HyprlandAdapter({ platform, env }), kind: 'hyprland', reason: 'HYPRLAND_INSTANCE_SIGNATURE' }
    return cached
  }

  // Wayland without a Hyprland signature can still be Hyprland (the daemon may
  // have been started by systemd before the variable existed). Probe the IPC
  // before falling back to an XWayland/X11 session.
  if (env.WAYLAND_DISPLAY?.trim()) {
    const hyprland = new HyprlandAdapter({ platform, env })
    const report = await hyprland.environment()
    if (report.graphical) {
      cached = { adapter: hyprland, kind: 'hyprland', reason: 'hyprctl responded on the Wayland session' }
      return cached
    }
    if (env.DISPLAY?.trim()) {
      cached = {
        adapter: new X11Adapter({ platform, env }),
        kind: 'x11',
        reason: `Wayland session is not Hyprland (${report.reason ?? 'unknown'}); using X11`
      }
      return cached
    }
    cached = { adapter: hyprland, kind: 'hyprland', reason: 'Wayland session without X11 fallback' }
    return cached
  }

  if (env.DISPLAY?.trim()) {
    cached = { adapter: new X11Adapter({ platform, env }), kind: 'x11', reason: 'DISPLAY is set' }
    return cached
  }

  cached = {
    adapter: new UnsupportedAdapter(
      platform,
      'No graphical session found (neither WAYLAND_DISPLAY nor DISPLAY is set). Desktop automation needs a logged-in user session; start the client as a login service, not as a system daemon.'
    ),
    kind: 'x11',
    reason: 'no graphical session'
  }
  return cached
}

export function resetPlatformAdapterForTests(): void {
  cached = null
}
