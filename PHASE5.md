# Phase 5 — Package the Python client as the unified artifact

Build on `9f565632` in this worktree/branch `feat/python-client`.

## Goal

Make the Python client installable and runnable as part of the same plugin,
without the TypeScript/Bun client, and without requiring the full Hermes plugin
dependency set just to start the client.

## Work items

1. **Lazy plugin imports.** `plugin/__init__.py` currently imports
   `android_tool`/`desktop_tool`/`relay_plugin_tool` at module import, so
   `python -m plugin.relay.client` fails on a clean venv with `requests` (and
   similar) missing. Move heavy imports inside `register(ctx)` or behind a
   lazy `__getattr__`; the client must run with only its own deps
   (`aiohttp`, `websocket-client`).
2. **Entry point.** Add `[project.scripts] hermes-relay-client =
   "plugin.relay.client.__main__:main"` to `pyproject.toml` (and package data
   for any resources). `hermes-relay-client pair/run/tools/status` must work
   after `pip install -e .` and from the plugin venv.
3. **State store.** Fix the `/tmp` failure: never `chmod` an existing parent
   directory. Default state path should be `$HERMES_HOME/relay/client.json`,
   with `--state` override; create only the leaf/parents as needed.
4. **Role config.** Add a small resolver for `host`, `client`, `both`
   (`HERMES_RELAY_ROLE`, default `host`). Client role reads `relay_url` and
   `client_state` from plugin config/env; document the keys. Do not auto-pair
   without a code.
5. **Service installers.** Add `scripts/install-client-linux.sh` (systemd
   *user* unit, `graphical-session.target`, same flags as the tested command)
   and `scripts/install-client-windows.ps1` (per-user logon Scheduled Task,
   hidden, restart on failure). They install the Python entry point, not the
   Bun binary. Dry-run/`--print` modes for tests; no live service changes.
6. **Docs.** `docs/python-client.md`: install, pair, run, state/token location,
   role config, uninstall, current gaps (CUA, drag, window capture).
7. **Tests.** Import the client in a subprocess with a minimal environment;
   entry point resolution; state dir creation under a normal home and a
   read-only `/tmp`-like parent; role resolver; installer generators produce
   expected unit/task content without executing it.

## Out of scope

Cutting the TypeScript client out of the plugin, changing the running server,
live service installation, CUA/computer-use, iframe/shadow DOM.

## Constraints

Same as phases 1–4: isolated worktree only, commit to `feat/python-client`, no
live tree/service/device changes, real pytest output, report gaps.
