// `desktop_ui_*` — platform-neutral observation and input for paired
// computers.
//
// These tools sit beside the older `desktop_computer_*` family:
//   - Windows keeps using the shipped CUA/legacy input and screenshot paths.
//   - Linux uses the Hyprland or X11 adapter from tools/platform.
//
// Gates match the existing computer-use model: observation needs an
// observe/assist/control grant, input needs an assist/control grant. Nothing
// here weakens the relay's control-session, approval, or audit contracts.

import {
  getComputerGrantSummary,
  hasComputerInputGrant,
  hasComputerObserveGrant
} from '../computerGrants.js'
import { selectPlatformAdapter } from '../platform/index.js'
import { BrowserLauncher, type BrowserStatus } from '../browser/launcher.js'
import { UiError, type UiPointerRequest, type UiRect } from '../platform/types.js'
import type { NormalizedComputerAction } from '../computerInput.js'
import type { ToolContext, ToolHandler } from '../router.js'
import { computerActionHandler, computerScreenshotHandler } from './computer.js'

const MAX_SCREENSHOT_BASE64 = 2_400_000
const MAX_WINDOWS = 100

function failure(code: string, message: string, extra: Record<string, unknown> = {}): Record<string, unknown> {
  return { ok: false, code, message, ...extra }
}

function stringArg(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

function numberArg(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? Math.round(value) : null
}

function boolArg(value: unknown, fallback: boolean): boolean {
  return typeof value === 'boolean' ? value : fallback
}

function adapterError(error: unknown): Record<string, unknown> {
  if (error instanceof UiError) {
    return failure(error.code, error.message, { adapter_error: true })
  }
  return failure('adapter_failed', error instanceof Error ? error.message : String(error))
}

function grantContext(ctx: ToolContext): Record<string, unknown> {
  return { grant: getComputerGrantSummary(ctx.controlSession) }
}

async function wait(ms: number, signal: AbortSignal): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    const timer = setTimeout(resolve, Math.min(10_000, Math.max(0, ms)))
    signal.addEventListener('abort', () => {
      clearTimeout(timer)
      reject(new UiError('aborted', 'aborted'))
    }, { once: true })
  })
}

export const uiStatusHandler: ToolHandler = async () => {
  const selection = await selectPlatformAdapter()
  const environment = await selection.adapter.environment()
  let displays: unknown[] = []
  const warnings: string[] = [...(environment.warnings ?? [])]
  if (environment.graphical) {
    const displayList = await selection.adapter.displays().catch(error => {
      warnings.push(`display enumeration failed: ${error instanceof Error ? error.message : String(error)}`)
      return []
    })
    displays = displayList
  }
  const capabilities = await selection.adapter.capabilities()
  return {
    ok: true,
    adapter: selection.adapter.id,
    selection_reason: selection.reason,
    environment,
    capabilities,
    displays,
    warnings: warnings.length ? warnings : undefined,
    note:
      selection.adapter.id === 'windows'
        ? 'Windows capture and input are served by desktop_computer_screenshot and desktop_computer_action; this tool reports window state.'
        : undefined
  }
}

export const uiWindowsHandler: ToolHandler = async (args, ctx) => {
  try {
    const selection = await selectPlatformAdapter()
    const focus = stringArg(args.focus)
    if (focus) {
      if (!hasComputerInputGrant(ctx.controlSession)) {
        return failure(
          'grant_required',
          'Focusing a window requires an active assist/control grant. Call desktop_computer_grant_request first.'
        )
      }
      const result = await selection.adapter.focusWindow(focus)
      return { ...result, ...grantContext(ctx) }
    }
    const environment = await selection.adapter.environment()
    if (!environment.graphical) {
      return failure('no_graphical_session', environment.reason ?? 'No logged-in graphical session.')
    }
    const appFilter = stringArg(args.app)?.toLowerCase() ?? null
    const includeHidden = boolArg(args.include_hidden, false)
    const windows = (await selection.adapter.windows())
      .filter(window => (includeHidden ? true : window.visible))
      .filter(window => (appFilter ? `${window.app} ${window.title}`.toLowerCase().includes(appFilter) : true))
      .slice(0, MAX_WINDOWS)
      .sort((left, right) => Number(right.focused) - Number(left.focused))
    return {
      ok: true,
      adapter: selection.adapter.id,
      count: windows.length,
      windows,
      truncated: windows.length === MAX_WINDOWS
    }
  } catch (error) {
    return adapterError(error)
  }
}

