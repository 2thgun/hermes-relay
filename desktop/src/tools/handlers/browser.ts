// `desktop_browser_*` — structured browser automation on the selected computer.
//
// The browser runs as a separate process with a dedicated automation profile
// (~/.hermes/browser-profile), so the user's normal signed-in browser is never
// touched. All state-changing actions require the same assist/control grant as
// desktop input; status is read-only and always available for discovery.

import { promises as fs } from 'node:fs'

import { hasComputerInputGrant } from '../computerGrants.js'
import { selectPlatformAdapter } from '../platform/index.js'
import type { ToolHandler } from '../router.js'
import { BrowserController } from '../browser/controller.js'

const MAX_SCREENSHOT_BASE64 = 2_400_000

let controller: BrowserController | null = null

export function getBrowserController(): BrowserController {
  if (!controller) controller = new BrowserController()
  return controller
}

export function resetBrowserControllerForTests(): void {
  controller = null
}

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

function errorResult(error: unknown): Record<string, unknown> {
  return failure('browser_error', error instanceof Error ? error.message : String(error))
}

function requireGrant(ctx: Parameters<ToolHandler>[1]): Record<string, unknown> | null {
  if (hasComputerInputGrant(ctx.controlSession)) return null
  return failure(
    'grant_required',
    'Browser automation requires an active assist/control grant. Call desktop_computer_grant_request first.'
  )
}

export const browserStatusHandler: ToolHandler = async () => {
  const environment = await selectPlatformAdapter().then(selection => selection.adapter.environment())
  if (!environment.graphical) {
    return failure('no_graphical_session', environment.reason ?? 'No logged-in graphical session.')
  }
  const status = await getBrowserController().status()
  return { ...status, ok: true, note: status.available ? undefined : 'Browser automation is unavailable on this computer.' }
}

export const browserOpenHandler: ToolHandler = async (args, ctx) => {
  const denied = requireGrant(ctx)
  if (denied) return denied
  const url = stringArg(args.url)
  if (!url) return failure('invalid_request', 'desktop_browser_open requires a url.')
  if (!/^https?:\/\//i.test(url) && !/^about:blank$/i.test(url)) {
    return failure('invalid_request', 'Only http(s) URLs (or about:blank) are allowed.')
  }
  try {
    const headless = typeof args.headless === 'boolean' ? args.headless : undefined
    const result = await getBrowserController().open(url, headless === undefined ? {} : { headless })
    return { ...result, ok: true, profile: 'dedicated automation profile' }
  } catch (error) {
    return errorResult(error)
  }
}

export const browserInspectHandler: ToolHandler = async (args, ctx) => {
  const denied = requireGrant(ctx)
  if (denied) return denied
  try {
    const result = await getBrowserController().inspect({
      max_elements: numberArg(args.max_elements) ?? undefined,
      include_text: boolArg(args.include_text, false)
    })
    return { ok: true, ...result }
  } catch (error) {
    return errorResult(error)
  }
}

export const browserClickHandler: ToolHandler = async (args, ctx) => {
  const denied = requireGrant(ctx)
  if (denied) return denied
  const ref = stringArg(args.ref)
  const selector = stringArg(args.selector)
  const x = numberArg(args.x)
  const y = numberArg(args.y)
  if (!ref && !selector && (x === null || y === null)) {
    return failure('invalid_request', 'Provide ref, selector, or x and y.')
  }
  try {
    const result = await getBrowserController().click({
      ref: ref ?? undefined,
      selector: selector ?? undefined,
      x: x ?? undefined,
      y: y ?? undefined,
      button: (stringArg(args.button) as 'left' | 'right' | 'middle' | null) ?? 'left',
      count: numberArg(args.count) ?? 1
    })
    return result
  } catch (error) {
    return errorResult(error)
  }
}

export const browserTypeHandler: ToolHandler = async (args, ctx) => {
  const denied = requireGrant(ctx)
  if (denied) return denied
  const text = typeof args.text === 'string' ? args.text : ''
  if (!text) return failure('invalid_request', 'desktop_browser_type requires text.')
  try {
    const result = await getBrowserController().type({
      ref: stringArg(args.ref) ?? undefined,
      selector: stringArg(args.selector) ?? undefined,
      text,
      clear: boolArg(args.clear, false),
      submit: boolArg(args.submit, false),
      mode: args.mode === 'value' ? 'value' : 'insert'
    })
    return result
  } catch (error) {
    return errorResult(error)
  }
}

