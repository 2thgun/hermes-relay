# Paired computers: Linux desktop control, browser automation, and device targeting

This fork keeps Hermes-Relay's server plugin and lightweight local clients, and
adds a platform-adapter layer so one server-hosted Hermes can operate a paired
**Windows** or **Linux** computer through the same relay, auth, audit, and grant
machinery.

The relay remains the trust boundary: memory, sessions, reasoning, credentials,
and policy live on the server. The computer runs a thin client that executes
bounded tool calls and reports what it can do.

## What changed

| Area | Upstream | This fork |
|---|---|---|
| Device identity | stable `device_id`, name matching, fail-closed multi-client routing | unchanged, plus a persisted directory of online **and offline** devices |
| Selection | per-call `device` / `device_id`; sole client auto-selected | plus a per-conversation pin: an explicit selection sticks, and a pinned offline device fails instead of switching |
| Desktop control | Windows-only CUA/legacy path (`desktop_computer_*`) | common adapter interface with Hyprland, X11, Windows; Windows keeps its shipped path |
| Linux input | none | `wl-input` helper (virtual-pointer protocol) + `wtype`, with `wlrctl` fallback |
| Browser | none | CDP automation in a dedicated profile: inspect refs, click, type, scroll, upload, download, screenshot |
| Background | daemon + reconnect; service installers deferred | systemd user unit (Linux) and logon Scheduled Task (Windows), plus capability reporting in `daemon --status` |
| Skill | status/setup/pair/doctor | `hermes-relay-devices` skill for targeting, verification, and limits |

The older `desktop_computer_*` tools are unchanged. `desktop_ui_*` and
`desktop_browser_*` are the cross-platform surface. On Windows they delegate to
the existing computer-use handlers, so there is no second, untested input stack.

## Architecture

```
Discord / Hermes Desktop
        │  (normal Hermes turn)
        ▼
server-hosted Hermes agent
        │  desktop_* tools (plugin/tools/desktop_tool.py, desktop_ui_tools.py)
        ▼
Relay plugin (aiohttp, loopback HTTP + authenticated WSS)
  │  /desktop/{tool}            -> DesktopHandler._resolve_target()
  │  /desktop/devices           -> online + remembered-offline directory
  │  desktop.command envelope   -> one selected client only
  ▼
local client (Node CLI / daemon)
  ├── filesystem / shell / jobs / process / adb / usb   (unchanged)
  ├── platform adapter
  │     ├── linux-hyprland: hyprctl + grim + wl-input + wtype
  │     ├── linux-x11:      xdotool + xrandr + imagemagick/scrot
  │     └── windows:        powershell/user32 + existing computer-use path
  └── browser controller: Chromium/Chrome via CDP, dedicated profile
```

Everything below the relay's `desktop.command` envelope is local and
capability-gated. A client that lacks `grim` still lists windows; a client that
lacks `wl-input` still clicks via `wlrctl` but reports drag as unavailable.

## Device identity and selection

Rules, in order:

1. **`device_id` is the only exact identity.** It is a random per-install UUID
   stored at `~/.hermes/desktop-device-id` (Windows: `%USERPROFILE%\.hermes\...`).
   Hostname and model are display metadata, not ownership keys.
2. **`device` is convenience only.** It matches name/host/device_id
   case-insensitively after stripping non-alphanumerics. Two matches are an
   ambiguity error, never a guess.
3. **No silent switching.** If a conversation explicitly selects a device, the
   relay pins that `device_id` for the conversation (12 h TTL, 256 entries). A
   later call without a selector uses the pin. If the pinned device is offline,
   the call fails with its identity and last-seen age. It never lands on another
   connected computer.
4. **Sole-client auto-selection is preserved.** With exactly one connected
   client and no pin, a call can omit the selector. This keeps single-machine
   ergonomics; it is not a fallback from a failed selector.
5. **Offline is a first-class answer.** `/desktop/devices` and the
   `desktop_devices` tool list recently seen devices with `online: false`,
   `last_seen_age_s`, and their last capability report. Offline rows are
   informational only.

The relay keeps at most 32 remembered devices and 20 recent commands per
client; nothing is persisted across a relay restart.

## Capability discovery

Each `desktop.status` heartbeat carries:

```json
{
  "adapter": "linux-hyprland",
  "platform": "linux",
  "session": "wayland",
  "compositor": "hyprland",
  "graphical": true,
  "reason": null,
  "capabilities": [
    { "id": "screen.capture", "available": true, "backend": "grim" },
    { "id": "pointer.drag", "available": false, "reason": "drag needs the wl-input helper" }
  ],
  "dependencies": { "hyprctl": {...}, "grim": {...}, "wtype": {...}, "wl-input": {...} },
  "browser": { "available": true, "running": false, "profile_dir": "~/.hermes/browser-profile" },
  "warnings": []
}
```

Capability ids: `screen.capture`, `windows.list`, `windows.focus`,
`pointer.move`, `pointer.click`, `pointer.drag`, `pointer.scroll`,
`keyboard.type`, `keyboard.keys`.

`desktop_ui_status` returns the same report for the selected computer;
`desktop_devices` returns it for every online device plus the last report for
offline ones. `desktop_ui_windows` and `desktop_ui_screenshot` return the
coordinate contract needed to act on what you see.

## Linux: Hyprland prototype (done first)

### Why a native helper

Hyprland exposes state and window management over `hyprctl`, and input through
standard Wayland protocols. The available CLI clients each fall short:

| Option | Verdict |
|---|---|
| `hyprctl dispatch movecursor` | good absolute positioning, no buttons/axis/drag |
| `wlrctl` | relative motion only; **no button down/up**, so drag is impossible; historical trapped-focus and multi-monitor absolute bugs |
| `ydotool` | needs `/dev/uinput`, `ydotoold`, root/udev; types physical US scancodes; pollutes the user's input devices |
| libei / RemoteDesktop portal | `xdg-desktop-portal-hyprland` does not implement the RemoteDesktop portal, so injection silently degrades |
| compositor plugin | version-pinned; unacceptable for a fork that must merge upstream |

`native/wl-input.c` speaks `zwlr_virtual_pointer_unstable_v1` directly:
absolute motion, button press/release, discrete scroll, and an interpolated
drag, all in one short-lived Wayland client. Hyprland vendors this protocol
(`protocols/wlr-virtual-pointer-unstable-v1.xml`) and implements it in
`src/protocols/VirtualPointer.cpp`, which normalises `motion_absolute` to
`x/xExtent` of the compositor layout. Keyboard input stays in `wtype`
(`zwp_virtual_keyboard_v1`), which owns XKB layout and unicode handling.

### Verified against a real compositor

The prototype ran the production `HyprlandAdapter` against sway's headless
wlroots backend (same protocol family as Hyprland), with a test-only `hyprctl`
shim translating sway state into Hyprland's JSON shape. `wev` logged the events
the compositor actually delivered.

```bash
# build and install
make -C desktop/native install            # -> ~/.hermes/bin/wl-input

# run the adapter end to end
XDG_RUNTIME_DIR=/tmp/xdg WAYLAND_DISPLAY=wayland-1 SWAYSOCK=... \
  PATH="$PWD/desktop/native/prototype:$PATH" \
  npx tsx desktop/native/prototype/run-prototype.ts
```

Observed result:

| Check | Result |
|---|---|
| adapter/capabilities | `linux-hyprland`, all nine capabilities available |
| window discovery/focus | `foot` and `wev` with stable addresses; focus verified by `activewindow` |
| typing | `wtype`, screenshot hash changed |
| click | `wl-input`, cursor `900,300`, active window became `wev`, verification true |
| drag | `wl-input`, 17 motion/button events, press + release, final motion `1000,500`, cursor verification true |
| scroll | wheel source, `axis_stop discrete: 3`, `axis value: 45` |
| region capture | `320x200` at origin `100,80`, scale 1 |

The unit suite additionally asserts the exact argv the adapter emits, including
the layout-space `--space` arguments, wlrctl's `scroll <dy> <dx>` order, missing
helper errors, and refusal to clamp off-layout coordinates.

### Known Hyprland limits

- **Hyprland 0.56+ uses Lua dispatchers.** `hyprctl dispatch focuswindow address:0x…`
  now fails with a Lua parse error; the adapter switches to
  `hl.dsp.focus({window="address:0x…"})` and
  `hl.dsp.cursor.move({x=…, y=…})` based on the reported version. Verified on
  Hyprland 0.56.2 (Omarchy, eDP-1 1440×900 at scale 2).
