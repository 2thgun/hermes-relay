import assert from 'node:assert/strict'
import test from 'node:test'

import { HyprlandAdapter } from '../src/tools/platform/hyprland.js'
import { UiError, type UiCommandBufferResult, type UiCommandResult, type UiCommandRunner } from '../src/tools/platform/types.js'

interface Call {
  cmd: string
  args: string[]
  stdin?: string
}

function pngHeader(width: number, height: number): Buffer {
  const buffer = Buffer.alloc(33)
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]).copy(buffer, 0)
  buffer.writeUInt32BE(13, 8)
  buffer.write('IHDR', 12, 'ascii')
  buffer.writeUInt32BE(width, 16)
  buffer.writeUInt32BE(height, 20)
  return buffer
}

class FakeRunner implements UiCommandRunner {
  readonly calls: Call[] = []
  constructor(private readonly respond: (call: Call) => Partial<UiCommandResult> & { stdoutBuffer?: Buffer } | null) {}

  async text(cmd: string, args: readonly string[], opts?: { stdin?: string }): Promise<UiCommandResult> {
    const call = { cmd, args: [...args], stdin: opts?.stdin }
    this.calls.push(call)
    const response = this.respond(call)
    return {
      ran: response?.ran ?? true,
      exitCode: response?.exitCode ?? 0,
      stdout: response?.stdout ?? '',
      stderr: response?.stderr ?? ''
    }
  }

  async buffer(cmd: string, args: readonly string[], opts?: { stdin?: string }): Promise<UiCommandBufferResult> {
    const call = { cmd, args: [...args], stdin: opts?.stdin }
    this.calls.push(call)
    const response = this.respond(call)
    return {
      ran: response?.ran ?? true,
      exitCode: response?.exitCode ?? 0,
      stdout: response?.stdoutBuffer ?? Buffer.alloc(0),
      stderr: response?.stderr ?? ''
    }
  }
}

const MONITORS = [
  { id: 0, name: 'DP-1', width: 2560, height: 1440, x: 0, y: 0, scale: 1.25, transform: 0, focused: true },
  { id: 1, name: 'HDMI-A-1', width: 1920, height: 1080, x: 2048, y: 0, scale: 1, transform: 0, focused: false }
]

const CLIENTS = [
  {
    address: '0x55aa11',
    mapped: true,
    hidden: false,
    at: [100, 120],
    size: [800, 600],
    workspace: { id: 1, name: '1' },
    monitor: 0,
    class: 'foot',
    title: 'terminal',
    pid: 4242,
    focusHistoryID: 0
  },
  {
    address: '0x55bb22',
    mapped: true,
    hidden: false,
    at: [2100, 40],
    size: [600, 400],
    workspace: { id: 2, name: '2' },
    monitor: 1,
    class: 'firefox',
    title: 'docs',
    pid: 5151,
    focusHistoryID: -1
  }
]

function baseResponder(
  overrides: (call: Call) => Partial<UiCommandResult> | null = () => null,
  version = '0.54.3'
) {
  return (call: Call): Partial<UiCommandResult> & { stdoutBuffer?: Buffer } | null => {
    const override = overrides(call)
    if (override) return override
    const name = call.cmd.split('/').pop()
    if (name === 'hyprctl' && call.args[0] === 'version') {
      return { stdout: JSON.stringify({ version, branch: 'main' }) }
    }
    if (name === 'hyprctl' && call.args[0] === 'monitors') {
      return { stdout: JSON.stringify(MONITORS) }
    }
    if (name === 'hyprctl' && call.args[0] === 'clients') {
      return { stdout: JSON.stringify(CLIENTS) }
    }
    if (name === 'hyprctl' && call.args[0] === 'cursorpos') {
      return { stdout: '300, 200\n' }
    }
    if (name === 'hyprctl' && call.args[0] === 'activewindow') {
      return { stdout: JSON.stringify({ address: '0x55aa11', class: 'foot', title: 'terminal' }) }
    }
    if (name === 'hyprctl' && call.args[0] === 'dispatch') {
      return { stdout: 'ok' }
    }
    if (name === 'grim') {
      return { stdoutBuffer: pngHeader(2048, 1152) }
    }
    if (name === 'wl-input' || name === 'wtype' || name === 'wlrctl') {
      return { stdout: '' }
    }
    return null
  }
}

