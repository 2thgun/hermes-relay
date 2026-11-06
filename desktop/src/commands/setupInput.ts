// setup-input — build/install the optional Wayland pointer helper.
//
// Hyprland (and other wlroots compositors) expose
// zwlr_virtual_pointer_unstable_v1, but the packaged CLI clients cannot
// express drag or reliable absolute positioning. The small C helper in
// `native/` fills that gap without ydotool, uinput, root, or a portal.
//
// Building is explicit: this command never installs packages. It reports the
// exact distro command when a toolchain or Wayland headers are missing.

import { execFileSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import type { ParsedArgs } from '../cli.js'
import { theme as makeTheme } from '../lib/theme.js'
import { printUsage, unknownSubcommand, type UsageSpec } from '../lib/usage.js'

const SETUP_INPUT_USAGE: UsageSpec = {
  name: 'setup-input',
  summary: 'build/install the optional Wayland pointer helper (wl-input)',
  usage: ['setup-input [--check] [--prefix <dir>]'],
  flags: [
    { flag: '--check', desc: 'report prerequisites without building' },
    { flag: '--prefix <dir>', desc: 'install prefix (default ~/.hermes)' }
  ],
  examples: ['hermes-relay setup-input', 'hermes-relay setup-input --check']
}

function nativeDir(): string {
  const here = dirname(fileURLToPath(import.meta.url))
  // src/commands (tsx) and dist/commands (compiled) are both two levels below
  // the package root, where native/ ships.
  return resolve(here, '..', '..', 'native')
}

function commandExists(command: string): boolean {
  try {
    execFileSync('sh', ['-c', `command -v ${JSON.stringify(command)} >/dev/null 2>&1`], { stdio: 'ignore' })
    return true
  } catch {
    return false
  }
}

export async function setupInputCommand(args: ParsedArgs): Promise<number> {
  const theme = makeTheme()
  if (args.positional.length > 0) {
    return unknownSubcommand(SETUP_INPUT_USAGE, args.positional[0], theme)
  }
  if (args.flags.help === true) {
    printUsage(SETUP_INPUT_USAGE, theme)
    return 0
  }
  if (process.platform !== 'linux') {
    console.error(`${theme.err('setup-input')} is Linux/Wayland only; this host is ${process.platform}.`)
    return 2
  }

  const dir = nativeDir()
  const checkOnly = args.flags.check === true
  const prefix = typeof args.flags.prefix === 'string' ? args.flags.prefix : join(homedir(), '.hermes')

  const prerequisites = [
    { name: 'cc', ok: commandExists('cc') || commandExists('gcc'), hint: 'build-essential (Debian/Ubuntu) or base-devel (Arch)' },
    { name: 'make', ok: commandExists('make'), hint: 'build-essential (Debian/Ubuntu) or base-devel (Arch)' },
    { name: 'wayland-scanner', ok: commandExists('wayland-scanner'), hint: 'libwayland-bin (Debian/Ubuntu) or wayland (Arch)' },
    { name: 'libwayland-client', ok: commandExists('pkg-config') && pkgConfigHas('wayland-client'), hint: 'libwayland-dev (Debian/Ubuntu) or wayland (Arch)' },
    { name: 'grim', ok: commandExists('grim'), hint: 'grim — screenshots and visual verification' },
    { name: 'wtype', ok: commandExists('wtype'), hint: 'wtype — keyboard input' }
  ]
  const missing = prerequisites.filter(item => !item.ok)

  if (!existsSync(join(dir, 'wl-input.c'))) {
    console.error(
      `${theme.err('missing source')}: expected ${join(dir, 'wl-input.c')}. ` +
        'Install the full package (the native/ directory ships with it).'
    )
    return 2
  }

  if (checkOnly) {
    for (const item of prerequisites) {
      console.log(`${item.ok ? theme.ok('ok') : theme.warn('missing')}  ${item.name}`)
    }
    if (missing.length > 0) {
      console.log('\nInstall hints:')
      for (const item of missing) console.log(`  ${item.name}: ${item.hint}`)
      return 1
    }
    console.log(`\nReady. ${theme.dim('Run `hermes-relay setup-input` to build.')}`)
    return 0
  }

  const toolchainMissing = missing.filter(item => ['cc', 'make', 'wayland-scanner', 'libwayland-client'].includes(item.name))
  if (toolchainMissing.length > 0) {
    console.error(`${theme.err('cannot build')} — missing: ${toolchainMissing.map(item => item.name).join(', ')}`)
    for (const item of toolchainMissing) console.error(`  ${item.name}: ${item.hint}`)
    console.error('\nAfter installing those, re-run: hermes-relay setup-input')
    return 1
  }

  try {
    const output = execFileSync('make', ['-C', dir, 'install', `PREFIX=${prefix}`], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe']
    })
    if (output.trim()) console.log(output.trim())
  } catch (error) {
    const detail = error instanceof Error && 'stderr' in error
      ? String((error as { stderr?: unknown }).stderr ?? '')
      : error instanceof Error ? error.message : String(error)
    console.error(`${theme.err('build failed')}\n${detail}`)
    return 1
  }

  const binary = join(prefix, 'bin', 'wl-input')
  if (!existsSync(binary)) {
    console.error(`${theme.err('install failed')}: ${binary} was not created.`)
    return 1
  }
  console.log(`${theme.ok('installed')} ${binary}`)
  if (missing.length > 0) {
    console.log('\nStill missing (optional, capability-gated):')
    for (const item of missing) console.log(`  ${item.name}: ${item.hint}`)
  }
  console.log('\nRestart the desktop client so it re-probes capabilities: hermes-relay daemon --restart')
  return 0
}

function pkgConfigHas(packageName: string): boolean {
  try {
    execFileSync('pkg-config', ['--exists', packageName], { stdio: 'ignore' })
    return true
  } catch {
    return false
  }
}