- Absolute virtual-pointer motion has multi-monitor bugs
  ([#6749](https://github.com/hyprwm/Hyprland/issues/6749)); the helper sends
  compositor-layout-normalised coordinates and the adapter verifies `cursorpos`
  afterwards.
- A button sent right after an external cursor warp can land at the previous
  position ([#4167](https://github.com/hyprwm/Hyprland/issues/4167)). The helper
  moves and clicks in one connection; the `wlrctl` fallback is explicitly
  verified and returns a warning.
- Keyboard input follows the focused window. Focusing another window is an
  explicit action; there is no silent background typing. Hyprland's
  `send_shortcut` can target a window for shortcuts, not arbitrary text.
- Special/hidden workspaces are reported as not visible; the adapter refuses to
  act on them rather than guessing.
- Screenshots use `grim`. A full multi-monitor capture with mixed scales warns
  that pixel-to-logical mapping is approximate; capture one display for precise
  clicks.
- Typing per keystroke is intentionally capped (2000 chars/call) and drags are
  bounded (40–5000 ms).

## X11 and Windows

- **X11** (`linux-x11`): `xdotool` for windows/input, `xrandr` for monitors,
  ImageMagick `import` (or `scrot`) for capture. Coordinates are physical pixels.
  This is the compatibility path for Xorg sessions and XWayland fallbacks.
- **Windows** (`windows`): PowerShell + user32 for window enumeration/focus;
  capture and input remain the shipped `desktop_computer_*` path (CUA driver or
  the legacy user32 bridge). Drag is reported unavailable rather than
  reimplemented untested.

### Evaluation of Cua Driver and computer-use-linux

Both were evaluated before writing a native lane:

- **Cua Driver** now ships Linux targets and Linux capture/input is described as
  maturing, with native Wayland input opt-in. It is a large external runtime and
  the existing Relay integration is deliberately Windows-first
  (`cuaDriver.ts` rejects non-`win32`). Adopting it as the Linux default would
  add a second daemon and an MCP-shaped dependency to every client. Kept as a
  future optional backend; the adapter interface leaves room for it.
- **computer-use-linux** is closer: Rust MCP/CLI, compositor-aware windows
  (including `hyprctl`), AT-SPI semantic selectors, and `/dev/uinput` or portal
  input. It solves accessibility-tree targeting well, but its pointer path
  depends on uinput/portal (the exact privilege and Hyprland-portal problems
  above), and adding it means shipping and supervising another server plus its
  AT-SPI setup. This fork uses it as a reference and a possible semantic layer,
  not as the default input backend.

The chosen path keeps the client thin, needs no root, no uinput, no portal, no
extra daemon, and uses protocols Hyprland itself ships.

## Browser automation

`desktop_browser_*` controls a Chromium-family browser over CDP:

- **Dedicated profile** at `~/.hermes/browser-profile` (override with
  `HERMES_RELAY_BROWSER_PROFILE`). It never attaches to the user's signed-in
  profile. Downloads go to `<profile>/downloads`, logs to
  `<profile>/chromium.log`. Detection includes Chrome, Chromium, Helium
  (`helium-browser`), Brave, and Edge; Helium 0.21/Chrome 157 was verified as a
  CDP runtime on Omarchy.
- **Launch**: `--remote-debugging-port=0` plus the profile; the client reads
  `DevToolsActivePort`. `HERMES_RELAY_BROWSER` selects a binary,
  `HERMES_RELAY_BROWSER_ARGS` adds local flags (e.g. `--no-sandbox` in a
  container), and profile/port/headless flags cannot be overridden that way.
- **Structured inspection** assigns `data-hermes-ref` handles to visible
  interactive elements and returns role/name/value/checked/disabled/bounds. Refs
  expire on navigation or a new inspection; clicks by ref resolve the element's
  current centre and dispatch real mouse events.
- **Interaction**: click (left/right/middle, 1–3), type (insert or native value
  setter, clear, submit), key combos, wheel scroll, upload via
  `DOM.setFileInputFiles`, download with settle detection, viewport/full-page
  screenshots.
- **Verification**: waits for selectors/text; screenshots return pixel
  dimensions; the tool returns the page URL/title after actions.

Limits: no iframe/shadow-DOM traversal, no browser extension APIs, no attaching
to existing profiles, and uploads are limited to 10 files per call. Actions
require the same assist/control grant as desktop input.

## Security model

Unchanged boundaries, extended to new tools:

- Pairing, session tokens, per-channel grants, TLS/pinning, rate limits, and
  audit logging are Relay's existing machinery.
- `desktop_ui_screenshot` and `desktop_ui_windows` require an
  observe/assist/control grant; `desktop_ui_action` and browser interaction
  require assist/control. The local client asks for approval through the
  existing visible prompt or the grant-bridge file for headless daemons.
- Tool calls carry the relay-minted control-session identity; the client
  validates the bound request/target/run ids and fails closed.
- Audit rows redact window titles and browser text by default; screenshots are
  only retained when the local retention setting is enabled.
- No new inbound listener. The browser's DevTools port binds loopback with a
  random port and is read from the profile directory.

Out of scope for this milestone: unattended control of a logged-out desktop,
elevated/administrator actions, mobile support, attaching to existing browser
profiles, and a redesigned chat UI.

## Background clients

Desktop automation needs a logged-in graphical session, so the client is a
**login** service, not a system daemon.

Linux (systemd user unit, inside `graphical-session.target`):

```bash
desktop/scripts/install-service-linux.sh
# unit: ~/.config/systemd/user/hermes-relay-daemon.service
# wrapper recovers WAYLAND_DISPLAY and HYPRLAND_INSTANCE_SIGNATURE
systemctl --user status hermes-relay-daemon
hermes-relay daemon --status
```

Windows (logon Scheduled Task, interactive, restart on failure):

```powershell
desktop/scripts/install-service-windows.ps1
# task: HermesRelayDesktop; log: %USERPROFILE%\.hermes\daemon-service.log
hermes-relay daemon --status
```

Both rely on the transport's existing auto-reconnect (exponential backoff to
30 s). The daemon deliberately does **not** send `tui.attach`: it never uses a
server-side TUI gateway, and attaching one used to drop the whole desktop
connection when that gateway could not start. `daemon --status` now prints the
platform adapter, session kind, whether a graphical session is reachable, and
browser availability, so "why can't the agent click?" is answerable without
journald.

## Testing

```bash
# desktop client unit + live browser (opt-in)
cd desktop
npx tsx --test tests/*.test.ts
HERMES_RELAY_LIVE_BROWSER=1 PATH=/path/to/node22:$PATH npx tsx --test tests/browserLive.test.ts

# Python device directory, offline errors, pinning, schemas
.venv/bin/python -m pytest plugin/tests/test_desktop_devices.py \
  plugin/tests/test_desktop_health.py plugin/tests/test_desktop_multidevice.py \
  plugin/tests/test_desktop_strict_id.py plugin/tests/test_desktop_tool_availability.py -q

# live Wayland prototype (needs a running wlroots compositor + wev)
npx tsx desktop/native/prototype/run-prototype.ts
```

Covered failure paths: no graphical session, missing helper, off-layout
coordinates, denied grant, offline device by id, offline device by name,
ambiguous name, pinned device disconnect, expired pin, CDP socket close,
oversized screenshot, unknown tool advertisement.

## Upstream merge strategy

- New files carry the work: `desktop/src/tools/platform/*`,
  `desktop/src/tools/browser/*`, `desktop/src/tools/handlers/{desktopUi,browser}.ts`,
  `desktop/native/*`, `plugin/tools/desktop_ui_tools.py`,
  `plugin/tests/test_desktop_devices.py`, `skills/devops/hermes-relay-devices/`.
- Existing files change only at registration seams: `handlerSet.ts`,
  `router.ts`, `cli.ts`, `daemonStatus.ts`, `desktop.py` (directory/pinning),
  `server.py` (one route), `desktop_tool.py` (schema merge + capability map).
- No upstream wire fields were renamed or repurposed. `desktop.status` gained
  optional `capabilities`; `/desktop/devices` is additive.
- The Windows computer-use path is untouched, so fixes there merge cleanly.