function makeAdapter(
  runner: FakeRunner,
  available: string[] = ['hyprctl', 'grim', 'wtype', 'wl-input', 'wlrctl'],
  version = '0.54.3'
) {
  return new HyprlandAdapter({
    runner,
    platform: 'linux',
    env: {
      WAYLAND_DISPLAY: 'wayland-1',
      HYPRLAND_INSTANCE_SIGNATURE: 'abc123',
      XDG_RUNTIME_DIR: '/run/user/1000',
      PATH: '/usr/bin',
      HOME: '/home/tester'
    },
    home: '/home/tester',
    which: async name => (available.includes(name) ? `/usr/bin/${name}` : null)
  })
}

test('Hyprland 0.56 uses Lua dispatch for window focus', async () => {
  const runner = new FakeRunner(baseResponder(undefined, '0.56.2'))
  const adapter = makeAdapter(runner, undefined, '0.56.2')
  const result = await adapter.focusWindow('0x55aa11')
  const dispatch = runner.calls.find(call => call.cmd.endsWith('hyprctl') && call.args[0] === 'dispatch')
  assert.deepEqual(dispatch?.args[1], 'hl.dsp.focus({window="address:0x55aa11"})')
  assert.equal(result.verification?.ok, true)
})

test('Hyprland 0.56 uses Lua dispatch for the wlrctl cursor fallback', async () => {
  const runner = new FakeRunner(baseResponder(undefined, '0.56.2'))
  const adapter = makeAdapter(runner, ['hyprctl', 'grim', 'wtype', 'wlrctl'], '0.56.2')
  await adapter.pointer({ action: 'click', x: 300, y: 200 })
  const dispatch = runner.calls.find(call => call.cmd.endsWith('hyprctl') && call.args[0] === 'dispatch')
  assert.deepEqual(dispatch?.args[1], 'hl.dsp.cursor.move({x=300, y=200})')
})

test('Hyprland reports logical display geometry and scaled coordinates', async () => {
  const runner = new FakeRunner(baseResponder())
  const adapter = makeAdapter(runner)
  const displays = await adapter.displays()
  assert.equal(displays.length, 2)
  assert.deepEqual(displays[0].rect, { x: 0, y: 0, width: 2048, height: 1152 })
  assert.equal(displays[0].scale, 1.25)
  assert.deepEqual(displays[1].rect, { x: 2048, y: 0, width: 1920, height: 1080 })
})

test('Hyprland lists windows with stable addresses and focus state', async () => {
  const runner = new FakeRunner(baseResponder())
  const adapter = makeAdapter(runner)
  const windows = await adapter.windows()
  assert.equal(windows.length, 2)
  assert.equal(windows[0].id, '0x55aa11')
  assert.equal(windows[0].app, 'foot')
  assert.equal(windows[0].focused, true)
  assert.equal(windows[1].display, 'HDMI-A-1')
  assert.equal(windows[1].focused, false)
})

test('Hyprland click is one atomic wl-input call in layout space', async () => {
  const runner = new FakeRunner(baseResponder())
  const adapter = makeAdapter(runner)
  const result = await adapter.pointer({ action: 'click', x: 300, y: 200, button: 'left' })
  const helperCall = runner.calls.find(call => call.cmd.endsWith('wl-input'))
  assert.ok(helperCall, 'expected wl-input to be invoked')
  assert.deepEqual(helperCall.args, ['--space', '0', '0', '3968', '1152', 'click', '300', '200', 'left', '1'])
  assert.equal(result.backend, 'wl-input')
  assert.equal(result.verification?.cursor?.x, 300)
})

test('Hyprland drag passes the destination and duration to wl-input', async () => {
  const runner = new FakeRunner(baseResponder())
  const adapter = makeAdapter(runner)
  const result = await adapter.pointer({
    action: 'drag',
    x: 120,
    y: 140,
    to_x: 640,
    to_y: 480,
    duration_ms: 600
  })
  const helperCall = runner.calls.find(call => call.cmd.endsWith('wl-input'))
  assert.deepEqual(helperCall?.args.slice(-7), ['drag', '120', '140', '640', '480', 'left', '600'])
  assert.equal(result.backend, 'wl-input')
})

