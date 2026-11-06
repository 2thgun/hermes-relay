// High-level browser control over CDP.
//
// The controller works on one dedicated automation page at a time. Structured
// inspection assigns `data-hermes-ref` handles so the agent can click/type by
// reference instead of guessing pixels; raw coordinates remain available.
// Downloads land in the automation profile's downloads/ directory.

import { openCdp, type CdpConnection, type WebSocketFactory } from './cdp.js'
import { BrowserLauncher } from './launcher.js'
import { imageSize } from '../platform/types.js'

export interface BrowserControllerDeps {
  wsFactory?: WebSocketFactory
  launcher?: BrowserLauncher
  timeoutMs?: number
}

export interface BrowserPageInfo {
  id: string
  url: string
  title: string
}

export interface InspectedElement {
  ref: string
  tag: string
  role?: string
  type?: string
  name: string
  value?: string
  disabled?: boolean
  checked?: boolean
  rect: { x: number; y: number; width: number; height: number }
}

export interface InspectResult {
  url: string
  title: string
  elements: InspectedElement[]
  text?: string
  truncated: boolean
}

export interface BrowserActionResult {
  ok: boolean
  url?: string
  title?: string
  detail?: string
  [key: string]: unknown
}

const MAX_ELEMENTS_LIMIT = 200
const DOWNLOAD_TIMEOUT_MS = 30_000

const MODIFIER_BITS: Record<string, number> = { alt: 1, ctrl: 2, control: 2, meta: 4, cmd: 4, shift: 8 }

const NAMED_KEYS: Record<string, { key: string; code: string; vk: number; text?: string }> = {
  enter: { key: 'Enter', code: 'Enter', vk: 13, text: '\r' },
  return: { key: 'Enter', code: 'Enter', vk: 13, text: '\r' },
  tab: { key: 'Tab', code: 'Tab', vk: 9 },
  escape: { key: 'Escape', code: 'Escape', vk: 27 },
  esc: { key: 'Escape', code: 'Escape', vk: 27 },
  backspace: { key: 'Backspace', code: 'Backspace', vk: 8 },
  delete: { key: 'Delete', code: 'Delete', vk: 46 },
  del: { key: 'Delete', code: 'Delete', vk: 46 },
  space: { key: ' ', code: 'Space', vk: 32, text: ' ' },
  arrowup: { key: 'ArrowUp', code: 'ArrowUp', vk: 38 },
  up: { key: 'ArrowUp', code: 'ArrowUp', vk: 38 },
  arrowdown: { key: 'ArrowDown', code: 'ArrowDown', vk: 40 },
  down: { key: 'ArrowDown', code: 'ArrowDown', vk: 40 },
  arrowleft: { key: 'ArrowLeft', code: 'ArrowLeft', vk: 37 },
  left: { key: 'ArrowLeft', code: 'ArrowLeft', vk: 37 },
  arrowright: { key: 'ArrowRight', code: 'ArrowRight', vk: 39 },
  right: { key: 'ArrowRight', code: 'ArrowRight', vk: 39 },
  home: { key: 'Home', code: 'Home', vk: 36 },
  end: { key: 'End', code: 'End', vk: 35 },
  pageup: { key: 'PageUp', code: 'PageUp', vk: 33 },
  pagedown: { key: 'PageDown', code: 'PageDown', vk: 34 },
  insert: { key: 'Insert', code: 'Insert', vk: 45 }
}

function modifierMask(parts: string[]): number {
  let mask = 0
  for (const part of parts) mask |= MODIFIER_BITS[part] ?? 0
  return mask
}

function keyDescriptor(part: string): { key: string; code: string; vk: number; text?: string } {
  const lower = part.toLowerCase()
  if (NAMED_KEYS[lower]) return NAMED_KEYS[lower]
  if (/^f([1-9]|1[0-2])$/.test(lower)) {
    const number = Number(lower.slice(1))
    return { key: part.toUpperCase(), code: part.toUpperCase(), vk: 111 + number }
  }
  if (/^[a-z]$/.test(lower)) {
    return { key: lower, code: `Key${lower.toUpperCase()}`, vk: lower.toUpperCase().charCodeAt(0), text: lower }
  }
  if (/^[0-9]$/.test(lower)) {
    return { key: lower, code: `Digit${lower}`, vk: lower.charCodeAt(0), text: lower }
  }
  throw new Error(`Unsupported key ${part}. Use letters, digits, f1-f12, or named keys (enter, tab, escape, arrows, pageup, pagedown, home, end, backspace, delete, space).`)
}

