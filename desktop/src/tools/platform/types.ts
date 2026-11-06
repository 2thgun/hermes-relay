// Common platform-adapter contract for desktop observation and input.
//
// The relay's existing `desktop_computer_*` tools grew out of the Windows CUA
// driver experiment. This module gives Linux (Hyprland and X11) and Windows a
// single internal interface so the tool layer can stay platform-neutral:
//
//   tools/handlers/desktopUi.ts  ->  selectPlatformAdapter()  ->  adapter
//
// Adapters are deliberately small and shell-based. They report *why* a
// capability is missing instead of throwing an opaque error, because the agent
// needs to tell the user "drag needs the wl-input helper" rather than "failed".

export type UiPlatform = 'linux' | 'win32' | 'darwin'
export type UiSessionKind = 'wayland' | 'x11' | 'windows' | 'none'

export type UiCapabilityId =
  | 'screen.capture'
  | 'windows.list'
  | 'windows.focus'
  | 'pointer.move'
  | 'pointer.click'
  | 'pointer.drag'
  | 'pointer.scroll'
  | 'keyboard.type'
  | 'keyboard.keys'

export interface UiCapability {
  id: UiCapabilityId
  available: boolean
  backend?: string
  reason?: string
}

export interface UiRect {
  x: number
  y: number
  width: number
  height: number
}

export interface UiDisplay {
  id: string
  name: string
  rect: UiRect
  /** Logical scale factor (Hyprland `scale`). 1 for X11; Windows reports the
   * capture factor here and the OS DPI separately in `dpi_scale`. */
  scale: number
  primary: boolean
  focused?: boolean
  transform?: number
  /** Physical panel pixels, when the OS exposes them separately (Windows). */
  physical_width?: number
  physical_height?: number
  /** OS DPI scale: 1.5 at 150%, 1 at 100%. */
  dpi_scale?: number
}

export interface UiWindow {
  /** Stable for the lifetime of the window, never reused across restarts. */
  id: string
  title: string
  app: string
  pid?: number
  rect: UiRect
  display?: string
  workspace?: string
  focused: boolean
  visible: boolean
  /** Platform-native handle(s) for debugging, never used as the tool-facing id. */
  native?: Record<string, string | number>
}

export interface UiEnvironment {
  platform: UiPlatform
  session: UiSessionKind
  compositor?: string
  desktop?: string
  /** True only when a logged-in graphical session is reachable. */
  graphical: boolean
  /** Why `graphical` is false, when it is. */
  reason?: string
  dependencies: Record<string, { available: boolean; path?: string; version?: string }>
  warnings?: string[]
}

export interface UiCaptureRequest {
  target?: 'display' | 'window' | 'region' | 'all'
  display?: string
  window_id?: string
  region?: UiRect
  format?: 'png' | 'jpeg'
  quality?: number
  save_to?: string
}

export interface UiCapture {
  format: 'png' | 'jpeg'
  bytes_base64?: string
  saved_path?: string
  size_bytes: number
  /** Pixel dimensions of the returned image. */
  pixel_width: number
  pixel_height: number
  /** Logical coordinates of the image's top-left pixel. */
  origin: { x: number; y: number }
  /** Logical-to-pixel mapping: global = origin + pixel / scale. */
  scale: number
  display: string
  target: string
  warnings?: string[]
}

export type UiPointerAction =
  | 'move'
  | 'click'
  | 'double_click'
  | 'right_click'
  | 'middle_click'
  | 'scroll'
  | 'drag'

export interface UiPointerRequest {
  action: UiPointerAction
  x?: number
  y?: number
  to_x?: number
  to_y?: number
  button?: 'left' | 'right' | 'middle'
  count?: number
  delta_x?: number
  delta_y?: number
  duration_ms?: number
  /** Capture the screen before/after and compare hashes. */
  verify?: boolean
}

export interface UiKeyboardRequest {
  text?: string
  keys?: string
  /** Focus this window first. Without it input goes to the focused window. */
  window_id?: string
  delay_ms?: number
  verify?: boolean
}

export interface UiVerification {
  method: string
  ok: boolean | null
  detail?: string
  cursor?: { x: number; y: number }
  active_window?: { id: string; app: string; title: string }
  image_changed?: boolean
}

export interface UiActionResult {
  ok: boolean
  backend: string
  action: string
  performed_at: string
  duration_ms: number
  verification?: UiVerification
  warnings?: string[]
}

export class UiError extends Error {
  readonly code: string

  constructor(code: string, message: string) {
    super(message)
    this.name = 'UiError'
    this.code = code
  }
}