test('Hyprland refuses off-layout coordinates instead of clamping', async () => {
  const runner = new FakeRunner(baseResponder())
  const adapter = makeAdapter(runner)
  await assert.rejects(
    () => adapter.pointer({ action: 'click', x: 9000, y: 200 }),
    (error: unknown) => error instanceof UiError && error.code === 'invalid_request' && /outside the desktop layout/.test(error.message)
  )
  assert.equal(runner.calls.some(call => call.cmd.endsWith('wl-input')), false)
})

test('Hyprland drag fails closed without the wl-input helper', async () => {
  const runner = new FakeRunner(baseResponder())
  const adapter = makeAdapter(runner, ['hyprctl', 'grim', 'wtype', 'wlrctl'])
  await assert.rejects(
    () => adapter.pointer({ action: 'drag', x: 100, y: 100, to_x: 200, to_y: 200 }),
    (error: unknown) => error instanceof UiError &&
      (error.code === 'capability_unavailable' || error.code === 'missing_dependency') &&
      /wl-input/.test(error.message)
  )
})

test('Hyprland wlrctl fallback preserves wlrctl scroll argument order and warns', async () => {
  const runner = new FakeRunner(baseResponder())
  const adapter = makeAdapter(runner, ['hyprctl', 'grim', 'wtype', 'wlrctl'])
  const result = await adapter.pointer({ action: 'scroll', x: 200, y: 150, delta_x: 1, delta_y: -2 })
  const wlrctlCall = runner.calls.find(call => call.cmd.endsWith('wlrctl'))
  assert.deepEqual(wlrctlCall?.args, ['pointer', 'scroll', '-2', '1'])
  assert.match(result.warnings?.[0] ?? '', /wlrctl fallback/)
})

test('Hyprland typing uses stdin and wtype modifiers in reverse order', async () => {
  const runner = new FakeRunner(baseResponder())
  const adapter = makeAdapter(runner)
  await adapter.keyboard({ text: 'hello from hermes' })
  const textCall = runner.calls.find(call => call.cmd.endsWith('wtype'))
  assert.deepEqual(textCall?.args, ['-s', '30', '-d', '10', '-'])
  assert.equal(textCall?.stdin, 'hello from hermes')

  runner.calls.length = 0
  await adapter.keyboard({ keys: 'ctrl+shift+t' })
  const keyCall = runner.calls.find(call => call.cmd.endsWith('wtype'))
  assert.deepEqual(keyCall?.args, ['-M', 'ctrl', '-M', 'shift', '-k', 't', '-m', 'shift', '-m', 'ctrl'])
})

test('Hyprland capture returns pixel dimensions and the pixel-to-logical contract', async () => {
  const runner = new FakeRunner(baseResponder())
  const adapter = makeAdapter(runner)
  const capture = await adapter.capture({ target: 'display', display: 'DP-1' })
  const grimCall = runner.calls.find(call => call.cmd.endsWith('grim'))
  assert.deepEqual(grimCall?.args, ['-g', '0,0 2048x1152', '-t', 'png', '-'])
  assert.equal(capture.pixel_width, 2048)
  assert.equal(capture.pixel_height, 1152)
  assert.equal(capture.scale, 1.25)
  assert.deepEqual(capture.origin, { x: 0, y: 0 })
})

test('Hyprland capability report names the missing helper', async () => {
  const runner = new FakeRunner(baseResponder())
  const adapter = makeAdapter(runner, ['hyprctl', 'grim', 'wtype', 'wlrctl'])
  const capabilities = await adapter.capabilities()
  const drag = capabilities.find(capability => capability.id === 'pointer.drag')
  assert.equal(drag?.available, false)
  assert.match(drag?.reason ?? '', /wl-input/)
})

test('Hyprland reports no graphical session when WAYLAND_DISPLAY is absent', async () => {
  const runner = new FakeRunner(baseResponder())
  const adapter = new HyprlandAdapter({
    runner,
    platform: 'linux',
    env: { PATH: '/usr/bin', HOME: '/home/tester' },
    home: '/home/tester',
    which: async name => `/usr/bin/${name}`
  })
  const environment = await adapter.environment()
  assert.equal(environment.graphical, false)
  assert.match(environment.reason ?? '', /WAYLAND_DISPLAY/)
  const windows = await adapter.windows().catch((error: unknown) => error)
  assert.equal(environment.dependencies.hyprctl.available, true)
  assert.equal(windows instanceof UiError, false) // windows() still parses fake IPC; environment reports the missing session
})
