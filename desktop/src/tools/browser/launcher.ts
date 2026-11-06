// Browser lifecycle: one dedicated automation profile per device.
//
// We never attach to the user's normal Chrome profile in the initial release.
// A dedicated --user-data-dir keeps cookies, localStorage and downloads
// separate, makes "close the browser" safe, and avoids touching signed-in
// sessions. Attaching existing profiles is a later, explicit opt-in.

import { spawn } from 'node:child_process'
import { closeSync, existsSync, openSync } from 'node:fs'
import { mkdir, readFile, rm, stat } from 'node:fs/promises'
import { homedir } from 'node:os'
import { join } from 'node:path'

import { whichBinary } from '../platform/command.js'
const DEVTOOLS_PORT_FILE = 'DevToolsActivePort'
const LAUNCH_TIMEOUT_MS = 15_000

export interface BrowserLauncherDeps {
  env?: NodeJS.ProcessEnv
  home?: string
  which?: (name: string) => Promise<string | null>
  spawnDetached?: (binary: string, args: readonly string[]) => void
  fetchJson?: (url: string, timeoutMs: number) => Promise<Record<string, unknown> | null>
}

export interface BrowserStatus {
  available: boolean
  running: boolean
  binary_path?: string
  profile_dir: string
  endpoint?: string
  web_socket_url?: string
  browser_version?: string
  reason?: string
}

export interface BrowserLaunchResult {
  endpoint: string
  web_socket_url: string
  profile_dir: string
  binary_path: string
  launched: boolean
  browser_version?: string
}

const LINUX_NAMES = [
  'google-chrome',
  'google-chrome-stable',
  'chromium',
  'chromium-browser',
  'helium-browser',
  'helium',
  'brave-browser',
  'microsoft-edge',
  'microsoft-edge-stable'
]
const MAC_NAMES = ['Google Chrome', 'Chromium', 'Brave Browser', 'Microsoft Edge']
const WINDOWS_PATHS = [
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Chromium\\Application\\chrome.exe'
]

async function fetchJson(url: string, timeoutMs: number): Promise<Record<string, unknown> | null> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  timer.unref?.()
  try {
    const response = await fetch(url, { signal: controller.signal })
    if (!response.ok) return null
    return (await response.json()) as Record<string, unknown>
  } catch {
    return null
  } finally {
    clearTimeout(timer)
  }
}

export class BrowserLauncher {
  private readonly env: NodeJS.ProcessEnv
  private readonly home: string
  private readonly which: (name: string) => Promise<string | null>
  private readonly spawnDetached: (binary: string, args: readonly string[]) => void
  private readonly fetchJsonImpl: (url: string, timeoutMs: number) => Promise<Record<string, unknown> | null>
  private binaryCache: string | null | undefined

  constructor(deps: BrowserLauncherDeps = {}) {
    this.env = deps.env ?? process.env
    this.home = deps.home ?? homedir()
    this.which = deps.which ?? (name => whichBinary(name, this.env.PATH, this.env))
    this.spawnDetached = deps.spawnDetached ?? ((binary, args) => {
      const logPath = join(this.profileDir, 'chromium.log')
      let logFd: number | null = null
      try {
        logFd = openSync(logPath, 'a')
      } catch {
        logFd = null
      }
      const child = spawn(binary, [...args], {
        detached: true,
        stdio: ['ignore', logFd ?? 'ignore', logFd ?? 'ignore'],
        windowsHide: false,
        env: this.env
      })
      child.unref()
      if (logFd !== null) {
        try {
          closeSync(logFd)
        } catch {
          // child owns its duplicate
        }
      }
    })
    this.fetchJsonImpl = deps.fetchJson ?? fetchJson
  }

  get profileDir(): string {
    return this.env.HERMES_RELAY_BROWSER_PROFILE?.trim() || join(this.home, '.hermes', 'browser-profile')
  }

  private get downloadsDir(): string {
    return join(this.profileDir, 'downloads')
  }

  async binary(): Promise<string | null> {
    if (this.binaryCache !== undefined) return this.binaryCache
    const configured = this.env.HERMES_RELAY_BROWSER?.trim()
    if (configured) {
      const resolved = (await this.which(configured)) ?? (existsSync(configured) ? configured : null)
      this.binaryCache = resolved
      return resolved
    }
    const names = process.platform === 'win32'
      ? ['chrome.exe', 'msedge.exe', 'brave.exe']
      : process.platform === 'darwin' ? MAC_NAMES : LINUX_NAMES
    for (const name of names) {
      const found = await this.which(name)
      if (found) {
        this.binaryCache = found
        return found
      }
    }
    if (process.platform === 'win32') {
      for (const candidate of WINDOWS_PATHS) {
        if (existsSync(candidate)) {
          this.binaryCache = candidate
          return candidate
        }
      }
    }
    if (process.platform === 'darwin') {
      for (const name of MAC_NAMES) {
        const candidate = `/Applications/${name}.app/Contents/MacOS/${name.replaceAll(' ', '')}`
        if (existsSync(candidate)) {
          this.binaryCache = candidate
          return candidate
        }
      }
    }
    this.binaryCache = null
    return null
  }

  private async readEndpoint(): Promise<{ port: number; path: string } | null> {
    try {
      const text = await readFile(join(this.profileDir, DEVTOOLS_PORT_FILE), 'utf8')
      const [portLine, pathLine] = text.trim().split('\n')
      const port = Number(portLine)
      if (!Number.isInteger(port) || port <= 0 || port > 65535 || !pathLine?.startsWith('/')) return null
      return { port, path: pathLine.trim() }
    } catch {
      return null
    }
  }

