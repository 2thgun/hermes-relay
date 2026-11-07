# Headless Python relay client

`plugin.relay.device` is the controlled-computer side of Hermes-Relay. It pairs
with a relay over WebSocket, stores the minted session token, and serves the
`desktop_*` tools (files, terminal, processes, clipboard, Linux/Hyprland and
Windows UI automation, and browser/CDP). It replaces the Bun/TypeScript client
for headless and service installs and ships in the same plugin package.

The same plugin install can run the relay **host**, the **client**, or **both**;
`role: client` makes the plugin start this device client automatically and skip
the relay listener entirely (see
[Automatic start from the plugin](#automatic-start-from-the-plugin)).

Requires Python 3.11+ and, at runtime, only:

- `aiohttp` (relay transport and CDP)
- `websocket-client` (reserved client dependency; matching the plugin manifest)

The client does not import `requests`, `yaml`, `psutil`, `httpx`, or `segno`.
Those are server/plugin dependencies; `plugin/__init__.py` and
`plugin/relay/__init__.py` load them lazily so `python -m plugin.relay.device`
starts on a client-only machine.

## Install

Full plugin install (server + client, all plugin dependencies):

```bash
pip install -e .
```

Client-only install on a controlled computer (no server stack installed):

```bash
pip install --no-deps -e .
pip install 'aiohttp>=3.14.1,<4' 'websocket-client>=1.8.0,<2'
```

Either form installs the `hermes-relay-client` console script:

```bash
hermes-relay-client --help
python -m plugin.relay.device --help   # equivalent, no script on PATH needed
```

A plugin-folder install (`hermes plugins install Codename-11/hermes-relay/plugin`)
drops the same tree into `~/.hermes/plugins/hermes-relay/`; there the package
root is that folder, so the equivalent module invocation is
`python -m relay.device` run from the plugin directory (or
`hermes relay client ...`). No `pip install` is required for that layout.

`pip install 'hermes-relay[client]'` is also available as an explicit
dependency declaration; it does not remove the base server dependencies.

## Pair

Pairing exchanges a one-time code (shown by the relay host) for a durable
session token. It never pairs automatically: a code is always required.

```bash
hermes-relay-client pair --url wss://relay.example:8767 --code ABC123 --name workstation
```

`--url` may be omitted when `HERMES_RELAY_URL` or the `relay_url` plugin setting
is configured (see [Role and connection settings](#role-and-connection-settings)).

The token is written to `$HERMES_HOME/relay/client.json` (default
`~/.hermes/relay/client.json`) with mode `0600`; newly created parent
directories are `0700`. Existing parent directories are never `chmod`-ed, so
`--state /tmp/client.json` works even when `/tmp` is owned by another user.

## Run

```bash
hermes-relay-client run
hermes-relay-client run --url wss://relay.example:8767
```

Without `--url`, the client uses `relay_url` from settings, then the active
stored pairing. Without a stored token or `--code`, `run` exits with a usage
error instead of pairing silently.

Useful flags:

| Flag | Meaning |
| --- | --- |
| `--state PATH` | client store override |
| `--cwd PATH` | working directory for tool handlers |
| `--allow-tools a,b` / `--deny-tools c` | tool allow/deny lists |
| `--heartbeat SECONDS` | `desktop.status` interval |
| `--insecure` | skip TLS verification (test relays only) |
| `--once` | exit after the first successful tool call (smoke tests) |
| `--json` | one JSON event per line on stdout |

Inspect state without a network connection:

```bash
hermes-relay-client status          # role, resolved URL, device id, store path, pairings
hermes-relay-client status --json
hermes-relay-client tools --json    # tool names this platform advertises
```

## CLI surface on an installed plugin

Three equivalent entry points reach the same commands:

| Context | Invocation |
| --- | --- |
| pip install / repo checkout | `hermes-relay-client pair\|run\|status\|tools\|install ...` |
| repo checkout, no script on PATH | `python -m plugin.relay.device ...` |
| installed plugin folder | `hermes relay client pair\|run\|status\|tools\|install ...` |

`hermes relay client` forwards its arguments unchanged to the device CLI. A
plugin-folder install (`hermes plugins install`) has no console script on
`PATH`, so this sub-command is the supported surface there. The supervisor
uses the same layout detection when it spawns the child.

## Role and connection settings

One install can be the relay **host** (serves pair codes and relays traffic),
a **client** (controlled computer), or **both** on the same machine.

| Setting | Environment | `.env` | `config.yaml` (`plugins.entries.hermes-relay.settings`) | Default |
| --- | --- | --- | --- | --- |
| Role | `HERMES_RELAY_ROLE` | same | `role` (alias `relay_role`) | `host` |
| Relay URL | `HERMES_RELAY_URL` | same | `relay_url` | unset |
| Client state | `HERMES_RELAY_CLIENT_STATE` | same | `client_state` | `$HERMES_HOME/relay/client.json` |

Resolution order is: explicit CLI flag → process environment → `$HERMES_HOME/.env`
→ `config.yaml` plugin settings → built-in default. Valid roles are `host`,
`client`, and `both`; anything else is rejected. Example:

```yaml
# ~/.hermes/config.yaml
plugins:
  entries:
    hermes-relay:
      settings:
        role: client
        relay_url: wss://relay.example:8767
        client_state: ~/.hermes/relay/client.json
```

```bash
HERMES_RELAY_ROLE=client hermes-relay-client run
```

When PyYAML is not installed (client-only venv), the `config.yaml` layer is
skipped; environment and `.env` settings still apply.

## Automatic start from the plugin

`plugin.register()` starts the services the configured role needs. It never
starts the same service twice, and a `client` install never opens the relay
port.

| Role | Relay host (embedded sidecar) | Device client |
| --- | --- | --- |
| `host` (default) | yes | no |
| `client` | no | yes, when paired |
| `both` | yes | yes, when paired |

For `client`/`both` the plugin launches a detached child with
`sys.executable`:

- **repo checkout** — `python -m plugin.relay.device run`, cwd = repo root
  (the `plugin` package import root);
- **installed plugin** (`~/.hermes/plugins/hermes-relay`) —
  `python -m relay.device run`, cwd = the plugin folder (its `relay` package
  is the import root).

The layout is detected from the package location (`Path(__file__)`), so a
client install does not need `plugin` to be importable. Tool handlers run with
`--cwd` set to the user's home directory because the child's process cwd is
the import root.

The child starts only if **both** conditions hold:

1. a relay URL is configured (`relay_url`) or a stored pairing supplies an
   active URL, and
2. the client store (`client_state`) holds a non-expired session token for
   that URL.

Otherwise the plugin logs one actionable warning and starts nothing; the
message names the missing piece and the pairing command. It never pairs
automatically:

```bash
# installed plugin (no console script on PATH)
hermes relay client pair --url wss://relay.example:8767 --code ABC123 --name workstation

# pip install / repo checkout
hermes-relay-client pair --url wss://relay.example:8767 --code ABC123
python -m plugin.relay.device pair --url wss://relay.example:8767 --code ABC123
```

After pairing, restart Hermes (or re-enable the plugin) so `register()` starts
the client. Supervision details:

- log: `$HERMES_HOME/logs/hermes-relay-device.log`
- pid file: `$HERMES_HOME/relay/hermes-relay-device.pid`
- a live child or pid file blocks a duplicate spawn, so plugin reloads are safe
- a child spawned by the plugin is terminated on clean plugin exit; a client
  started by systemd or another process is left alone
- an invalid `role` is logged and treated as `host`, preserving pre-6 behaviour

Inspect what the plugin would use without starting anything:

```bash
hermes relay client status --json
```

## Install as a service

Both installers run the **Python entry point**, not the Bun binary. They
support `--print` (emit the unit/task script) and `--dry-run` (show files and
commands) without touching the machine. The generated command uses the
detected module (`plugin.relay.device` in a checkout, `relay.device` in an
installed plugin folder), sets the unit/task working directory to that import
root, and passes `--cwd` so tool handlers still run from your home directory.

### Linux — systemd user unit

```bash
scripts/install-client-linux.sh --url wss://relay.example:8767
scripts/install-client-linux.sh --print --url wss://relay.example:8767
```

This writes `$XDG_CONFIG_HOME/systemd/user/hermes-relay-client.service`
(default `~/.config/systemd/user/`), then runs:

```bash
systemctl --user daemon-reload
systemctl --user enable --now hermes-relay-client.service
```

The unit uses `After=`/`PartOf=`/`WantedBy=graphical-session.target`, so it
starts with the desktop session and stops with it. No root access is required.
For a headless machine without a graphical login, run
`loginctl enable-linger "$USER"` so the user manager starts at boot; the
`graphical-session.target` binding still means the unit only activates when
that target exists.

Equivalent direct command:

```bash
hermes-relay-client install --platform linux --dry-run
hermes-relay-client install --platform linux --unit-dir /tmp/units --print
```

### Windows — per-user logon Scheduled Task

```powershell
.\scripts\install-client-windows.ps1 -Url wss://relay.example:8767
.\scripts\install-client-windows.ps1 -Print -Url wss://relay.example:8767
```

The generated PowerShell registers task **Hermes Relay Client** with:

- an `AtLogOn` trigger for the current user,
- `-Hidden` and `Interactive`/`Limited` principal (no admin rights),
- no execution time limit, restart every minute on failure (`RestartCount 999`),
- `pythonw.exe -m plugin.relay.device run ...` so no console window appears.

Remove it with:

```powershell
Unregister-ScheduledTask -TaskName 'Hermes Relay Client' -Confirm:$false
```

Linux uninstall:

```bash
systemctl --user disable --now hermes-relay-client.service
rm ~/.config/systemd/user/hermes-relay-client.service
systemctl --user daemon-reload
```

Unpairing is just deleting the store (`$HERMES_HOME/relay/client.json`, or the
`--state` path); the host keeps its own session database and is unaffected.

## Current gaps

- **Computer-use / CUA**: no `desktop_computer_*` tools yet. The client serves
  the `desktop_*` file/terminal/process/clipboard/UI/browser families only.
- **Drag**: Linux Hyprland supports pointer drag only when the `wl-input`
  helper is installed; the Windows adapter reports `pointer.drag` as
  unavailable (user32 input bridge has no drag).
- **Window capture**: Windows captures the full display only; window and region
  capture are unimplemented there. Hyprland and X11 support display, window,
  region, and all-display capture.
- Browser automation targets Chromium-family browsers with CDP; iframe/shadow
  DOM traversal is out of scope for this phase.

## Files

| Path | Purpose |
| --- | --- |
| `plugin/relay/device/` | client package (transport, dispatcher, handlers, platform adapters) |
| `plugin/relay/device/settings.py` | role/URL/state resolver |
| `plugin/relay/device/supervisor.py` | plugin-side role launcher (layout detection, pid/log, token gate) |
| `plugin/relay/device/service.py` | unit and Scheduled Task generators |
| `plugin/relay/device/installer.py` | install plan/apply (dry-run safe) |
| `scripts/install-client-linux.sh` | systemd user unit wrapper |
| `scripts/install-client-windows.ps1` | Scheduled Task wrapper |