function refSelector(ref: string): string {
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(ref)) throw new Error('Invalid element reference.')
  return `[data-hermes-ref="${ref}"]`
}

function escapeForJs(value: string): string {
  return JSON.stringify(value)
}

export class BrowserController {
  readonly launcher: BrowserLauncher
  private readonly wsFactory: WebSocketFactory | undefined
  private readonly timeoutMs: number
  private browser: CdpConnection | null = null
  private browserWs: string | null = null
  private page: CdpConnection | null = null
  private pageTargetId: string | null = null
  private inspectCounter = 0

  constructor(deps: BrowserControllerDeps = {}) {
    this.launcher = deps.launcher ?? new BrowserLauncher()
    this.wsFactory = deps.wsFactory
    this.timeoutMs = deps.timeoutMs ?? 20_000
  }

  private async connectBrowser(): Promise<CdpConnection> {
    const status = await this.launcher.ensure()
    if (this.browser && this.browserWs === status.web_socket_url && !this.browser.isClosed) return this.browser
    this.browser?.close()
    this.browser = await openCdp(status.web_socket_url, this.wsFactory)
    this.browserWs = status.web_socket_url
    return this.browser
  }

  async status(): Promise<Record<string, unknown>> {
    const status = await this.launcher.status()
    const pages = status.running && status.endpoint
      ? await this.launcher.listTargets(status.endpoint).catch(() => [])
      : []
    return {
      ...status,
      pages: pages
        .filter(page => page.type === 'page')
        .map(page => ({ id: page.id, url: page.url, title: page.title }))
    }
  }

  private async connectPage(): Promise<{ page: CdpConnection; info: BrowserPageInfo }> {
    const browser = await this.connectBrowser()
    const status = await this.launcher.status()
    if (!status.endpoint) throw new Error('Browser endpoint disappeared.')
    let targets = await this.launcher.listTargets(status.endpoint)
    let pageTarget = targets.find(target => target.type === 'page' && typeof target.webSocketDebuggerUrl === 'string')
    if (!pageTarget) {
      await browser.send('Target.createTarget', { url: 'about:blank' })
      await new Promise(resolve => setTimeout(resolve, 300))
      targets = await this.launcher.listTargets(status.endpoint)
      pageTarget = targets.find(target => target.type === 'page' && typeof target.webSocketDebuggerUrl === 'string')
    }
    if (!pageTarget || typeof pageTarget.webSocketDebuggerUrl !== 'string') {
      throw new Error('The browser has no inspectable page target.')
    }
    const targetId = String(pageTarget.id)
    if (!this.page || this.page.isClosed || this.pageTargetId !== targetId) {
      this.page?.close()
      this.page = await openCdp(pageTarget.webSocketDebuggerUrl, this.wsFactory)
      this.pageTargetId = targetId
      await this.page.send('Page.enable').catch(() => undefined)
      await this.page.send('Runtime.enable').catch(() => undefined)
    }
    return {
      page: this.page,
      info: {
        id: targetId,
        url: String(pageTarget.url ?? ''),
        title: String(pageTarget.title ?? '')
      }
    }
  }

  async open(url: string, options: { headless?: boolean } = {}): Promise<BrowserActionResult> {
    if (options.headless !== undefined) {
      await this.launcher.ensure({ headless: options.headless })
    }
    const { page } = await this.connectPage()
    const loaded = page.waitForEvent('Page.loadEventFired', this.timeoutMs).catch(() => null)
    const navigation = await page.send<{ frameId?: string; errorText?: string }>('Page.navigate', { url }, { timeoutMs: this.timeoutMs })
    if (navigation.errorText) throw new Error(`Navigation failed: ${navigation.errorText}`)
    await loaded
    return this.pageState(page, 'navigated')
  }

