// Live browser integration test.
//
// Opt-in: HERMES_RELAY_LIVE_BROWSER=1. Requires a Chrome/Chromium binary and a
// Node build with a global WebSocket (>= 21). It launches the dedicated
// automation profile against a throwaway local HTTP server, so it proves the
// real CDP wiring (navigation, refs, IO, upload, download, screenshot) rather
// than a mock protocol.

import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { mkdtemp, readFile, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'

import { BrowserController } from '../src/tools/browser/controller.js'

const live = process.env.HERMES_RELAY_LIVE_BROWSER === '1'

const PAGE = `<!doctype html>
<html><head><title>Relay browser test</title></head>
<body>
  <h1>Relay browser test</h1>
  <label>Name <input id="name" aria-label="Name"></label>
  <button id="go" type="button">Submit</button>
  <input type="file" id="file" aria-label="Upload">
  <a id="dl" href="/download" download="report.txt">Download report</a>
  <div id="out" aria-live="polite"></div>
  <script>
    document.getElementById('go').addEventListener('click', () => {
      document.getElementById('out').innerText = 'submitted:' + document.getElementById('name').value;
    });
    document.getElementById('file').addEventListener('change', (event) => {
      document.getElementById('out').innerText = 'file:' + event.target.files[0].name;
    });
  </script>
</body></html>`

async function withServer(run: (base: string) => Promise<void>): Promise<void> {
  const server = createServer((request, response) => {
    if (request.url === '/download') {
      response.writeHead(200, {
        'Content-Type': 'text/plain',
        'Content-Disposition': 'attachment; filename="report.txt"'
      })
      response.end('relay download payload\n')
      return
    }
    response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' })
    response.end(PAGE)
  })
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  const address = server.address()
  if (!address || typeof address === 'string') throw new Error('server did not bind')
  try {
    await run(`http://127.0.0.1:${address.port}/`)
  } finally {
    await new Promise<void>(resolve => server.close(() => resolve()))
  }
}

test('live browser: navigate, inspect, type, click, upload, download, screenshot', { skip: !live }, async () => {
  const workspace = await mkdtemp(join(tmpdir(), 'relay-browser-test-'))
  process.env.HERMES_RELAY_BROWSER_PROFILE = join(workspace, 'profile')
  process.env.HERMES_RELAY_BROWSER_HEADLESS = '1'
  if (process.env.HERMES_RELAY_BROWSER_ARGS === undefined && process.getuid?.() === 0) {
    // Chrome refuses to start as root without --no-sandbox. CI containers hit this.
    process.env.HERMES_RELAY_BROWSER_ARGS = '--no-sandbox'
  }
  const uploadFile = join(workspace, 'upload.txt')
  await writeFile(uploadFile, 'upload content\n', 'utf8')

  const controller = new BrowserController()
  try {
    await withServer(async base => {
      const opened = await controller.open(base)
      assert.equal(opened.ok, true)
      assert.match(String(opened.url), /^http:\/\/127\.0\.0\.1:/)

      const inspected = await controller.inspect({ include_text: true })
      assert.equal(inspected.title, 'Relay browser test')
      const nameField = inspected.elements.find(element => element.name === 'Name' || element.tag === 'input')
      assert.ok(nameField, 'expected the name input to be discoverable')
      const submit = inspected.elements.find(element => element.name === 'Submit')
      assert.ok(submit, 'expected the submit button to be discoverable')

      await controller.type({ selector: '#name', text: 'hello relay' })
      await controller.click({ selector: '#go' })
      const submitted = await controller.waitFor({ text: 'submitted:hello relay', timeout_ms: 5_000 })
      assert.equal(submitted.ok, true)

      await controller.upload({ selector: '#file', files: [uploadFile] })
      const uploaded = await controller.waitFor({ text: 'file:upload.txt', timeout_ms: 5_000 })
      assert.equal(uploaded.ok, true)

      const screenshot = await controller.screenshot({ format: 'jpeg' })
      assert.ok(Number(screenshot.pixel_width) > 0)
      assert.ok(Number(screenshot.pixel_height) > 0)

      const download = await controller.download({ selector: '#dl', timeout_ms: 20_000 })
      assert.equal(download.ok, true, JSON.stringify(download))
      const downloaded = download.download as { path: string; size: number }
      assert.ok(downloaded.size > 0)
      const text = await readFile(downloaded.path, 'utf8')
      assert.match(text, /relay download payload/)
    })
  } finally {
    await controller.close()
  }
})
