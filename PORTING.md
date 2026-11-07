# Unified Python client port — phase 1

Goal: make `hermes-relay` one Python plugin package with three roles selected by
config: `host` (current relay server), `client` (the controlled computer), and
`both` (local use). This worktree is only for the **client** half, phase 1.
Do not change the working server/plugin in `/root/.hermes/projects/hermes-relay-devices`.

## Read these first

- `plugin/relay/channels/desktop.py` — server-side desktop channel: auth,
  `desktop.tool` request/response frames, cancellation, capability gating.
- `desktop/src/transport/RelayTransport.ts` — client auth handshake, reconnect,
  heartbeat, request dispatch.
- `desktop/src/tools/router.ts` — tool registration, timeouts, tool list.
- `desktop/src/tools/handlers/fs.ts`, `shell.ts`, `jobs.ts`, `processes.ts`,
  `clipboard.ts` — wire shapes to match.
- `plugin/tools/desktop_tool.py` — server-side tool schemas and args.

## Phase 1 deliverable

A working headless Python client that can pair and serve the non-GUI tools:

- package: `plugin/relay/client/`
- modules: `__main__.py`, `transport.py`, `identity.py`, `dispatch.py`,
  `handlers/{files,shell,jobs,processes,clipboard}.py`
- CLI:
  - `python -m plugin.relay.client pair --url wss://... --code ABC123 --name hostname`
  - `python -m plugin.relay.client run --url wss://... [--allow-tools ...]`
- auth/token storage: `$HERMES_HOME/relay/client.json` (never the server's
  `hermes-relay-sessions.json`)
- reconnect with exponential backoff, heartbeat, request cancellation
- tools: `desktop_read_file`, `desktop_write_file`, `desktop_patch`,
  `desktop_search_files`, `desktop_terminal`, `desktop_powershell` (Windows
  only), `desktop_job_start/status/logs/cancel/list`, `desktop_list_processes`,
  `desktop_kill_process`, `desktop_find_pid_by_port`, `desktop_clipboard_read`,
  `desktop_clipboard_write`, `desktop_checksum`, `desktop_health`
- advertise exactly the tool names implemented; unknown tool call returns a
  structured `unsupported_tool` error.

Match the server's existing JSON frames exactly; do not invent a new protocol.
If a frame shape is ambiguous, read both sides and add a test.

## Acceptance

- `pytest plugin/tests/test_python_client_*.py` passes.
- A fake server drives `pair` + `run`, sends `desktop_read_file` and
  `desktop_terminal`, and gets correct results.
- A live smoke against the local relay is optional, but if used, register a
  throwaway pairing code via `POST http://127.0.0.1:18767/pairing/register` and
  use `HERMES_HOME=/tmp/...`; never disturb connected devices.
- No changes to running services, systemd units, or the installed plugin.
- Commit only to branch `feat/python-client`.

## Out of scope phase 1

Desktop UI adapters (Hyprland/X11/Windows), browser/CDP, computer-use, GUI
screenshot/input, packaging wheels, and service installers. Those are phases
2–4 and need their own review.

## Style

Python 3.11+, async, aiohttp/websocket-client already in the plugin deps. Keep
modules small and testable. MIT headers consistent with the repo. Do not
rewrite the relay server; reuse its constants and wire names.