  async navigate(url: string): Promise<BrowserActionResult> {
    const { page } = await this.connectPage()
    const loaded = page.waitForEvent('Page.loadEventFired', this.timeoutMs).catch(() => null)
    const navigation = await page.send<{ errorText?: string }>('Page.navigate', { url }, { timeoutMs: this.timeoutMs })
    if (navigation.errorText) throw new Error(`Navigation failed: ${navigation.errorText}`)
    await loaded
    return this.pageState(page, 'navigated')
  }

  private async pageState(page: CdpConnection, detail: string): Promise<BrowserActionResult> {
    const state = await page.send<{ result?: { value?: { url?: string; title?: string } } }>('Runtime.evaluate', {
      expression: '({url: location.href, title: document.title})',
      returnByValue: true
    }).catch(() => null)
    const value = state?.result?.value
    return { ok: true, detail, url: value?.url, title: value?.title }
  }

  async pages(): Promise<BrowserPageInfo[]> {
    const browser = await this.connectBrowser()
    const targets = await browser.send<{ targetInfos?: Array<{ targetId: string; type: string; url: string; title: string }> }>(
      'Target.getTargets'
    )
    return (targets.targetInfos ?? [])
      .filter(target => target.type === 'page')
      .map(target => ({ id: target.targetId, url: target.url, title: target.title }))
  }

  async inspect(options: { max_elements?: number; include_text?: boolean } = {}): Promise<InspectResult> {
    const { page } = await this.connectPage()
    const max = Math.min(MAX_ELEMENTS_LIMIT, Math.max(1, Math.round(options.max_elements ?? 60)))
    const includeText = options.include_text === true
    const prefix = `h${Date.now().toString(36)}${this.inspectCounter++}`
    const expression = `(() => {
      const max = ${max};
      const prefix = ${escapeForJs(prefix)};
      const selector = 'a[href],button,input:not([type=hidden]),textarea,select,[role=button],[role=link],[role=checkbox],[role=tab],[role=menuitem],summary,[contenteditable=true]';
      const nodes = Array.from(document.querySelectorAll(selector));
      const elements = [];
      for (const el of nodes) {
        const rect = el.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) continue;
        const style = getComputedStyle(el);
        if (style.visibility === 'hidden' || style.display === 'none' || Number(style.opacity) === 0) continue;
        const ref = prefix + '-' + (elements.length + 1);
        el.setAttribute('data-hermes-ref', ref);
        const tag = el.tagName.toLowerCase();
        const rawName = el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('placeholder') ||
          (el.innerText || '').trim() || (el.value || '');
        elements.push({
          ref,
          tag,
          role: el.getAttribute('role') || undefined,
          type: el.getAttribute('type') || undefined,
          name: String(rawName).replace(/\\s+/g, ' ').trim().slice(0, 160),
          value: 'value' in el && typeof el.value === 'string' ? el.value.slice(0, 300) : undefined,
          disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
          checked: 'checked' in el ? Boolean(el.checked) : undefined,
          rect: { x: Math.round(rect.x + scrollX), y: Math.round(rect.y + scrollY), width: Math.round(rect.width), height: Math.round(rect.height) }
        });
        if (elements.length >= max) break;
      }
      return {
        url: location.href,
        title: document.title,
        elements,
        truncated: elements.length >= max,
        text: ${includeText ? 'document.body ? document.body.innerText.slice(0, 6000) : ""' : 'undefined'}
      };
    })()`
    const result = await page.send<{ result?: { value?: InspectResult } }>('Runtime.evaluate', {
      expression,
      returnByValue: true,
      awaitPromise: false
    }, { timeoutMs: this.timeoutMs })
    const value = result.result?.value
    if (!value) throw new Error('Inspection returned no value.')
    return value
  }

