"""Command line entry point for the headless Python relay client.

Usage::

    python -m plugin.relay.client pair --url wss://host:8767 --code ABC123
    python -m plugin.relay.client run  --url wss://host:8767
    python -m plugin.relay.client tools
    python -m plugin.relay.client status

``pair`` performs one handshake, stores the minted session token in
``$HERMES_HOME/relay/client.json`` and exits.  ``run`` serves ``desktop_*``
tool calls until interrupted, reconnecting with exponential backoff.

Diagnostics go to stderr; machine-readable events go to stdout (one JSON
object per line) when ``--json`` is passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from typing import Any

from . import __version__
from .handlers import all_tool_names
from .identity import ClientIdentity, IdentityError, default_store_path, normalize_relay_url
from .runtime import (
    ServeOptions,
    pair,
    prepare_serve,
    serve,
)
from .transport import AuthError, TransportError

logger = logging.getLogger("hermes_relay.client")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


def _split_tools(raw: str | None) -> set[str] | None:
    if not raw:
        return None
    names = {part.strip() for part in raw.replace(",", " ").split() if part.strip()}
    return names or None


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True), flush=True)
        return
    for key, value in payload.items():
        print(f"{key}: {value}", flush=True)


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


# ── pair ────────────────────────────────────────────────────────────────────


async def _cmd_pair(args: argparse.Namespace) -> int:
    try:
        result = await pair(
            args.url,
            args.code,
            name=args.name,
            store_path=args.state,
            insecure=args.insecure,
            auth_timeout=args.timeout,
        )
    except IdentityError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return EXIT_USAGE
    except AuthError as exc:
        print(json.dumps({"ok": False, "error": f"pairing rejected: {exc}"}), file=sys.stderr)
        return EXIT_ERROR
    except (TransportError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return EXIT_ERROR

    _emit(
        {
            "ok": True,
            "url": result.url,
            "device_id": result.device_id,
            "device_name": result.device_name,
            "server_version": result.server_version,
            "transport_hint": result.transport_hint,
            "expires_at": _iso(result.expires_at),
            "stored_path": result.stored_path,
        },
        as_json=args.json,
    )
    return EXIT_OK


# ── run ─────────────────────────────────────────────────────────────────────


def _resolve_run_url(args: argparse.Namespace, identity: ClientIdentity) -> str:
    if args.url:
        return normalize_relay_url(args.url)
    active = identity.active_record()
    if active is None:
        raise IdentityError(
            "no relay URL supplied and the client store has no active pairing; "
            "run `python -m plugin.relay.client pair` first"
        )
    return active[0]


async def _cmd_run(args: argparse.Namespace) -> int:
    identity = ClientIdentity.load(args.state)
    try:
        url = _resolve_run_url(args, identity)
    except IdentityError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return EXIT_USAGE

    allow = _split_tools(args.allow_tools)
    deny = _split_tools(args.deny_tools)
    if allow:
        known = set(all_tool_names())
        unknown = sorted(allow - known)
        if unknown:
            logger.warning(
                "allowed tool(s) not implemented on this platform: %s", ", ".join(unknown)
            )

    options = ServeOptions(
        url=url,
        token=args.token or None,
        code=args.code,
        cwd=args.cwd or os.getcwd(),
        allow=allow,
        deny=deny,
        store_path=args.state,
        insecure=args.insecure,
        heartbeat_interval=args.heartbeat,
        handler_timeout=args.handler_timeout,
        reconnect_base=args.reconnect_base,
        reconnect_max=args.reconnect_max,
        max_reconnect_attempts=args.max_reconnect_attempts,
        once=args.once,
    )

    try:
        handle, identity = prepare_serve(options)
    except IdentityError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return EXIT_USAGE

    advertised = handle.dispatcher.registry.names()
    if not advertised:
        print(json.dumps({"ok": False, "error": "no tools selected"}), file=sys.stderr)
        return EXIT_USAGE

    def on_ready(auth: Any) -> None:
        _emit(
            {
                "event": "ready",
                "url": url,
                "device_id": identity.device_id,
                "device_name": identity.device_name,
                "server_version": auth.server_version,
                "advertised_tools": advertised,
                "cwd": options.cwd,
            },
            as_json=args.json,
        )

    def on_disconnected(reason: str) -> None:
        _emit({"event": "disconnected", "reason": reason}, as_json=args.json)

    options.on_ready = on_ready
    options.on_disconnected = on_disconnected

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    async def _watch_stop() -> None:
        await stop_event.wait()
        handle.stop_event.set()
        handle.transport.stop()

    watcher = asyncio.create_task(_watch_stop(), name="cli-stop")
    try:
        await serve(handle, identity, options)
    except AuthError as exc:
        print(json.dumps({"ok": False, "error": f"authentication failed: {exc}"}), file=sys.stderr)
        return EXIT_ERROR
    except (TransportError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return EXIT_ERROR
    finally:
        watcher.cancel()
        try:
            await watcher
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    _emit({"event": "stopped", "reason": "once" if args.once else "signal"}, as_json=args.json)
    return EXIT_OK


# ── tools / status ──────────────────────────────────────────────────────────


def _cmd_tools(args: argparse.Namespace) -> int:
    allow = _split_tools(args.allow_tools)
    deny = _split_tools(args.deny_tools)
    from .dispatch import ClientState
    from .handlers import build_registry

    registry = build_registry(state=ClientState(), allow=allow, deny=deny)
    names = registry.names()
    if args.json:
        print(json.dumps({"ok": True, "tools": names}, sort_keys=True))
    else:
        for name in names:
            print(name)
    return EXIT_OK


def _cmd_status(args: argparse.Namespace) -> int:
    try:
        identity = ClientIdentity.load(args.state)
    except IdentityError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return EXIT_ERROR
    rows = []
    for url, record in sorted(identity.hosts.items()):
        rows.append(
            {
                "url": url,
                "server_version": record.server_version,
                "paired_at": _iso(record.paired_at),
                "expires_at": _iso(record.expires_at),
                "transport_hint": record.transport_hint,
                "token_present": bool(record.session_token),
                "token_expired": record.is_expired(),
                "active": url == identity.active_url,
            }
        )
    payload = {
        "ok": True,
        "device_id": identity.device_id,
        "device_name": identity.device_name,
        "store": str(identity.path),
        "hosts": rows,
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"device_id: {identity.device_id}")
        print(f"device_name: {identity.device_name}")
        print(f"store: {identity.path}")
        for row in rows:
            marker = " *" if row["active"] else ""
            print(f"  {row['url']}{marker} (paired {row['paired_at'] or '?'})")
    return EXIT_OK


# ── Parser ──────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m plugin.relay.client",
        description="Headless Hermes-Relay client: pair a computer and serve desktop tools.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    common_state = argparse.ArgumentParser(add_help=False)
    common_state.add_argument(
        "--state",
        default=None,
        help=f"client store path (default: {default_store_path()})",
    )

    pair_parser = sub.add_parser("pair", parents=[common_state], help="pair and store a session token")
    pair_parser.add_argument("--url", required=True, help="relay WebSocket URL (ws:// or wss://)")
    pair_parser.add_argument("--code", required=True, help="one-time pairing code")
    pair_parser.add_argument("--name", default=None, help="display name (default: hostname)")
    pair_parser.add_argument("--insecure", action="store_true", help="skip TLS verification")
    pair_parser.add_argument("--timeout", type=float, default=15.0, help="auth timeout in seconds")
    pair_parser.add_argument("--json", action="store_true", help="JSON output")
    pair_parser.set_defaults(handler=_cmd_pair, is_async=True)

    run_parser = sub.add_parser("run", parents=[common_state], help="connect and serve desktop tools")
    run_parser.add_argument("--url", default=None, help="relay WebSocket URL (default: active pairing)")
    run_parser.add_argument("--code", default=None, help="pairing code to exchange on first connect")
    run_parser.add_argument("--token", default=None, help="session token override")
    run_parser.add_argument("--cwd", default=os.getcwd(), help="working directory for tool handlers")
    run_parser.add_argument(
        "--allow-tools", default=None, help="comma-separated tool allowlist (default: all)"
    )
    run_parser.add_argument("--deny-tools", default=None, help="comma-separated tools to disable")
    run_parser.add_argument("--insecure", action="store_true", help="skip TLS verification")
    run_parser.add_argument("--heartbeat", type=float, default=30.0, help="desktop.status interval")
    run_parser.add_argument("--handler-timeout", type=float, default=30.0, help="per-request timeout")
    run_parser.add_argument("--reconnect-base", type=float, default=1.0, help=argparse.SUPPRESS)
    run_parser.add_argument("--reconnect-max", type=float, default=30.0, help=argparse.SUPPRESS)
    run_parser.add_argument("--max-reconnect-attempts", type=int, default=0, help=argparse.SUPPRESS)
    run_parser.add_argument(
        "--once",
        action="store_true",
        help="exit after the first successfully served tool call (smoke tests)",
    )
    run_parser.add_argument("--json", action="store_true", help="JSON event output")
    run_parser.set_defaults(handler=_cmd_run, is_async=True)

    tools_parser = sub.add_parser("tools", help="list tool names this client advertises")
    tools_parser.add_argument("--allow-tools", default=None, help="comma-separated allowlist")
    tools_parser.add_argument("--deny-tools", default=None, help="comma-separated denylist")
    tools_parser.add_argument("--json", action="store_true", help="JSON output")
    tools_parser.set_defaults(handler=_cmd_tools)

    status_parser = sub.add_parser("status", parents=[common_state], help="show stored pairings")
    status_parser.add_argument("--json", action="store_true", help="JSON output")
    status_parser.set_defaults(handler=_cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, (os.environ.get("HERMES_RELAY_LOG_LEVEL") or "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    handler = args.handler
    if getattr(args, "is_async", False):
        try:
            return asyncio.run(handler(args))
        except KeyboardInterrupt:
            return EXIT_OK
    return handler(args)


if __name__ == "__main__":  # pragma: no cover - exercised by subprocess tests
    sys.exit(main())
