# Phase 6 — Role-aware plugin integration (self-sufficient install)

Build on `55b4d283` in this worktree/branch `feat/python-client`.

## Goal

A plugin install on a controlled computer should start the Python device client
(`relay.device`) and **not** start a relay host. A server install keeps starting
the relay host. One package, one install, role chosen by config.

## Work items

1. **Role wiring in `plugin/__init__.py` (`register(ctx)`).**
   - Resolve role with `plugin.relay.device.settings` (env / `.env` /
     `plugins.entries.hermes-relay.settings` / default `host`).
   - `host`: current embedded relay sidecar only.
   - `client`: no relay listener; start the Python device client as a detached
     subprocess when `relay_url` and a stored token/state exist. If no token,
     log a clear “pair first” message and do not spawn.
   - `both`: relay host + device client.
   - Never start both twice; health/PID check before spawn.
2. **Installed-layout module path.** In the repo the client module is
   `plugin.relay.device`; in the installed plugin folder
   (`~/.hermes/plugins/hermes-relay/`) the package root is `relay/`, so the
   child must run `python -m relay.device` with the plugin dir as cwd. Detect
   the layout from `Path(__file__)`; do not hardcode one prefix. Keep
   `python -m plugin.relay.device` working in the repo.
3. **Supervision.** Spawn with `sys.executable`, detached session, log under
   `$HERMES_HOME/logs/hermes-relay-device.log`, record pid, terminate on clean
   plugin exit. Don’t fight an already-running client (check state/health).
4. **Config/docs.** Document `role`, `relay_url`, `client_state` in
   `docs/python-client.md` and the plugin README; fix stale `plugin.relay.client`
   references introduced by the rename. Add a `config_schema` entry in
   `plugin/plugin.yaml` if the manifest supports it.
5. **Pairing path.** Document the one-time `hermes-relay-client pair` (or
   `python -m relay.device pair`) before role=client starts. Do not auto-pair.
6. **Tests.** Fake role/subprocess tests: host starts only relay, client starts
   only device (and refuses without token), both starts both, repo vs installed
   module path, idempotent re-register, no media import regression.
7. **CLI surface.** Ensure `hermes relay client ...` or the console script can
   pair/run/status on an installed plugin (whichever the existing CLI scaffolding
   makes cleanest); document the chosen path.

## Out of scope

Removing the TypeScript client, CUA/computer-use, changing the live installed
plugin, service installation on real machines.

## Constraints

Same as phases 1–5: isolated worktree only, commit to `feat/python-client`, no
live tree/service/device changes, real pytest output, report gaps.