export interface UiCommandOptions {
  stdin?: string
  timeoutMs?: number
  signal?: AbortSignal
  env?: NodeJS.ProcessEnv
}

export interface UiCommandResult {
  ran: boolean
  exitCode: number
  stdout: string
  stderr: string
}

export interface UiCommandBufferResult {
  ran: boolean
  exitCode: number
  stdout: Buffer
  stderr: string
}

/** Injectable process boundary — every adapter is exercised with a fake runner. */
export interface UiCommandRunner {
  text(cmd: string, args: readonly string[], opts?: UiCommandOptions): Promise<UiCommandResult>
  buffer(cmd: string, args: readonly string[], opts?: UiCommandOptions): Promise<UiCommandBufferResult>
}

export interface UiAdapter {
  readonly id: string
  readonly platform: UiPlatform
  environment(): Promise<UiEnvironment>
  capabilities(): Promise<UiCapability[]>
  displays(): Promise<UiDisplay[]>
  windows(): Promise<UiWindow[]>
  focusWindow(windowId: string): Promise<UiActionResult>
  capture(request: UiCaptureRequest, signal?: AbortSignal): Promise<UiCapture>
  pointer(request: UiPointerRequest, signal?: AbortSignal): Promise<UiActionResult>
  keyboard(request: UiKeyboardRequest, signal?: AbortSignal): Promise<UiActionResult>
}

// ── Shared helpers ───────────────────────────────────────────────────────────

export function rectContains(rect: UiRect, x: number, y: number): boolean {
  return x >= rect.x && y >= rect.y && x < rect.x + rect.width && y < rect.y + rect.height
}

export function normalizeRect(value: unknown): UiRect | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const raw = value as Record<string, unknown>
  const read = (key: string): number | null => {
    const current = raw[key]
    return typeof current === 'number' && Number.isFinite(current) ? Math.round(current) : null
  }
  const x = read('x')
  const y = read('y')
  const width = read('width')
  const height = read('height')
  if (x === null || y === null || width === null || height === null) return null
  if (width <= 0 || height <= 0) return null
  return { x, y, width, height }
}

export function layoutBounds(displays: readonly UiDisplay[]): UiRect | null {
  if (displays.length === 0) return null
  let minX = Infinity
  let minY = Infinity
  let maxX = -Infinity
  let maxY = -Infinity
  for (const display of displays) {
    minX = Math.min(minX, display.rect.x)
    minY = Math.min(minY, display.rect.y)
    maxX = Math.max(maxX, display.rect.x + display.rect.width)
    maxY = Math.max(maxY, display.rect.y + display.rect.height)
  }
  return { x: minX, y: minY, width: maxX - minX, height: maxY - minY }
}

export function displayAt(displays: readonly UiDisplay[], x: number, y: number): UiDisplay | null {
  return displays.find(display => rectContains(display.rect, x, y)) ?? null
}

/** PNG IHDR parse — avoids an image-library dependency just for dimensions. */
export function pngSize(buffer: Buffer): { width: number; height: number } | null {
  if (buffer.length < 24) return null
  const signature = buffer.subarray(0, 8)
  if (!signature.equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) return null
  if (buffer.toString('ascii', 12, 16) !== 'IHDR') return null
  return { width: buffer.readUInt32BE(16), height: buffer.readUInt32BE(20) }
}

/** JPEG SOF parse for the same purpose. Handles progressive and baseline. */
export function jpegSize(buffer: Buffer): { width: number; height: number } | null {
  if (buffer.length < 4 || buffer[0] !== 0xff || buffer[1] !== 0xd8) return null
  let offset = 2
  while (offset + 9 < buffer.length) {
    if (buffer[offset] !== 0xff) {
      offset += 1
      continue
    }
    const marker = buffer[offset + 1]
    if (marker === 0xd8 || marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) {
      offset += 2
      continue
    }
    const length = buffer.readUInt16BE(offset + 2)
    if (length < 2 || offset + 2 + length > buffer.length) return null
    const isStartOfFrame =
      marker >= 0xc0 && marker <= 0xcf && marker !== 0xc4 && marker !== 0xc8 && marker !== 0xcc
    if (isStartOfFrame) {
      return {
        height: buffer.readUInt16BE(offset + 5),
        width: buffer.readUInt16BE(offset + 7)
      }
    }
    offset += 2 + length
  }
  return null
}

export function imageSize(
  format: 'png' | 'jpeg',
  buffer: Buffer
): { width: number; height: number } | null {
  return format === 'png' ? pngSize(buffer) : jpegSize(buffer)
}
