---
name: hermes-relay-devices
description: Operate paired Windows/Linux computers through Hermes-Relay. Use when the user asks to run something on another machine, control a Linux desktop, drive a browser, or when a desktop_* call fails with an offline/ambiguous/grant error.
version: 1.0.0
author: Axiom Labs
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [desktop, relay, hyprland, wayland, x11, windows, browser, computer-use, device-targeting]
    category: devops
    homepage: https://github.com/Codename-11/hermes-relay
    related_skills: [hermes-relay-status, hermes-relay-desktop-setup, hermes-relay-doctor]
---

# Operating paired computers

Hermes runs on the server. The paired computers run a thin `hermes-relay` client
that exposes files, shell, jobs, and (after a local grant) screen/input/browser
tools. Memory, sessions, reasoning, and policy stay on the server.

## 1. Discover before acting

Always start multi-device work with `desktop_devices`:

- `device_id` — stable UUID, the only exact identity. Use it.
- `name` — readable hostname. Convenient, not unique.
- `online` — a selector for an offline device fails; it never switches.
- `capabilities` — `available` / `unavailable` with reasons.
- `adapter` — `linux-hyprland`, `linux-x11`, `windows`.
- `browser` — binary/profile/running state.

If exactly one device is connected and this conversation has no pin, the
selector may be omitted. Otherwise pass `device_id` (or an unambiguous `device`)
on every call.

**Pinning:** naming a device once pins it for this conversation (12 h). A later
failure on that device is reported as that device being offline; do not "try the
other one" silently. If the user wants a different machine, name it explicitly.

## 2. Pick the right tool

| Need | Tool |
|---|---|
| See what the machine can do | `desktop_ui_status` |
| Read/edit files | `desktop_read_file`, `desktop_write_file`, `desktop_patch`, `desktop_search_files` |
| Run a command | `desktop_terminal` (short), `desktop_job_start` (long, persistent logs) |
| Windows / focus | `desktop_ui_windows`, `desktop_ui_windows(focus=...)` |
| Look at the screen | `desktop_ui_screenshot` (needs observe grant) |
| Click/type/drag | `desktop_ui_action` (needs assist/control grant) |
| Structured web work | `desktop_browser_*` (needs assist/control grant) |
| Windows CUA/structured UI | `desktop_computer_*` (unchanged upstream surface) |

Prefer semantic state over pixels: `desktop_ui_windows` then focus/act, or
`desktop_browser_inspect` then click by ref. Screenshot only when the app has no
queryable state.

## 3. Grants and denials

Observation and input are separate. A `grant_required` response is not a bug:

1. Call `desktop_computer_grant_request(mode="observe"|"assist"|"control",
   duration_seconds=..., reason=...)`.
2. The local user approves at a visible prompt, or through
   `hermes-relay grants approve <id>` when the client is headless.
3. Retry the original action. Grants are task-scoped and expire.

If the user denies it, stop and report that. Do not retry in a loop or look for
another device, browser profile, or shell route around the denial.

## 4. Coordinates and verification

Every capture returns `origin`, `scale`, `pixel_width`, `pixel_height`, and the
contract `global = origin + pixel / scale`. Click in **global logical**
coordinates, not raw screenshot pixels (they differ under fractional scaling).

After an action, read the returned `verification`:

- `cursor` — where the pointer actually is.
- `active_window` — what actually has focus.
- `image_changed` — pixels changed when `verify=true`.
- `ok` — null means "not measured", not success.

For browser work: `inspect` → ref → click/type → `wait` for a selector or text →
`inspect`/`screenshot` again. Refs expire after navigation.

Never report success from the command exit alone. Confirm the state the user
asked for (file content, window title, page text, downloaded file size).

## 5. Platform limits to state plainly

- **Hyprland**: needs the `wl-input` helper for drag and atomic clicks. Without
  it, clicks fall back to `wlrctl` (verified, but known quirks) and drag is
  unavailable. `hermes-relay setup-input` builds it. Keyboard goes to the
  focused window; there is no silent background typing. Screenshots need `grim`.
- **X11**: `xdotool` + ImageMagick/scrot; physical pixels, no Wayland surfaces.
- **Windows**: window discovery/focus via PowerShell; capture/input go through
  `desktop_computer_*`; drag is not exposed by the compatibility bridge.
- **Browser**: dedicated automation profile only; no existing signed-in profile,
  no iframes/shadow DOM, downloads land in the profile's `downloads/`.
- A logged-in graphical session is required. A service that starts before login
  will report `no_graphical_session`; fix the login service, don't retry.

## 6. Failure playbook

| Error | Meaning | Action |
|---|---|---|
| `no_graphical_session` | client is outside a graphical login | tell the user to start the login service; check `hermes-relay daemon --status` |
| `missing_dependency` | `grim`/`wtype`/`wl-input` absent | report the exact install hint; offer `hermes-relay setup-input` |
| `capability_unavailable` | platform can't express it (e.g. drag without helper) | state the limit; propose a fallback the user accepts |
| `invalid_request` + layout bounds | coordinate outside the desktop | re-read `desktop_ui_status` displays; don't clamp |
| `window_not_found` / `not_visible` | stale id or hidden workspace | re-list windows; ask before switching workspaces |
| `grant_required` | no active grant | request it once, then wait for the user |
| `browser_error` | no binary, launch failure, CDP timeout | `desktop_browser_status`; check `<profile>/chromium.log` |
| pinned/offline error | selected machine disconnected | report its name and last-seen; do not switch |

## 7. Useful recipes

**Read/edit/verify a file on the laptop**

```
desktop_devices
desktop_read_file(device_id=..., path="/tmp/relay-test.txt")
desktop_write_file(device_id=..., path="/tmp/relay-test.txt", content="...")
desktop_read_file(...)  # verify the exact bytes
```

**Command + exit code**

```
desktop_terminal(device_id=..., command="uname -a", timeout=15)
```

**Desktop action with proof**

```
desktop_ui_windows(device_id=..., app="firefox")
desktop_ui_windows(device_id=..., focus="0x55aa...")
desktop_ui_action(device_id=..., action="type", text="hello", verify=true)
desktop_ui_screenshot(device_id=..., target="window", window_id="0x55aa...")
```

**Browser flow**

```
desktop_browser_open(device_id=..., url="https://example.com/login")
desktop_browser_inspect(device_id=...)
desktop_browser_type(device_id=..., ref="h...-1", text="user", clear=true)
desktop_browser_click(device_id=..., ref="h...-2")
desktop_browser_wait(device_id=..., text="Welcome")
desktop_browser_download(device_id=..., selector="a[download]")
```

**Diagnose "can you reach my PC?"**

```
desktop_devices
desktop_health
# if offline, report the remembered name/id and last-seen age
```