  async status(): Promise<BrowserStatus> {
    const binary = await this.binary()
    const endpoint = await this.readEndpoint()
    if (!binary) {
      return {
        available: false,
        running: false,
        profile_dir: this.profileDir,
        reason: 'No Chrome/Chromium/Edge/Brave binary found. Set HERMES_RELAY_BROWSER to an absolute path.',
        ...(endpoint ? { endpoint: `http://127.0.0.1:${endpoint.port}` } : {})
      }
    }
    if (!endpoint) {
      return { available: true, running: false, binary_path: binary, profile_dir: this.profileDir }
    }
    const version = await this.fetchJsonImpl(`http://127.0.0.1:${endpoint.port}/json/version`, 2_000)
    if (!version) {
      return {
        available: true,
        running: false,
        binary_path: binary,
        profile_dir: this.profileDir,
        reason: 'A DevToolsActivePort file exists but the browser is not answering; it will be replaced on launch.'
      }
    }
    return {
      available: true,
      running: true,
      binary_path: binary,
      profile_dir: this.profileDir,
      endpoint: `http://127.0.0.1:${endpoint.port}`,
      web_socket_url: typeof version.webSocketDebuggerUrl === 'string'
        ? version.webSocketDebuggerUrl
        : `ws://127.0.0.1:${endpoint.port}${endpoint.path}`,
      browser_version: typeof version.Browser === 'string' ? version.Browser : undefined
    }
  }

  async ensure(options: { headless?: boolean } = {}): Promise<BrowserLaunchResult> {
    const current = await this.status()
    if (current.running && current.endpoint && current.web_socket_url && current.binary_path) {
      return {
        endpoint: current.endpoint,
        web_socket_url: current.web_socket_url,
        profile_dir: current.profile_dir,
        binary_path: current.binary_path,
        launched: false,
        browser_version: current.browser_version
      }
    }
    const binary = await this.binary()
    if (!binary) {
      throw new Error(
        'No Chrome/Chromium/Edge/Brave binary found. Install one or set HERMES_RELAY_BROWSER to its absolute path.'
      )
    }
    await mkdir(this.profileDir, { recursive: true, mode: 0o700 })
    await mkdir(this.downloadsDir, { recursive: true, mode: 0o700 })
    // A stale port file would make us connect to another process.
    await rm(join(this.profileDir, DEVTOOLS_PORT_FILE), { force: true }).catch(() => undefined)

    const headless = options.headless ?? this.env.HERMES_RELAY_BROWSER_HEADLESS === '1'
    const args = [
      '--remote-debugging-port=0',
      `--user-data-dir=${this.profileDir}`,
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-features=Translate,MediaRouter',
      '--disable-background-networking',
      '--disable-sync',
      '--disable-extensions',
      `--class=HermesRelayBrowser`,
      'about:blank'
    ]
    if (headless) args.unshift('--headless=new')
    // Local operator escape hatch (containers need --no-sandbox). Profile and
    // debug-port flags stay under our control so isolation cannot be broken.
    const extra = (this.env.HERMES_RELAY_BROWSER_ARGS ?? '')
      .split(/\s+/)
      .filter(Boolean)
      .filter(arg => !/^--(user-data-dir|remote-debugging-port|headless)/i.test(arg))
    if (extra.length) args.unshift(...extra)
    this.spawnDetached(binary, args)

    const deadline = Date.now() + LAUNCH_TIMEOUT_MS
    while (Date.now() < deadline) {
      const endpoint = await this.readEndpoint()
      if (endpoint) {
        const version = await this.fetchJsonImpl(`http://127.0.0.1:${endpoint.port}/json/version`, 1_500)
        if (version) {
          return {
            endpoint: `http://127.0.0.1:${endpoint.port}`,
            web_socket_url: typeof version.webSocketDebuggerUrl === 'string'
              ? version.webSocketDebuggerUrl
              : `ws://127.0.0.1:${endpoint.port}${endpoint.path}`,
            profile_dir: this.profileDir,
            binary_path: binary,
            launched: true,
            browser_version: typeof version.Browser === 'string' ? version.Browser : undefined
          }
        }
      }
      await new Promise(resolve => setTimeout(resolve, 200))
    }
    throw new Error(`Browser did not expose a DevTools endpoint within ${LAUNCH_TIMEOUT_MS}ms.`)
  }

  async listTargets(endpoint: string): Promise<Array<Record<string, unknown>>> {
    const version = await this.fetchJsonImpl(`${endpoint}/json/list`, 3_000)
    return Array.isArray(version) ? version as Array<Record<string, unknown>> : []
  }

  async closeGracefully(): Promise<void> {
    const status = await this.status()
    if (!status.running || !status.endpoint) return
    const { openCdp } = await import('./cdp.js')
    if (!status.web_socket_url) return
    const connection = await openCdp(status.web_socket_url)
    try {
      await connection.send('Browser.close', {}, { timeoutMs: 5_000 })
    } finally {
      connection.close()
    }
  }

  async lastDownload(): Promise<{ path: string; size: number; modified_ms: number } | null> {
    try {
      const names = await import('node:fs/promises').then(fs => fs.readdir(this.downloadsDir))
      let newest: { path: string; size: number; modified_ms: number } | null = null
      for (const name of names) {
        if (name.endsWith('.crdownload') || name.endsWith('.part')) continue
        const path = join(this.downloadsDir, name)
        const info = await stat(path).catch(() => null)
        if (!info?.isFile()) continue
        if (!newest || info.mtimeMs > newest.modified_ms) {
          newest = { path, size: info.size, modified_ms: info.mtimeMs }
        }
      }
      return newest
    } catch {
      return null
    }
  }

  async downloadsPath(): Promise<string> {
    await mkdir(this.downloadsDir, { recursive: true, mode: 0o700 })
    return this.downloadsDir
  }
}
