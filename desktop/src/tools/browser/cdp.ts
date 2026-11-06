// Minimal Chrome DevTools Protocol client.
//
// The desktop CLI ships with zero runtime dependencies, so this speaks CDP
// over the platform WebSocket (global in Node >= 21, which package.json
// already requires). Tests inject a fake WebSocket factory; the browser
// controller never constructs a socket directly.

export interface WebSocketLike {
  send(data: string): void
  close(code?: number, reason?: string): void
  addEventListener(type: 'open' | 'error' | 'close' | 'message', listener: (event: unknown) => void): void
}

export type WebSocketFactory = (url: string) => WebSocketLike

export function defaultWebSocketFactory(url: string): WebSocketLike {
  const candidate = (globalThis as { WebSocket?: new (url: string) => WebSocketLike }).WebSocket
  if (!candidate) {
    throw new Error(
      'This Node build has no global WebSocket. Hermes-Relay requires Node >= 21 for browser automation.'
    )
  }
  return new candidate(url)
}

interface PendingCall {
  resolve: (value: unknown) => void
  reject: (error: Error) => void
  timer: ReturnType<typeof setTimeout>
  method: string
}

export interface CdpEvent {
  method: string
  params: Record<string, unknown>
  sessionId?: string
}

function messageText(event: unknown): string | null {
  const data = (event as { data?: unknown })?.data
  if (typeof data === 'string') return data
  if (data instanceof ArrayBuffer) return Buffer.from(data).toString('utf8')
  if (Buffer.isBuffer(data)) return data.toString('utf8')
  return null
}

export class CdpConnection {
  private nextId = 1
  private readonly pending = new Map<number, PendingCall>()
  private readonly listeners = new Map<string, Set<(event: CdpEvent) => void>>()
  private closed = false
  private readonly closeHandlers = new Set<(reason: string) => void>()

  constructor(
    private readonly socket: WebSocketLike,
    private readonly defaultTimeoutMs = 15_000
  ) {
    socket.addEventListener('message', event => {
      const text = messageText(event)
      if (!text) return
      let parsed: Record<string, unknown>
      try {
        parsed = JSON.parse(text) as Record<string, unknown>
      } catch {
        return
      }
      if (typeof parsed.id === 'number') {
        const pending = this.pending.get(parsed.id)
        if (!pending) return
        this.pending.delete(parsed.id)
        clearTimeout(pending.timer)
        if (parsed.error) {
          const error = parsed.error as { message?: string; code?: number }
          pending.reject(new Error(`${pending.method}: ${error.message ?? 'CDP error'} (${error.code ?? 'unknown'})`))
        } else {
          pending.resolve(parsed.result)
        }
        return
      }
      if (typeof parsed.method === 'string') {
        const event: CdpEvent = {
          method: parsed.method,
          params: (parsed.params as Record<string, unknown>) ?? {},
          sessionId: typeof parsed.sessionId === 'string' ? parsed.sessionId : undefined
        }
        for (const listener of this.listeners.get(event.method) ?? []) {
          try {
            listener(event)
          } catch {
            // listener errors must not break the socket pump
          }
        }
      }
    })
    socket.addEventListener('close', () => {
      this.closed = true
      const error = new Error('CDP connection closed')
      for (const [id, pending] of this.pending) {
        clearTimeout(pending.timer)
        pending.reject(error)
        this.pending.delete(id)
      }
      for (const handler of this.closeHandlers) handler('closed')
    })
    socket.addEventListener('error', () => {
      for (const handler of this.closeHandlers) handler('socket error')
    })
  }

  get isClosed(): boolean {
    return this.closed
  }

  on(method: string, listener: (event: CdpEvent) => void): () => void {
    const set = this.listeners.get(method) ?? new Set()
    set.add(listener)
    this.listeners.set(method, set)
    return () => set.delete(listener)
  }

  onClose(listener: (reason: string) => void): () => void {
    this.closeHandlers.add(listener)
    return () => this.closeHandlers.delete(listener)
  }

  send<T = Record<string, unknown>>(
    method: string,
    params: Record<string, unknown> = {},
    options: { sessionId?: string; timeoutMs?: number } = {}
  ): Promise<T> {
    if (this.closed) return Promise.reject(new Error('CDP connection is closed'))
    const id = this.nextId++
    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id)
        reject(new Error(`${method} timed out after ${options.timeoutMs ?? this.defaultTimeoutMs}ms`))
      }, options.timeoutMs ?? this.defaultTimeoutMs)
      timer.unref?.()
      this.pending.set(id, {
        resolve: value => resolve(value as T),
        reject,
        timer,
        method
      })
      try {
        this.socket.send(JSON.stringify({ id, method, params, ...(options.sessionId ? { sessionId: options.sessionId } : {}) }))
      } catch (error) {
        clearTimeout(timer)
        this.pending.delete(id)
        reject(error instanceof Error ? error : new Error(String(error)))
      }
    })
  }

  /** Await one event, with a deadline. */
  waitForEvent(method: string, timeoutMs = 15_000): Promise<CdpEvent> {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        off()
        reject(new Error(`Timed out waiting for ${method}`))
      }, timeoutMs)
      timer.unref?.()
      const off = this.on(method, event => {
        clearTimeout(timer)
        off()
        resolve(event)
      })
    })
  }

  close(): void {
    if (this.closed) return
    this.closed = true
    try {
      this.socket.close()
    } catch {
      // already closed
    }
  }
}

export async function openCdp(
  url: string,
  factory: WebSocketFactory = defaultWebSocketFactory,
  timeoutMs = 10_000
): Promise<CdpConnection> {
  const socket = factory(url)
  await new Promise<void>((resolve, reject) => {
    let settled = false
    const timer = setTimeout(() => {
      if (settled) return
      settled = true
      reject(new Error(`Timed out connecting to CDP at ${url}`))
    }, timeoutMs)
    timer.unref?.()
    socket.addEventListener('open', () => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve()
    })
    socket.addEventListener('error', () => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      reject(new Error(`Could not connect to CDP at ${url}`))
    })
  })
  return new CdpConnection(socket)
}
