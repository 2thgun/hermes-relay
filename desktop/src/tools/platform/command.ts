// Process boundary for the platform adapters.
//
// Everything the adapters do goes through here so tests can supply a fake
// runner and assert on the exact argv a Hyprland/X11 session would receive.
// The default runner is deliberately dependency-free (spawn + buffers) and
// caps output so one runaway command cannot blow up the daemon.

import { spawn } from 'node:child_process'
import { access, constants as fsConstants } from 'node:fs/promises'
import { delimiter, isAbsolute, join } from 'node:path'

import type {
  UiCommandBufferResult,
  UiCommandOptions,
  UiCommandResult,
  UiCommandRunner
} from './types.js'

const DEFAULT_TIMEOUT_MS = 15_000
const MAX_TEXT_BYTES = 8 * 1024 * 1024
const MAX_BUFFER_BYTES = 64 * 1024 * 1024

interface SpawnCapture {
  ran: boolean
  exitCode: number
  stdout: Buffer
  stderr: string
  tooLarge: boolean
}

function runSpawn(
  cmd: string,
  args: readonly string[],
  opts: UiCommandOptions | undefined,
  maxBytes: number
): Promise<SpawnCapture> {
  return new Promise(resolve => {
    let child
    try {
      child = spawn(cmd, [...args], {
        stdio: ['pipe', 'pipe', 'pipe'],
        windowsHide: true,
        env: opts?.env
      })
    } catch (error) {
      resolve({
        ran: false,
        exitCode: -1,
        stdout: Buffer.alloc(0),
        stderr: error instanceof Error ? error.message : String(error),
        tooLarge: false
      })
      return
    }

    const stdoutChunks: Buffer[] = []
    let stdoutBytes = 0
    let stderr = ''
    let tooLarge = false
    let settled = false
    let timedOut = false

    const finish = (exitCode: number, ran: boolean) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      opts?.signal?.removeEventListener('abort', onAbort)
      resolve({
        ran,
        exitCode,
        stdout: Buffer.concat(stdoutChunks),
        stderr,
        tooLarge
      })
    }

    const kill = () => {
      try {
        child.kill('SIGKILL')
      } catch {
        // already gone
      }
    }
    const onAbort = () => kill()
    const timer = setTimeout(() => {
      timedOut = true
      kill()
    }, opts?.timeoutMs ?? DEFAULT_TIMEOUT_MS)
    timer.unref?.()
    opts?.signal?.addEventListener('abort', onAbort, { once: true })

    child.stdout?.on('data', (chunk: Buffer) => {
      if (tooLarge) return
      stdoutBytes += chunk.length
      if (stdoutBytes > maxBytes) {
        tooLarge = true
        kill()
        return
      }
      stdoutChunks.push(chunk)
    })
    child.stderr?.on('data', (chunk: Buffer) => {
      if (stderr.length > 64_000) return
      stderr += chunk.toString('utf8')
    })
    child.on('error', (error: NodeJS.ErrnoException) => {
      if (error.code === 'ENOENT') {
        finish(-1, false)
        return
      }
      stderr += error.message
      finish(-1, true)
    })
    child.on('close', code => {
      if (timedOut) {
        stderr += `\n[timed out after ${opts?.timeoutMs ?? DEFAULT_TIMEOUT_MS}ms]`
      }
      finish(typeof code === 'number' ? code : -1, true)
    })

    if (opts?.stdin !== undefined) {
      child.stdin?.end(opts.stdin)
    } else {
      child.stdin?.end()
    }
  })
}

export const defaultCommandRunner: UiCommandRunner = {
  async text(cmd, args, opts): Promise<UiCommandResult> {
    const result = await runSpawn(cmd, args, opts, MAX_TEXT_BYTES)
    return {
      ran: result.ran,
      exitCode: result.exitCode,
      stdout: result.stdout.toString('utf8'),
      stderr: result.tooLarge ? `${result.stderr}\n[output exceeded ${MAX_TEXT_BYTES} bytes]` : result.stderr
    }
  },
  async buffer(cmd, args, opts): Promise<UiCommandBufferResult> {
    const result = await runSpawn(cmd, args, opts, MAX_BUFFER_BYTES)
    return {
      ran: result.ran,
      exitCode: result.exitCode,
      stdout: result.stdout,
      stderr: result.tooLarge ? `${result.stderr}\n[output exceeded ${MAX_BUFFER_BYTES} bytes]` : result.stderr
    }
  }
}

/** Resolve an executable without invoking a shell. */
export async function whichBinary(
  name: string,
  pathValue: string | undefined = process.env.PATH,
  env: NodeJS.ProcessEnv = process.env
): Promise<string | null> {
  if (!name) return null
  const candidates: string[] = []
  if (isAbsolute(name)) {
    candidates.push(name)
  } else {
    const directories = (pathValue ?? '').split(delimiter).filter(Boolean)
    for (const directory of directories) {
      candidates.push(join(directory, name))
      if (process.platform === 'win32') {
        for (const extension of ['.exe', '.cmd', '.bat']) candidates.push(join(directory, `${name}${extension}`))
      }
    }
    // Common user-local install locations that login services often miss.
    if (process.platform !== 'win32') {
      candidates.push(join(env.HOME ?? '', '.local', 'bin', name), join(env.HOME ?? '', '.hermes', 'bin', name))
    }
  }
  for (const candidate of candidates) {
    if (!candidate) continue
    try {
      await access(candidate, fsConstants.X_OK)
      return candidate
    } catch {
      // try next
    }
  }
  return null
}

export function parseJson<T>(text: string): T | null {
  const trimmed = text.trim()
  if (!trimmed) return null
  try {
    return JSON.parse(trimmed) as T
  } catch {
    return null
  }
}

/** Parse `hyprctl cursorpos` ("1920, 540") defensively. */
export function parseCursorPos(text: string): { x: number; y: number } | null {
  const match = /(-?\d+)\s*,\s*(-?\d+)/.exec(text)
  if (!match) return null
  return { x: Number(match[1]), y: Number(match[2]) }
}