export const uiScreenshotHandler: ToolHandler = async (args, ctx) => {
  if (!hasComputerObserveGrant(ctx.controlSession)) {
    return failure(
      'grant_required',
      'Screenshots require an active observe/assist/control grant. Call desktop_computer_grant_request first.'
    )
  }
  const selection = await selectPlatformAdapter()
  if (selection.adapter.id === 'windows') {
    const result = (await computerScreenshotHandler(args, ctx)) as Record<string, unknown>
    if (!result || result.ok !== true) return result
    const displays = await selection.adapter.displays()
    const display = displays.find(item => item.focused) ?? displays[0]
    const captured = (result.display ?? {}) as { width?: number; height?: number }
    return {
      ...result,
      adapter: 'windows',
      pixel_width: captured.width ?? display?.rect.width,
      pixel_height: captured.height ?? display?.rect.height,
      origin: display ? { x: display.rect.x, y: display.rect.y } : { x: 0, y: 0 },
      scale: display?.scale ?? 1,
      requested_target: stringArg(args.target) ?? 'display',
      captured_target: 'display'
    }
  }
  try {
    const target = stringArg(args.target) ?? 'display'
    const saveTo = stringArg(args.save_to)
    const format = args.format === 'jpeg' ? 'jpeg' : 'png'
    const capture = await selection.adapter.capture(
      {
        target: target as 'display' | 'window' | 'region' | 'all',
        display: stringArg(args.display) ?? undefined,
        window_id: stringArg(args.window_id) ?? stringArg(args.window) ?? undefined,
        region: (args.region as UiRect | undefined) ?? undefined,
        format,
        quality: numberArg(args.quality) ?? undefined,
        save_to: saveTo ?? undefined
      },
      ctx.abortSignal
    )
    if (!saveTo && capture.bytes_base64 && capture.bytes_base64.length > MAX_SCREENSHOT_BASE64) {
      return failure(
        'screenshot_too_large',
        `The capture is ${Math.round(capture.bytes_base64.length / 1024)} KiB encoded, over the relay response budget. Retry with target=window, target=region, format=jpeg, or save_to and read the file.`,
        { pixel_width: capture.pixel_width, pixel_height: capture.pixel_height }
      )
    }
    return {
      ok: true,
      adapter: selection.adapter.id,
      mode: 'observe',
      ...capture,
      grant: getComputerGrantSummary(ctx.controlSession)
    }
  } catch (error) {
    return adapterError(error)
  }
}

function windowsAction(args: Record<string, unknown>, action: string): NormalizedComputerAction | null {
  const x = numberArg(args.x)
  const y = numberArg(args.y)
  const text = stringArg(args.text)
  const keys = stringArg(args.keys) ?? stringArg(args.key)
  const scrollY = numberArg(args.delta_y) ?? numberArg(args.scroll_y)
  const returnScreenshot = boolArg(args.return_screenshot, false)
  switch (action) {
    case 'move':
      if (x === null || y === null) return null
      return { action: 'mouse_move', x, y, returnScreenshot }
    case 'click':
    case 'left_click':
      if (x === null || y === null) return null
      return { action: 'left_click', x, y, returnScreenshot }
    case 'right_click':
      return x !== null && y !== null ? { action: 'right_click', x, y, returnScreenshot } : null
    case 'double_click':
      return x !== null && y !== null ? { action: 'double_click', x, y, returnScreenshot } : null
    case 'scroll':
      if (x === null || y === null || scrollY === null) return null
      return { action: 'scroll', x, y, scrollY, returnScreenshot }
    case 'type':
      return text ? { action: 'type', text, returnScreenshot } : null
    case 'key':
      return keys ? { action: 'key', keys, returnScreenshot } : null
    case 'wait':
      return { action: 'wait', durationMs: numberArg(args.duration_ms) ?? 500, returnScreenshot }
    default:
      return null
  }
}

