import assert from 'node:assert/strict'
import test from 'node:test'

import { CdpConnection, type WebSocketLike } from '../src/tools/browser/cdp.js'
import { BrowserController } from '../src/tools/browser/controller.js'
import type { BrowserLauncher } from '../src/tools/browser/launcher.js'

type Listener = (event: unknown) => void

class FakeSocket implements WebSocketLike {
  readonly sent: Array<Record<string, unknown>> = []
  private readonly listeners = new Map<string, Set<Listener>>()
  onSend: ((message: Record<string, unknown>) => void) | null = null

  constructor(private readonly label: string) {}

  send(data: string): void {
    const message = JSON.parse(data) as Record<string, unknown>
    this.sent.push(message)
    this.onSend?.(message)
  }

  close(): void {
    this.emit('close', {})
  }

  addEventListener(type: 'open' | 'error' | 'close' | 'message', listener: Listener): void {
    const set = this.listeners.get(type) ?? new Set<Listener>()
    set.add(listener)
    this.listeners.set(type, set)
  }

  emit(type: string, event: unknown): void {
    for (const listener of this.listeners.get(type) ?? []) listener(event)
  }

  respond(id: number, result: unknown): void {
    this.emit('message', { data: JSON.stringify({ id, result }) })
  }

  emitEvent(method: string, params: Record<string, unknown> = {}): void {
    this.emit('message', { data: JSON.stringify({ method, params }) })
  }

  get socketLabel(): string {
    return this.label
  }
}

function pngBase64(): string {
  const buffer = Buffer.alloc(33)
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]).copy(buffer, 0)
  buffer.writeUInt32BE(13, 8)
  buffer.write('IHDR', 12, 'ascii')
  buffer.writeUInt32BE(640, 16)
  buffer.writeUInt32BE(480, 20)
  return buffer.toString('base64')
}

interface Harness {
  controller: BrowserController
  page: FakeSocket
  browser: FakeSocket
  methods: () => string[]
}

function harness(): Harness {
  const page = new FakeSocket('page')
  const browser = new FakeSocket('browser')
  const sockets = new Map<string, FakeSocket>([['ws://page', page], ['ws://browser', browser]])

  const launcher = {
    profileDir: '/home/tester/.hermes/browser-profile',
    downloadsPath: async () => '/home/tester/.hermes/browser-profile/downloads',
    lastDownload: async () => null,
    ensure: async () => ({
      endpoint: 'http://127.0.0.1:9222',
      web_socket_url: 'ws://browser',
      profile_dir: '/home/tester/.hermes/browser-profile',
      binary_path: '/usr/bin/chromium',
      launched: false,
      browser_version: 'Chromium/140'
    }),
    status: async () => ({
      available: true,
      running: true,
      binary_path: '/usr/bin/chromium',
      profile_dir: '/home/tester/.hermes/browser-profile',
      endpoint: 'http://127.0.0.1:9222',
      web_socket_url: 'ws://browser',
      browser_version: 'Chromium/140'
    }),
    listTargets: async () => [
      {
        id: 'page-1',
        type: 'page',
        url: 'https://example.test/',
        title: 'Example',
        webSocketDebuggerUrl: 'ws://page'
      }
    ],
    closeGracefully: async () => undefined
  } as unknown as BrowserLauncher

  const controller = new BrowserController({
    launcher,
    wsFactory: url => {
      const socket = sockets.get(url)
      if (!socket) throw new Error(`unexpected socket ${url}`)
      setTimeout(() => socket.emit('open', {}), 0)
      return socket
    }
  })

  const methods = () => page.sent.map(message => String(message.method))

  page.onSend = message => {
    const id = message.id as number
    const method = message.method as string
    const params = (message.params as Record<string, unknown>) ?? {}
    setTimeout(() => {
      if (method === 'Page.enable' || method === 'Runtime.enable' || method === 'Input.insertText' ||
          method === 'Input.dispatchKeyEvent' || method === 'Input.dispatchMouseEvent' ||
          method === 'DOM.setFileInputFiles' || method === 'DOM.enable') {
        page.respond(id, {})
        return
      }
      if (method === 'Page.navigate') {
        page.respond(id, { frameId: 'F1' })
        setTimeout(() => page.emitEvent('Page.loadEventFired', { timestamp: Date.now() / 1000 }), 5)
        return
      }
      if (method === 'Runtime.evaluate') {
        const expression = String(params.expression ?? '')
        if (expression.includes('scrollIntoView')) {
          page.respond(id, { result: { value: { x: 150, y: 80 } } })
          return
        }
        if (expression.includes('const max =')) {
          page.respond(id, {
            result: {
              value: {
                url: 'https://example.test/',
                title: 'Example',
                truncated: false,
                text: 'Hello page',
                elements: [
                  {
                    ref: 'h1-1',
                    tag: 'button',
                    name: 'Submit',
                    rect: { x: 100, y: 60, width: 100, height: 40 }
                  }
                ]
              }
            }
          })
          return
        }
        if (expression.includes('location.href')) {
          page.respond(id, { result: { value: { url: 'https://example.test/', title: 'Example' } } })
          return
        }
        page.respond(id, { result: { value: true } })
        return
      }
      if (method === 'DOM.getDocument') {
        page.respond(id, { root: { nodeId: 1 } })
        return
      }
      if (method === 'DOM.querySelector') {
        page.respond(id, { nodeId: 7 })
        return
      }
      if (method === 'Page.getLayoutMetrics') {
        page.respond(id, {
          cssLayoutViewport: { clientWidth: 800, clientHeight: 600 },
          cssContentSize: { width: 800, height: 2400 }
        })
        return
      }
      if (method === 'Page.captureScreenshot') {
        page.respond(id, { data: pngBase64() })
        return
      }
      page.respond(id, {})
    }, 0)
  }

  browser.onSend = message => {
    const id = message.id as number
    const method = message.method as string
    setTimeout(() => {
      if (method === 'Browser.setDownloadBehavior') {
        browser.respond(id, {})
        return
      }
      if (method === 'Target.createTarget') {
        browser.respond(id, { targetId: 'page-1' })
        return
      }
      if (method === 'Target.getTargets') {
        browser.respond(id, {
          targetInfos: [{ targetId: 'page-1', type: 'page', url: 'https://example.test/', title: 'Example' }]
        })
        return
      }
      browser.respond(id, {})
    }, 0)
  }

  return { controller, page, browser, methods }
}

