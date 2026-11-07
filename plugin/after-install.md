# Hermes-Relay Plugin Installed

Enable the plugin in Hermes, restart the Hermes process, then verify the install:

```bash
hermes relay doctor
```

Use `hermes pair` to create a QR pairing payload. Use `hermes relay start` to run
the relay foreground server, or use the legacy installer if you still need the
systemd user service and shell shims.

## Role: host, client, or both

`plugin.yaml` declares a `role` setting (also `HERMES_RELAY_ROLE`). It defaults
to `host`, which keeps the embedded relay server running exactly as before.

On a **controlled computer** set the role to `client` after installing the
plugin, pair once, and restart Hermes:

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
hermes relay client pair --url wss://relay.example:8767 --code ABC123
```

With `role: client` the plugin does **not** open the relay port; it starts the
Python device client (`python -m relay.device run` from the plugin folder)
as a detached child once a stored session token exists. Without a token it
logs a "pair first" warning and starts nothing. `role: both` runs the relay
server and the device client on one machine. Logs go to
`$HERMES_HOME/logs/hermes-relay-device.log` and the pid is recorded in
`$HERMES_HOME/relay/hermes-relay-device.pid`. See
[docs/python-client.md](../docs/python-client.md#automatic-start-from-the-plugin)
for the full supervision contract.

## Two install paths

**Tools-only (this path — `hermes plugins install`):** discovers + enables the
`android_*` / `desktop_*` tools, prompts for any optional voice-provider keys,
and registers the plugin with the gateway. The plugin's `plugin.yaml` lives in
the repo's `plugin/` subdir, so install with the subdir identifier:

```bash
hermes plugins install Codename-11/hermes-relay/plugin
```

This is the right path if you only want the agent-side tools and will run the
WSS relay yourself (or don't need it). It does **not** set up the relay server,
a systemd unit, the `pip install -e`, the shell shims, or the legacy
compatibility bootstrap.

**Full relay (`install.sh` curl one-liner):** everything above **plus** the
relay server, systemd user unit, editable pip install, `hermes-pair` /
`hermes-relay` / `hermes-relay-tailscale` shims, the skills dir, and the
optional compatibility bootstrap. Use this for terminal, bridge/phone control,
relay voice, desktop tooling, and remote access:

```bash
curl -fsSL https://raw.githubusercontent.com/Codename-11/hermes-relay/main/install.sh | bash
```

**Official Docker image:** on `nousresearch/hermes-agent` the native
`hermes plugins install` path above is the only supported one. The image is
immutable (`/opt/hermes/.venv`, no git clone, no user systemd), so
`install.sh`'s editable-install/systemd path is not applicable there — the
installer detects that layout and steers back to the native path. Start the
relay inside the container with `hermes relay start`.

The optional legacy compatibility hook is managed separately:

```bash
hermes relay compat status
hermes relay compat install   # only for older Hermes builds or route gaps
hermes relay compat remove
```

Standard Android chat, Manage, and dashboard voice use vanilla upstream Hermes.
The Relay plugin is additive: phone control, terminal, remote desktop tooling,
dashboard Relay management, and optional compatibility diagnostics.

Legacy installs may also have a `hermes_relay_bootstrap.pth` monkeypatch in the
Hermes Python environment. `hermes relay doctor --json` and
`hermes relay compat status --json` report whether that bootstrap is present.
Keep it only for older Hermes builds or compatibility-only route gaps.
