#!/usr/bin/env bash
# Install Hermes-Relay's desktop agent as a systemd *user* service.
#
# Why a user service: desktop automation needs the logged-in graphical session
# (WAYLAND_DISPLAY / HYPRLAND_INSTANCE_SIGNATURE / DISPLAY). A system service
# starts before login and cannot see the compositor. `WantedBy=graphical-session.target`
# keeps it inside the session and lets systemd restart it after a crash.
#
# Usage:
#   ./install-service-linux.sh [--bin /path/to/hermes-relay] [--status] [--uninstall]
#
# The service runs `hermes-relay daemon`, which reconnects with the transport's
# existing exponential backoff. It does not grant desktop tools by itself:
# pairing and host access policy still decide what the agent may do.

set -euo pipefail

UNIT_NAME="hermes-relay-daemon.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
HERMES_DIR="${HERMES_HOME:-$HOME/.hermes}"
WRAPPER="$HERMES_DIR/bin/hermes-relay-daemon-run"
BIN="${HERMES_RELAY_BIN:-}"
ACTION="install"

info() { printf '  %s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --bin) BIN="${2:-}"; shift 2 ;;
    --status) ACTION="status"; shift ;;
    --uninstall) ACTION="uninstall"; shift ;;
    -h|--help)
      sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) fail "unknown argument: $1" ;;
  esac
done

if ! systemctl --user show-environment >/dev/null 2>&1; then
  fail "no systemd user manager for this session. Run this inside a graphical login (not sudo)."
fi

case "$ACTION" in
  uninstall)
    systemctl --user disable --now "$UNIT_NAME" 2>/dev/null || true
    rm -f "$UNIT_DIR/$UNIT_NAME" "$WRAPPER"
    systemctl --user daemon-reload
    systemctl --user reset-failed "$UNIT_NAME" 2>/dev/null || true
    info "removed $UNIT_NAME"
    exit 0
    ;;
  status)
    systemctl --user status "$UNIT_NAME" --no-pager || true
    if command -v hermes-relay >/dev/null 2>&1; then
      echo
      hermes-relay daemon --status || true
    fi
    exit 0
    ;;
esac

if [ -z "$BIN" ]; then
  BIN="$(command -v hermes-relay 2>/dev/null || true)"
fi
[ -n "$BIN" ] || fail "hermes-relay not found on PATH. Pass --bin /absolute/path/to/hermes-relay."
[ -x "$BIN" ] || fail "$BIN is not executable."

mkdir -p "$UNIT_DIR" "$HERMES_DIR/bin"

# The systemd user manager often starts before the compositor exports its
# environment. Recover the Wayland socket and Hyprland signature from the
# runtime directory so the service can find the session without a logout.
cat > "$WRAPPER" <<EOF
#!/usr/bin/env bash
set -euo pipefail
RUNTIME="\${XDG_RUNTIME_DIR:-/run/user/\$(id -u)}"
export XDG_RUNTIME_DIR="\$RUNTIME"

if [ -z "\${WAYLAND_DISPLAY:-}" ]; then
  for candidate in "\$RUNTIME"/wayland-*; do
    [ -S "\$candidate" ] || continue
    case "\$candidate" in *.lock) continue ;; esac
    export WAYLAND_DISPLAY="\$(basename "\$candidate")"
    break
  done
fi

if [ -z "\${HYPRLAND_INSTANCE_SIGNATURE:-}" ]; then
  for base in "\$RUNTIME/hypr" "/tmp/hypr"; do
    [ -d "\$base" ] || continue
    for candidate in "\$base"/*; do
      [ -d "\$candidate" ] || continue
      export HYPRLAND_INSTANCE_SIGNATURE="\$(basename "\$candidate")"
      break 2
    done
  done
fi

exec "$BIN" daemon "\$@"
EOF
chmod 0755 "$WRAPPER"

cat > "$UNIT_DIR/$UNIT_NAME" <<EOF
[Unit]
Description=Hermes Relay desktop agent (paired-computer control)
Documentation=https://github.com/Codename-11/hermes-relay
After=graphical-session.target
PartOf=graphical-session.target
Wants=graphical-session.target

[Service]
Type=simple
ExecStart=$WRAPPER --experimental-computer-use --no-voice
Restart=always
RestartSec=5
TimeoutStopSec=15
KillMode=mixed
Environment=HERMES_RELAY_DAEMON=1

[Install]
WantedBy=graphical-session.target
EOF

systemctl --user daemon-reload
systemctl --user enable "$UNIT_NAME"
systemctl --user restart "$UNIT_NAME"

sleep 1
if systemctl --user is-active --quiet "$UNIT_NAME"; then
  info "installed and started $UNIT_NAME"
  info "logs: journalctl --user -u $UNIT_NAME -f"
  info "status: hermes-relay daemon --status"
else
  printf 'warning: unit installed but not active. Check:\n  journalctl --user -u %s -n 50\n' "$UNIT_NAME" >&2
  exit 1
fi

# Capability hint: the pointer helper is optional and capability-gated, so a
# missing compiler must not fail the service install.
if command -v hermes-relay >/dev/null 2>&1; then
  if ! hermes-relay setup-input --check >/dev/null 2>&1; then
    echo
    info "optional: run 'hermes-relay setup-input' to enable drag/precise clicks on Wayland"
  fi
fi