  private async resolvePoint(options: { ref?: string; selector?: string; x?: number; y?: number }): Promise<{ x: number; y: number }> {
    if (typeof options.x === 'number' && typeof options.y === 'number') {
      return { x: Math.round(options.x), y: Math.round(options.y) }
    }
    const selector = options.ref ? refSelector(options.ref) : options.selector
    if (!selector) throw new Error('Provide ref, selector, or x and y.')
    const { page } = await this.connectPage()
    const expression = `(() => {
      const el = document.querySelector(${escapeForJs(selector)});
      if (!el) return {error: 'not_found'};
      el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
      const rect = el.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) return {error: 'not_visible'};
      return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
    })()`
    const result = await page.send<{ result?: { value?: { x?: number; y?: number; error?: string } } }>('Runtime.evaluate', {
      expression,
      returnByValue: true
    })
    const value = result.result?.value
    if (!value || value.error) {
      throw new Error(value?.error === 'not_found' ? `No element matches ${selector}. Re-run desktop_browser_inspect; references expire after navigation.` : 'Element is not visible.')
    }
    return { x: Math.round(value.x ?? 0), y: Math.round(value.y ?? 0) }
  }

  private async mouse(page: CdpConnection, type: 'mousePressed' | 'mouseReleased' | 'mouseMoved', x: number, y: number, extras: Record<string, unknown>): Promise<void> {
    await page.send('Input.dispatchMouseEvent', { type, x, y, ...extras })
  }

  async click(options: {
    ref?: string
    selector?: string
    x?: number
    y?: number
    button?: 'left' | 'right' | 'middle'
    count?: number
  }): Promise<BrowserActionResult> {
    const { page } = await this.connectPage()
    const point = await this.resolvePoint(options)
    const button = options.button ?? 'left'
    const count = Math.min(3, Math.max(1, Math.round(options.count ?? 1)))
    await this.mouse(page, 'mouseMoved', point.x, point.y, {})
    for (let index = 1; index <= count; index += 1) {
      await this.mouse(page, 'mousePressed', point.x, point.y, { button, clickCount: index, buttons: button === 'left' ? 1 : button === 'right' ? 2 : 4 })
      await this.mouse(page, 'mouseReleased', point.x, point.y, { button, clickCount: index, buttons: 0 })
    }
    const state = await this.pageState(page, `clicked ${button}`)
    return { ...state, x: point.x, y: point.y, click_count: count }
  }

  private async pressKey(page: CdpConnection, combo: string): Promise<void> {
    const parts = combo.split('+').map(part => part.trim()).filter(Boolean)
    if (parts.length === 0) throw new Error('Empty key combination.')
    const descriptor = keyDescriptor(parts[parts.length - 1])
    const modifiers = modifierMask(parts.slice(0, -1).map(part => part.toLowerCase()))
    const base = {
      key: descriptor.key,
      code: descriptor.code,
      windowsVirtualKeyCode: descriptor.vk,
      nativeVirtualKeyCode: descriptor.vk,
      modifiers
    }
    await page.send('Input.dispatchKeyEvent', {
      type: descriptor.text && modifiers === 0 ? 'keyDown' : 'rawKeyDown',
      ...base,
      ...(descriptor.text && modifiers === 0 ? { text: descriptor.text, unmodifiedText: descriptor.text } : {})
    })
    await page.send('Input.dispatchKeyEvent', { type: 'keyUp', ...base })
  }