export const browserKeyHandler: ToolHandler = async (args, ctx) => {
  const denied = requireGrant(ctx)
  if (denied) return denied
  const keys = stringArg(args.keys)
  if (!keys) return failure('invalid_request', 'desktop_browser_key requires keys (e.g. "ctrl+l" or "enter").')
  try {
    return await getBrowserController().key({ keys })
  } catch (error) {
    return errorResult(error)
  }
}

export const browserScrollHandler: ToolHandler = async (args, ctx) => {
  const denied = requireGrant(ctx)
  if (denied) return denied
  try {
    return await getBrowserController().scroll({
      x: numberArg(args.x) ?? undefined,
      y: numberArg(args.y) ?? undefined,
      delta_x: numberArg(args.delta_x) ?? undefined,
      delta_y: numberArg(args.delta_y) ?? numberArg(args.scroll_y) ?? undefined
    })
  } catch (error) {
    return errorResult(error)
  }
}

export const browserScreenshotHandler: ToolHandler = async (args, ctx) => {
  const denied = requireGrant(ctx)
  if (denied) return denied
  try {
    const result = await getBrowserController().screenshot({
      format: args.format === 'png' ? 'png' : 'jpeg',
      quality: numberArg(args.quality) ?? undefined,
      full_page: boolArg(args.full_page, false)
    })
    const saveTo = stringArg(args.save_to)
    if (saveTo && typeof result.bytes_base64 === 'string') {
      await fs.writeFile(saveTo, Buffer.from(result.bytes_base64, 'base64'))
      return { ...result, bytes_base64: undefined, saved_path: saveTo }
    }
    if (typeof result.bytes_base64 === 'string' && result.bytes_base64.length > MAX_SCREENSHOT_BASE64) {
      return failure(
        'screenshot_too_large',
        `The screenshot is ${Math.round(result.bytes_base64.length / 1024)} KiB encoded, over the relay response budget. Retry with full_page=false, format=jpeg, a smaller quality, or save_to.`,
        { pixel_width: result.pixel_width, pixel_height: result.pixel_height }
      )
    }
    return result
  } catch (error) {
    return errorResult(error)
  }
}

export const browserUploadHandler: ToolHandler = async (args, ctx) => {
  const denied = requireGrant(ctx)
  if (denied) return denied
  const files = Array.isArray(args.files) ? args.files.filter((file): file is string => typeof file === 'string' && file.length > 0) : []
  if (files.length === 0) return failure('invalid_request', 'desktop_browser_upload requires files (absolute paths on the selected computer).')
  try {
    return await getBrowserController().upload({
      ref: stringArg(args.ref) ?? undefined,
      selector: stringArg(args.selector) ?? undefined,
      files
    })
  } catch (error) {
    return errorResult(error)
  }
}

export const browserDownloadHandler: ToolHandler = async (args, ctx) => {
  const denied = requireGrant(ctx)
  if (denied) return denied
  try {
    return await getBrowserController().download({
      ref: stringArg(args.ref) ?? undefined,
      selector: stringArg(args.selector) ?? undefined,
      x: numberArg(args.x) ?? undefined,
      y: numberArg(args.y) ?? undefined,
      timeout_ms: numberArg(args.timeout_ms) ?? undefined
    })
  } catch (error) {
    return errorResult(error)
  }
}

export const browserWaitHandler: ToolHandler = async (args, ctx) => {
  const denied = requireGrant(ctx)
  if (denied) return denied
  try {
    return await getBrowserController().waitFor({
      selector: stringArg(args.selector) ?? undefined,
      text: stringArg(args.text) ?? undefined,
      timeout_ms: numberArg(args.timeout_ms) ?? undefined
    })
  } catch (error) {
    return errorResult(error)
  }
}

export const browserCloseHandler: ToolHandler = async (args, ctx) => {
  const denied = requireGrant(ctx)
  if (denied) return denied
  try {
    if (boolArg(args.page_only, false)) {
      await getBrowserController().closePage()
      return { ok: true, detail: 'Closed the automation page; the browser stays running.' }
    }
    await getBrowserController().close()
    return { ok: true, detail: 'Closed the automation browser.' }
  } catch (error) {
    return errorResult(error)
  }
}
