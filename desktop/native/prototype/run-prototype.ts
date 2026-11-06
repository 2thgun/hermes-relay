// Live prototype: run the real HyprlandAdapter against a real wlroots
// compositor (sway) through the test-only hyprctl shim.
//
// It proves the parts that are hard to unit-test:
//   - grim capture + coordinate metadata
//   - hyprctl-driven window discovery/focus
//   - wl-input absolute move, click, drag, scroll
//   - wtype text input
//   - post-action verification (cursor + active window + pixels)
//
// Run:
//   XDG_RUNTIME_DIR=... WAYLAND_DISPLAY=wayland-1 SWAYSOCK=... \
//     PATH="$PWD/desktop/native/prototype:$PATH" \
//     npx tsx desktop/native/prototype/run-prototype.ts

import { createHash } from 'node:crypto'
import { promises as fs } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

import { HyprlandAdapter } from '../../src/tools/platform/hyprland.js'

const stateDir = process.env.RELAY_PROTO_STATE ?? '/tmp/relay-proto'
const realHelper = process.env.RELAY_PROTO_REAL_WL_INPUT ?? join(homedir(), '.hermes/bin/wl-input')
const wevLog = process.env.RELAY_PROTO_WEV_LOG ?? '/tmp/wev3.log'

function hash(base64: string | undefined): string | null {
  return base64 ? createHash('sha256').update(Buffer.from(base64, 'base64')).digest('hex').slice(0, 16) : null
}

async function readLog(): Promise<string> {
  try {
    return await fs.readFile(wevLog, 'utf8')
  } catch {
    return ''
  }
}

async function main(): Promise<void> {
  await fs.mkdir(stateDir, { recursive: true })
  process.env.RELAY_PROTO_STATE = stateDir
  process.env.RELAY_PROTO_REAL_WL_INPUT = realHelper
  process.env.RELAY_PROTO_WL_INPUT = realHelper
  process.env.HERMES_RELAY_WL_INPUT = join(stateDir, 'wl-input-wrap')

  const adapter = new HyprlandAdapter({
    env: process.env,
    home: homedir()
  })

  const report: Record<string, unknown> = {}
  const environment = await adapter.environment()
  report.environment = {
    graphical: environment.graphical,
    compositor: environment.compositor,
    session: environment.session,
    dependencies: environment.dependencies,
    warnings: environment.warnings
  }

  const capabilities = await adapter.capabilities()
  report.capabilities = capabilities.map(capability => ({
    id: capability.id,
    available: capability.available,
    backend: capability.backend
  }))

  const windows = await adapter.windows()
  report.windows = windows.map(window => ({ id: window.id, app: window.app, title: window.title, rect: window.rect }))
  const foot = windows.find(window => window.app === 'foot')
  const wev = windows.find(window => window.app === 'wev')
  if (!foot || !wev) throw new Error(`expected foot + wev windows, got ${JSON.stringify(report.windows)}`)

  // ── Window focus + typing + pixel verification ────────────────────────
  const focus = await adapter.focusWindow(foot.id)
  report.focus = focus
  if (focus.verification?.ok !== true) throw new Error('focus verification failed')

  const before = await adapter.capture({ target: 'display', display: 'HEADLESS-1' })
  const typed = await adapter.keyboard({ text: 'prototype hyprland input\n', verify: false })
  await new Promise(resolve => setTimeout(resolve, 300))
  const after = await adapter.capture({ target: 'display', display: 'HEADLESS-1' })
  report.typing = {
    backend: typed.backend,
    before_hash: hash(before.bytes_base64),
    after_hash: hash(after.bytes_base64),
    changed: hash(before.bytes_base64) !== hash(after.bytes_base64)
  }

  // ── Click + drag + scroll on the event-logging window ─────────────────
  const click = await adapter.pointer({ action: 'click', x: 900, y: 300, button: 'left' })
  report.click = {
    backend: click.backend,
    cursor: click.verification?.cursor,
    active_window: click.verification?.active_window?.app,
    verified: click.verification?.ok
  }
  if (click.verification?.active_window?.app !== 'wev') {
    throw new Error(`click did not focus wev: ${JSON.stringify(click.verification)}`)
  }

  const logBeforeDrag = await readLog()
  const drag = await adapter.pointer({
    action: 'drag',
    x: 700,
    y: 200,
    to_x: 1000,
    to_y: 500,
    duration_ms: 400
  })
  await new Promise(resolve => setTimeout(resolve, 200))
  const logAfterDrag = await readLog()
  const dragEvents = logAfterDrag
    .slice(logBeforeDrag.length)
    .split('\n')
    .filter(line => /wl_pointer\] (motion|button)/.test(line))
  report.drag = {
    backend: drag.backend,
    verification: drag.verification,
    events: dragEvents.length,
    pressed: dragEvents.some(line => /button: 272 \(left\), state: 1/.test(line)),
    released: dragEvents.some(line => /button: 272 \(left\), state: 0/.test(line)),
    last_motion: dragEvents.filter(line => line.includes('motion')).at(-1)?.trim()
  }

  const logBeforeScroll = await readLog()
  const scroll = await adapter.pointer({ action: 'scroll', x: 900, y: 300, delta_y: 3 })
  await new Promise(resolve => setTimeout(resolve, 200))
  const logAfterScroll = await readLog()
  report.scroll = {
    backend: scroll.backend,
    axis_events: logAfterScroll
      .slice(logBeforeScroll.length)
      .split('\n')
      .filter(line => line.includes('axis')).map(line => line.trim())
  }

  // ── Region capture metadata ───────────────────────────────────────────
  const region = await adapter.capture({
    target: 'region',
    region: { x: 100, y: 80, width: 320, height: 200 },
    format: 'png'
  })
  report.region_capture = {
    origin: region.origin,
    scale: region.scale,
    pixel: [region.pixel_width, region.pixel_height]
  }

  console.log(JSON.stringify(report, null, 2))
}

main().catch(error => {
  console.error(error)
  process.exit(1)
})
