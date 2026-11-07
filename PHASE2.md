# Phase 2 — Linux desktop adapter for the Python client

Build on `b9ca2a05` in this worktree/branch `feat/python-client`.

## Goal

Add the Linux graphical path to the Python client, matching the existing
TypeScript adapter's behavior:

- `plugin/relay/client/platform/base.py` — adapter interface:
  `environment()`, `displays()`, `windows()`, `focus_window(id)`,
  `capture(request)`, `pointer(request)`, `type_text(text)`, `keys(keys)`.
- `plugin/relay/client/platform/command.py` — bounded subprocess runner with
  timeout/kill, stdout/stderr capture.
- `plugin/relay/client/platform/linux_hyprland.py`
- `plugin/relay/client/platform/linux_x11.py`
- `plugin/relay/client/platform/__init__.py` — select by environment
  (`HYPRLAND_INSTANCE_SIGNATURE`, `WAYLAND_DISPLAY`, `DISPLAY`), never by OS
  alone.

Reference: `desktop/src/tools/platform/hyprland.ts`, `x11.ts`, `command.ts`,
`types.ts`, and `plugin/tools/desktop_ui_tools.py`.

## Tools to add

`desktop_ui_status`, `desktop_ui_windows`, `desktop_ui_screenshot`,
`desktop_ui_action`, plus the legacy `desktop_screenshot` if the server schema
expects it. Advertise only what is actually implemented; capability-gate
missing `grim`/`wtype`/`wl-input` and return the exact reason.

## Hyprland specifics

- 0.56+ parses `hyprctl dispatch` as Lua: use
  `hl.dsp.focus({window="address:0x…"})` and
  `hl.dsp.cursor.move({x=…, y=…})`; older releases use the string form.
  Detect the version with `hyprctl -j version`.
- Capture with `grim`; keyboard with `wtype`; pointer with the existing
  `wl-input` helper (absolute move/click/drag/scroll); fall back to `wlrctl`
  and report drag unavailable without the helper.
- Return `origin`, `scale`, `pixel_width`, `pixel_height` and coordinate
  contract `global = origin + pixel / scale`.
- Verify focus by re-reading `activewindow`; verify pointer by `cursorpos`.
  Never claim success from an exit code alone.

## Tests

Unit tests with a fake command runner covering: Hyprland 0.54 vs 0.56
dispatch strings, focus verification failure, missing grim/wtype/wl-input,
screenshot metadata, drag unavailable, multi-display bounds. Do not require a
live compositor; optionally add a headless sway test behind an env flag.

## Out of scope

Browser/CDP, Windows, computer-use/CUA, service installers, packaging.

## Constraints

Same as phase 1: isolated worktree only, commit to `feat/python-client`, no
live tree/service changes, real test output, no fake claims.