export const uiActionHandler: ToolHandler = async (args, ctx) => {
  if (!hasComputerInputGrant(ctx.controlSession)) {
    return failure(
      'grant_required',
      'Desktop input requires an active assist/control grant. Call desktop_computer_grant_request first.'
    )
  }
  const action = stringArg(args.action)?.toLowerCase()
  if (!action) return failure('invalid_request', 'desktop_ui_action requires an action name.')
  const selection = await selectPlatformAdapter()

  if (selection.adapter.id === 'windows') {
    if (action === 'drag') {
      return failure('capability_unavailable', 'Drag is not available through the Windows compatibility input bridge.')
    }
    const normalized = windowsAction(args, action)
    if (!normalized) return failure('invalid_request', `Missing fields for ${action}.`)
    const delegated: Record<string, unknown> = {
      action: normalized.action,
      x: normalized.x,
      y: normalized.y,
      text: normalized.text,
      keys: normalized.keys,
      scroll_y: normalized.scrollY,
      duration_ms: normalized.durationMs,
      return_screenshot: normalized.returnScreenshot
    }
    for (const key of Object.keys(delegated)) {
      if (delegated[key] === undefined) delete delegated[key]
    }
    return computerActionHandler(delegated, ctx)
  }

  try {
    if (action === 'wait') {
      await wait(numberArg(args.duration_ms) ?? 500, ctx.abortSignal)
      return { ok: true, adapter: selection.adapter.id, action, performed_at: new Date().toISOString() }
    }
    if (action === 'type' || action === 'key' || action === 'keys') {
      const text = stringArg(args.text)
      const keys = stringArg(args.keys) ?? stringArg(args.key)
      if (!text && !keys) return failure('invalid_request', 'type/key requires text or keys.')
      const result = await selection.adapter.keyboard(
        {
          text: text ?? undefined,
          keys: keys ?? undefined,
          window_id: stringArg(args.window_id) ?? undefined,
          delay_ms: numberArg(args.delay_ms) ?? undefined,
          verify: boolArg(args.verify, false)
        },
        ctx.abortSignal
      )
      const response: Record<string, unknown> = { ...result, ...grantContext(ctx) }
      if (boolArg(args.return_screenshot, false)) {
        response.after_screenshot = await uiScreenshotHandler({ display: args.display, format: 'jpeg', quality: 70 }, ctx)
      }
      return response
    }

    const pointerAction = action === 'left_click' ? 'click' : action
    const allowed = new Set(['move', 'click', 'double_click', 'right_click', 'middle_click', 'scroll', 'drag'])
    if (!allowed.has(pointerAction)) {
      return failure('invalid_request', `Unknown action ${action}. Use move, click, double_click, right_click, middle_click, scroll, drag, type, key, or wait.`)
    }
    const request: UiPointerRequest = {
      action: pointerAction as UiPointerRequest['action'],
      x: numberArg(args.x) ?? undefined,
      y: numberArg(args.y) ?? undefined,
      to_x: numberArg(args.to_x) ?? undefined,
      to_y: numberArg(args.to_y) ?? undefined,
      button: action === 'right_click' ? 'right' : action === 'middle_click' ? 'middle' : (stringArg(args.button) as UiPointerRequest['button']) ?? 'left',
      count: numberArg(args.count) ?? undefined,
      delta_x: numberArg(args.delta_x) ?? undefined,
      delta_y: numberArg(args.delta_y) ?? numberArg(args.scroll_y) ?? undefined,
      duration_ms: numberArg(args.duration_ms) ?? undefined,
      verify: boolArg(args.verify, true)
    }
    const result = await selection.adapter.pointer(request, ctx.abortSignal)
    const response: Record<string, unknown> = { ...result, ...grantContext(ctx) }
    if (boolArg(args.return_screenshot, false)) {
      response.after_screenshot = await uiScreenshotHandler({ display: args.display, format: 'jpeg', quality: 70 }, ctx)
    }
    return response
  } catch (error) {
    return adapterError(error)
  }
}

/** Shared by the daemon heartbeat: a compact capability summary without
 * spawning a full display enumeration on every status tick. */
export async function platformCapabilitySummary(): Promise<Record<string, unknown>> {
  const selection = await selectPlatformAdapter()
  const environment = await selection.adapter.environment()
  const capabilities = await selection.adapter.capabilities()
  const launcher = new BrowserLauncher()
  const browser = await launcher.status().catch((error: unknown): BrowserStatus => ({
    available: false,
    running: false,
    profile_dir: launcher.profileDir,
    reason: error instanceof Error ? error.message : String(error)
  }))
  return {
    adapter: selection.adapter.id,
    platform: environment.platform,
    session: environment.session,
    compositor: environment.compositor,
    graphical: environment.graphical,
    reason: environment.reason,
    capabilities: capabilities.map(capability => ({
      id: capability.id,
      available: capability.available,
      backend: capability.backend,
      reason: capability.reason
    })),
    dependencies: environment.dependencies,
    warnings: environment.warnings,
    browser: {
      available: browser.available,
      running: browser.running,
      profile_dir: browser.profile_dir,
      binary_path: browser.binary_path,
      version: browser.browser_version,
      reason: browser.reason
    }
  }
}
