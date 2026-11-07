# Phase 3 — Browser/CDP for the Python client

Build on `7bf77ed9` in this worktree/branch `feat/python-client`.

## Goal

Port the structured browser surface to the Python client, matching
`desktop/src/tools/browser/{cdp,launcher,controller}.ts` and the server schemas
in `plugin/tools/desktop_ui_tools.py`.

Add `plugin/relay/client/browser/`:

- `launcher.py` — detect Chrome/Chromium/Helium/Brave/Edge, launch a dedicated
  profile (`$HERMES_HOME/browser-profile`), `--remote-debugging-port=0`, read
  `DevToolsActivePort`, return endpoint/pid/log path. Never touch the user's
  signed-in profile.
- `cdp.py` — minimal CDP client over WebSocket (aiohttp or websocket-client),
  request ids, timeouts, event dispatch, target discovery.
- `controller.py` — page state, inspect (refs), click, type, key, scroll,
  screenshot (png/jpeg), upload, download, wait for selector/text, close.
- `handlers.py` — register `desktop_browser_status`, `_open`, `_inspect`,
  `_click`, `_type`, `_key`, `_scroll`, `_screenshot`, `_upload`, `_download`,
  `_wait`, `_close`.

Match the TypeScript result shapes exactly: `ref`, `rect`, `title`, `url`,
`elements`, `download` `{path,size,modified_ms}`, `download_dir`, screenshot
`bytes_base64`/`format`/`size_bytes`, and structured `ok:false, code, message`
errors. Enforce http(s) URLs (and `about:blank`) and the dedicated-profile rule.

## Tests

- Unit tests with a fake CDP server for inspect/click/type/wait/upload/download
  and ref expiry after navigation.
- Launcher tests with a fake process/DevToolsActivePort, missing browser, and a
  profile override.
- Optional live Chromium smoke behind an env flag (headless + `--no-sandbox`
  when running as root); do not require a GUI.
- No network access to real third-party sites in tests; local `file://` is not
  allowed, so use a tiny local HTTP fixture if needed.

## Out of scope

Windows desktop adapter, computer-use/CUA, iframes/shadow DOM, cookies/storage,
multi-tab orchestration (single page is the current upstream scope), service
installers.

## Constraints

Same as phases 1–2: isolated worktree only, commit to `feat/python-client`, no
live tree/service/paired-device changes, real pytest output, report gaps.