  async type(options: {
    ref?: string
    selector?: string
    text: string
    clear?: boolean
    submit?: boolean
    mode?: 'insert' | 'value'
  }): Promise<BrowserActionResult> {
    const { page } = await this.connectPage()
    if (options.text.length > 4_000) throw new Error('text is limited to 4000 characters per call.')
    if (options.ref || options.selector) {
      const point = await this.resolvePoint(options)
      await this.mouse(page, 'mouseMoved', point.x, point.y, {})
      await this.mouse(page, 'mousePressed', point.x, point.y, { button: 'left', clickCount: 1, buttons: 1 })
      await this.mouse(page, 'mouseReleased', point.x, point.y, { button: 'left', clickCount: 1, buttons: 0 })
      const selector = options.ref ? refSelector(options.ref) : (options.selector as string)
      await page.send('Runtime.evaluate', {
        expression: `(() => { const el = document.querySelector(${escapeForJs(selector)}); if (!el) return false; el.focus(); return true; })()`,
        returnByValue: true
      })
    }
    if (options.clear) {
      await page.send('Runtime.evaluate', {
        expression: `(() => {
          const el = document.activeElement;
          if (!el) return false;
          if (el.isContentEditable) { document.execCommand('selectAll'); return true; }
          if (typeof el.select === 'function') { el.select(); return true; }
          return false;
        })()`,
        returnByValue: true
      })
    }
    if (options.mode === 'value') {
      const selector = options.ref ? refSelector(options.ref) : options.selector
      if (!selector) throw new Error('mode=value requires ref or selector.')
      const result = await page.send<{ result?: { value?: boolean } }>('Runtime.evaluate', {
        expression: `(() => {
          const el = document.querySelector(${escapeForJs(selector)});
          if (!el) return false;
          const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
          const setter = Object.getOwnPropertyDescriptor(proto, 'value') && Object.getOwnPropertyDescriptor(proto, 'value').set;
          if (setter) setter.call(el, ${escapeForJs(options.text)}); else el.value = ${escapeForJs(options.text)};
          el.dispatchEvent(new Event('input', {bubbles: true}));
          el.dispatchEvent(new Event('change', {bubbles: true}));
          return true;
        })()`,
        returnByValue: true
      })
      if (!result.result?.value) throw new Error('Could not set the field value.')
    } else {
      await page.send('Input.insertText', { text: options.text })
    }
    if (options.submit) await this.pressKey(page, 'enter')
    const state = await this.pageState(page, 'typed')
    return { ...state, characters: options.text.length }
  }

  async key(options: { keys: string }): Promise<BrowserActionResult> {
    const { page } = await this.connectPage()
    await this.pressKey(page, options.keys)
    return this.pageState(page, `pressed ${options.keys}`)
  }

  async scroll(options: { x?: number; y?: number; delta_x?: number; delta_y?: number }): Promise<BrowserActionResult> {
    const { page } = await this.connectPage()
    const metrics = await page.send<{ cssLayoutViewport?: { clientWidth: number; clientHeight: number } }>('Page.getLayoutMetrics').catch(() => null)
    const x = Math.round(options.x ?? (metrics?.cssLayoutViewport?.clientWidth ?? 800) / 2)
    const y = Math.round(options.y ?? (metrics?.cssLayoutViewport?.clientHeight ?? 600) / 2)
    const deltaX = Math.round(options.delta_x ?? 0)
    const deltaY = Math.round(options.delta_y ?? 0)
    if (!deltaX && !deltaY) throw new Error('scroll needs delta_x or delta_y.')
    await page.send('Input.dispatchMouseEvent', { type: 'mouseWheel', x, y, deltaX, deltaY })
    return this.pageState(page, 'scrolled')
  }

  async screenshot(options: { format?: 'png' | 'jpeg'; quality?: number; full_page?: boolean } = {}): Promise<Record<string, unknown>> {
    const { page } = await this.connectPage()
    const format = options.format ?? 'jpeg'
    const params: Record<string, unknown> = {
      format,
      captureBeyondViewport: options.full_page === true
    }
    if (format === 'jpeg') params.quality = Math.min(100, Math.max(1, Math.round(options.quality ?? 75)))
    if (options.full_page) {
      const metrics = await page.send<{ cssContentSize?: { width: number; height: number } }>('Page.getLayoutMetrics')
      const size = metrics.cssContentSize
      if (size) params.clip = { x: 0, y: 0, width: Math.ceil(size.width), height: Math.ceil(size.height), scale: 1 }
    }
    const result = await page.send<{ data?: string }>('Page.captureScreenshot', params, { timeoutMs: 30_000 })
    if (!result.data) throw new Error('Screenshot returned no data.')
    const buffer = Buffer.from(result.data, 'base64')
    const size = imageSize(format, buffer)
    return {
      ok: true,
      format,
      bytes_base64: result.data,
      size_bytes: buffer.length,
      pixel_width: size?.width ?? null,
      pixel_height: size?.height ?? null,
      full_page: options.full_page === true
    }
  }