test('browser inspect returns structured refs and page identity', async () => {
  const { controller } = harness()
  const result = await controller.inspect({ include_text: true })
  assert.equal(result.url, 'https://example.test/')
  assert.equal(result.title, 'Example')
  assert.equal(result.elements[0].ref, 'h1-1')
  assert.equal(result.elements[0].name, 'Submit')
  assert.equal(result.text, 'Hello page')
})

test('browser click resolves a ref to viewport coordinates and dispatches real mouse events', async () => {
  const { controller, page } = harness()
  const result = await controller.click({ ref: 'h1-1' })
  const clicks = page.sent.filter(message => message.method === 'Input.dispatchMouseEvent')
  assert.equal(clicks.length, 3) // moved, pressed, released
  const pressed = clicks.find(message => (message.params as Record<string, unknown>).type === 'mousePressed')
  assert.equal((pressed?.params as Record<string, unknown>).x, 150)
  assert.equal((pressed?.params as Record<string, unknown>).y, 80)
  assert.equal(result.ok, true)
})

test('browser type inserts text and submits with Enter', async () => {
  const { controller, page } = harness()
  await controller.type({ ref: 'h1-1', text: 'hello', submit: true, clear: true })
  const insert = page.sent.find(message => message.method === 'Input.insertText')
  assert.equal((insert?.params as Record<string, unknown>).text, 'hello')
  const keys = page.sent.filter(message => message.method === 'Input.dispatchKeyEvent')
  assert.equal(keys.length, 2)
  assert.equal((keys[0].params as Record<string, unknown>).key, 'Enter')
})

test('browser upload sets the selected file paths on the input node', async () => {
  const { controller, page } = harness()
  await controller.upload({ selector: 'input[type=file]', files: ['/tmp/a.txt', '/tmp/b.txt'] })
  const upload = page.sent.find(message => message.method === 'DOM.setFileInputFiles')
  assert.equal(upload?.params && (upload.params as Record<string, unknown>).nodeId, 7)
  assert.deepEqual((upload?.params as Record<string, unknown>).files, ['/tmp/a.txt', '/tmp/b.txt'])
})

test('browser navigation waits for the load event', async () => {
  const { controller } = harness()
  const result = await controller.open('https://example.test/login')
  assert.equal(result.ok, true)
  assert.equal(result.url, 'https://example.test/')
})

test('browser screenshot returns pixel dimensions from the PNG header', async () => {
  const { controller } = harness()
  const result = await controller.screenshot({ format: 'png' })
  assert.equal(result.pixel_width, 640)
  assert.equal(result.pixel_height, 480)
  assert.equal(typeof result.bytes_base64, 'string')
})

test('browser scroll dispatches a wheel event at the viewport centre', async () => {
  const { controller, page } = harness()
  await controller.scroll({ delta_y: 600 })
  const wheel = page.sent.find(message => message.method === 'Input.dispatchMouseEvent')
  assert.equal((wheel?.params as Record<string, unknown>).type, 'mouseWheel')
  assert.equal((wheel?.params as Record<string, unknown>).x, 400)
  assert.equal((wheel?.params as Record<string, unknown>).deltaY, 600)
})

test('CDP rejects in-flight calls when the socket closes', async () => {
  const socket = new FakeSocket('unit')
  const connection = new CdpConnection(socket)
  const pending = connection.send('Page.navigate', { url: 'https://example.test' })
  socket.emit('close', {})
  await assert.rejects(pending, /CDP connection closed/)
})
