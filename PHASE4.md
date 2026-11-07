# Phase 4 — Windows adapter for the Python client

Build on `b73b8452` in this worktree/branch `feat/python-client`.

## Goal

Add `plugin/relay/client/platform/windows.py` and select it when
`sys.platform == "win32"`. Match the TypeScript implementation in
`desktop/src/tools/platform/windows.ts`, including the corrections made during
the hardware run:

- `environment()`: `win32`, PowerShell dependency path, graphical true.
- `displays()`: logical `Screen.Bounds` rect plus `physical_width`,
  `physical_height` from `Win32_VideoController`, and `dpi_scale` derived from
  physical/logical (or AppliedDPI). Do not report scale=1 for a 150% panel.
- `windows()`: PowerShell/user32 list with pid, app, title, rect, focused;
  filter zero-size/hidden windows.
- `focus_window(id)`: `AttachThreadInput` + `BringWindowToTop` + topmost +
  `SetForegroundWindow` + reset topmost, then re-read the foreground. If it did
  not move, raise `input_failed` with the actual foreground; never return
  `ok:true` with a false verification.
- `capture()`: display capture via PowerShell/System.Drawing; return
  `pixel_width/height`, `origin`, `scale`, `dpi_scale`, physical size. Window or
  region capture must return an explicit `capability_unavailable`, not silently
  capture the display.
- `pointer()`: click/move/scroll via the legacy user32 bridge; drag must return
  `capability_unavailable` with the exact reason. Verify cursor if available,
  otherwise report `verification: not_available`.
- `type_text`/`keys`: legacy bridge; they target the foreground window. Refuse
  to claim success when focus verification failed.

Do not implement CUA/computer-use (`desktop_computer_*`) in this phase; leave it
as a documented phase-5 seam.

## Tests

Fake command runner covering: display metadata 2880×1800 logical 1920×1200 at
150%, focus success and failure (foreground stays elsewhere), window filtering,
drag unavailable, window-capture unavailable, and PowerShell quoting of paths
with spaces/quotes. No Windows host required.

## Constraints

Same as phases 1–3: isolated worktree only, commit to `feat/python-client`, no
live tree/service/device changes, real pytest output, honest gaps.
