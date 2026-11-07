#!/usr/bin/env bash
# Install the headless Hermes-Relay Python client as a systemd *user* service.
#
# The unit starts with the graphical session (graphical-session.target) and
# runs the Python entry point — not the Bun/TypeScript client. No root access
# is required and no system-wide unit is created.
#
# Usage:
#   scripts/install-client-linux.sh [--url URL] [--state PATH] [--print|--dry-run]
#
#   --print     print the generated unit and exit (no files, no systemctl)
#   --dry-run   print the files/commands that would be used, then exit
#   --python    Python interpreter to embed in the unit (default: current)
#   --exec      console-script path to embed instead of `python -m ...`
#   --unit-dir  override the systemd user unit directory
#   --url       relay URL to embed (default: HERMES_RELAY_URL / plugin setting)
#   --state     client store path to embed (default: $HERMES_HOME/relay/client.json)
#
# Set HERMES_RELAY_CLIENT to run a specific `hermes-relay-client` executable,
# or PYTHON to choose the interpreter used to invoke the CLI itself.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"

MODE=""
ARGS=()

usage() {
    sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --print)
            MODE="--print"
            shift
            ;;
        --dry-run)
            MODE="--dry-run"
            shift
            ;;
        --url | --state | --python | --exec | --unit-dir)
            if [[ $# -lt 2 ]]; then
                echo "error: $1 requires a value" >&2
                exit 2
            fi
            ARGS+=("$1" "$2")
            shift 2
            ;;
        --url=* | --state=* | --python=* | --exec=* | --unit-dir=*)
            ARGS+=("$1")
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            echo "error: unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ -n "${HERMES_RELAY_CLIENT:-}" ]]; then
    CMD=("$HERMES_RELAY_CLIENT")
elif command -v hermes-relay-client >/dev/null 2>&1; then
    CMD=("hermes-relay-client")
else
    CMD=("$PYTHON" "-m" "plugin.relay.device")
    export PYTHONPATH="${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
fi

if [[ -n "$MODE" ]]; then
    exec "${CMD[@]}" install --platform linux "$MODE" "${ARGS[@]}"
fi
exec "${CMD[@]}" install --platform linux "${ARGS[@]}"