  async upload(options: { ref?: string; selector?: string; files: string[] }): Promise<BrowserActionResult> {
    const { page } = await this.connectPage()
    const selector = options.ref ? refSelector(options.ref) : options.selector
    if (!selector) throw new Error('upload requires ref or selector.')
    if (options.files.length === 0 || options.files.length > 10) throw new Error('files must contain 1..10 absolute paths.')
    const document = await page.send<{ root?: { nodeId: number } }>('DOM.getDocument', { depth: 1 })
    const rootId = document.root?.nodeId
    if (!rootId) throw new Error('Could not read the DOM root.')
    const query = await page.send<{ nodeId?: number }>('DOM.querySelector', { nodeId: rootId, selector })
    if (!query.nodeId) throw new Error(`No element matches ${selector}.`)
    await page.send('DOM.setFileInputFiles', { nodeId: query.nodeId, files: options.files })
    return this.pageState(page, 'uploaded')
  }

  async download(options: {
    ref?: string
    selector?: string
    x?: number
    y?: number
    timeout_ms?: number
  }): Promise<BrowserActionResult> {
    const browser = await this.connectBrowser()
    const downloadPath = await this.launcher.downloadsPath()
    await browser.send('Browser.setDownloadBehavior', {
      behavior: 'allow',
      downloadPath,
      eventsEnabled: true
    }).catch(async () => {
      const { page } = await this.connectPage()
      await page.send('Page.setDownloadBehavior', { behavior: 'allow', downloadPath })
    })
    const before = await this.launcher.lastDownload()
    const click = await this.click(options)
    const deadline = Date.now() + Math.min(120_000, Math.max(1_000, options.timeout_ms ?? DOWNLOAD_TIMEOUT_MS))
    let latest = before
    let stableSince = 0
    while (Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 300))
      latest = await this.launcher.lastDownload()
      if (latest && (!before || latest.path !== before.path || latest.modified_ms !== before.modified_ms)) {
        if (stableSince === 0) stableSince = Date.now()
        if (Date.now() - stableSince > 800 && latest.size > 0) {
          return { ...click, ok: true, download: latest, download_dir: downloadPath }
        }
      }
    }
    return {
      ...click,
      ok: false,
      detail: 'No completed download appeared before the timeout.',
      download_dir: downloadPath,
      timeout_ms: Math.min(120_000, Math.max(1_000, options.timeout_ms ?? DOWNLOAD_TIMEOUT_MS))
    }
  }

  async waitFor(options: { selector?: string; text?: string; timeout_ms?: number }): Promise<BrowserActionResult> {
    const { page } = await this.connectPage()
    if (!options.selector && !options.text) throw new Error('wait_for requires selector or text.')
    const deadline = Date.now() + Math.min(60_000, Math.max(250, options.timeout_ms ?? 10_000))
    const expression = options.selector
      ? `!!document.querySelector(${escapeForJs(options.selector)})`
      : `(document.body ? document.body.innerText : '').includes(${escapeForJs(options.text as string)})`
    let lastError: string | null = null
    while (Date.now() < deadline) {
      try {
        const result = await page.send<{ result?: { value?: boolean } }>('Runtime.evaluate', {
          expression,
          returnByValue: true
        })
        if (result.result?.value === true) return { ...(await this.pageState(page, 'condition met')), ok: true }
      } catch (error) {
        lastError = error instanceof Error ? error.message : String(error)
      }
      await new Promise(resolve => setTimeout(resolve, 250))
    }
    return {
      ok: false,
      detail: `Condition not met within ${Math.min(60_000, Math.max(250, options.timeout_ms ?? 10_000))}ms.`,
      error: lastError ?? undefined
    }
  }

  async close(): Promise<void> {
    this.page?.close()
    this.page = null
    this.pageTargetId = null
    this.browser?.close()
    this.browser = null
    await this.launcher.closeGracefully().catch(() => undefined)
  }

  async closePage(): Promise<void> {
    this.page?.close()
    this.page = null
    this.pageTargetId = null
  }
}
