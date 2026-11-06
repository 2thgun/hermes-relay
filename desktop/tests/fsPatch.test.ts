import assert from 'node:assert/strict'
import { promises as fs } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'

import { configureCapabilityPolicies } from '../src/tools/capabilityRuntime.js'
import { patchHandler } from '../src/tools/handlers/fs.js'
import type { ToolContext } from '../src/tools/router.js'

async function withFile(
  content: string,
  run: (dir: string, path: string) => Promise<void>
): Promise<void> {
  const dir = await fs.mkdtemp(join(tmpdir(), 'relay-patch-'))
  const path = join(dir, 'test.txt')
  await fs.writeFile(path, content, 'utf8')
  try {
    await run(dir, path)
  } finally {
    await fs.rm(dir, { recursive: true, force: true })
  }
}

function context(dir: string): ToolContext {
  return {
    cwd: dir,
    abortSignal: new AbortController().signal,
    interactive: false
  }
}

test('patch applies a hunk that ends with a newline', async () => {
  configureCapabilityPolicies({
    commands: 'disabled',
    files: 'allow',
    screen_input: 'disabled',
    usb: 'disabled',
    microphone: 'disabled',
    camera: 'disabled'
  })
  await withFile('alpha\nbeta\ngamma\n', async (dir, path) => {
    const patch = [
      '--- a/test.txt',
      '+++ b/test.txt',
      '@@ -1,3 +1,3 @@',
      '-alpha',
      '+ALPHA',
      ' beta',
      ' gamma',
      ''
    ].join('\n')
    const result = (await patchHandler({ path, patch }, context(dir))) as {
      ok: boolean
      hunks_applied: number
    }
    assert.equal(result.ok, true)
    assert.equal(result.hunks_applied, 1)
    assert.equal(await fs.readFile(path, 'utf8'), 'ALPHA\nbeta\ngamma\n')
  })
})

test('patch applies a last-line hunk without inventing a context line', async () => {
  configureCapabilityPolicies({
    commands: 'disabled',
    files: 'allow',
    screen_input: 'disabled',
    usb: 'disabled',
    microphone: 'disabled',
    camera: 'disabled'
  })
  await withFile('one\ntwo\n', async (dir, path) => {
    const patch = ['@@ -2,1 +2,1 @@', '-two', '+TWO'].join('\n')
    await patchHandler({ path, patch }, context(dir))
    assert.equal(await fs.readFile(path, 'utf8'), 'one\nTWO\n')
  })
})
