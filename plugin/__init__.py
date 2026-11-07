"""
hermes-relay plugin — registers android_* + desktop_* tools and the
`hermes pair` + `hermes relay` CLI sub-commands into hermes-agent via the
v0.3.0+ plugin system.

Drop this folder into ~/.hermes/plugins/hermes-relay and add `hermes-relay`
to `plugins.enabled` in ~/.hermes/config.yaml, then restart hermes.
Run `hermes pair` to generate a QR code for pairing clients.

Import cost matters here: ``python -m plugin.relay.device`` imports this
package first, and the headless client must start on a machine that only has
its own runtime dependencies (``aiohttp``, ``websocket-client``).  Tool
modules pull in ``requests``/``yaml``/``segno`` and the rest of the plugin
stack, so they are imported inside :func:`register` (or on first attribute
access via :pep:`562`) instead of at module import time.

Role awareness: ``plugins.entries.hermes-relay.settings.role`` (or
``HERMES_RELAY_ROLE``) selects what this install runs.  ``host`` (default)
keeps the embedded relay sidecar; ``client`` starts the Python device client
only; ``both`` runs both.  See :func:`start_role_services`.
"""

import importlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Attributes that used to be imported eagerly at module scope.  Kept as lazy
# re-exports so ``from plugin import _SCHEMAS`` and friends keep working for
# callers that reach into the plugin package directly.
_LAZY_TOOL_ATTRIBUTES: dict[str, tuple[str, str]] = {
    "_SCHEMAS": ("plugin.tools.android_tool", "_SCHEMAS"),
    "_HANDLERS": ("plugin.tools.android_tool", "_HANDLERS"),
    "_check_requirements": ("plugin.tools.android_tool", "_check_requirements"),
    "_DESKTOP_SCHEMAS": ("plugin.tools.desktop_tool", "_SCHEMAS"),
    "_DESKTOP_HANDLERS": ("plugin.tools.desktop_tool", "_HANDLERS"),
    "_desktop_check_tool": ("plugin.tools.desktop_tool", "_check_tool"),
    "_RELAY_PLUGIN_SCHEMAS": ("plugin.tools.relay_plugin_tool", "_SCHEMAS"),
    "_RELAY_PLUGIN_HANDLERS": ("plugin.tools.relay_plugin_tool", "_HANDLERS"),
}


def __getattr__(name: str) -> Any:
    """Resolve legacy tool-table attributes without importing them eagerly."""
    try:
        module_name, attribute = _LAZY_TOOL_ATTRIBUTES[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(importlib.import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_TOOL_ATTRIBUTES))


def _resolve_role_settings():
    """Load the role/URL/state settings, falling back to ``host``.

    An invalid role must not make the plugin unloadable; it downgrades to the
    pre-phase-6 behaviour with a warning naming the bad value.
    """
    from .relay.device.settings import ConfigError, resolve_settings

    try:
        return resolve_settings()
    except ConfigError as exc:
        logger.warning(
            "Invalid Hermes-Relay role configuration (%s); falling back to role=host",
            exc,
        )
        return resolve_settings(role="host")


def _start_embedded_host() -> bool:
    """Start the relay sidecar; returns True when this call started one."""
    try:
        from .relay.embedded import start_embedded_relay

        return start_embedded_relay() is not None
    except Exception:
        logger.warning("Embedded relay startup failed; continuing", exc_info=True)
        return False


def start_role_services(settings=None) -> dict[str, Any]:
    """Start the services required by this install's configured role.

    * ``host``   — embedded relay sidecar only.
    * ``client`` — no relay listener; detached Python device client only.
    * ``both``   — relay sidecar plus device client.

    The device client starts only with a stored, unexpired session token and a
    resolvable relay URL; otherwise the reason is logged and nothing spawns.
    Idempotent across repeated registration: a live child or pid file is not
    duplicated.  Returns a summary dict for tests/diagnostics.
    """
    if settings is None:
        try:
            settings = _resolve_role_settings()
        except Exception:
            logger.warning(
                "Could not resolve the Hermes-Relay role; assuming host", exc_info=True
            )
            return {
                "role": "host",
                "host_started": _start_embedded_host(),
                "device_started": False,
                "device_reason": "role-unresolved",
            }

    role = settings.role
    host_started = False
    device_started = False
    device_reason = "not-client"

    if settings.runs_host:
        host_started = _start_embedded_host()
    else:
        logger.info("Hermes-Relay role=%s: embedded relay host not started", role)

    if settings.runs_client:
        try:
            from .relay.device.supervisor import start_device_client

            result = start_device_client(settings)
            device_started = result.started
            device_reason = result.reason
        except Exception:
            logger.warning("Device client startup failed; continuing", exc_info=True)
            device_reason = "error"

    return {
        "role": role,
        "host_started": host_started,
        "device_started": device_started,
        "device_reason": device_reason,
    }


def register(ctx):
    """Called by hermes-agent plugin loader. Registers tools and CLI commands."""
    # Imported here, not at module scope: a relay *client* install must not
    # need requests/segno/yaml just to import `plugin`.
    from .tools.android_tool import (
        _HANDLERS,
        _SCHEMAS,
        _check_requirements,
    )
    from .tools.desktop_tool import (
        _HANDLERS as _DESKTOP_HANDLERS,
    )
    from .tools.desktop_tool import (
        _SCHEMAS as _DESKTOP_SCHEMAS,
    )
    from .tools.desktop_tool import (
        _check_tool as _desktop_check_tool,
    )
    from .tools.relay_plugin_tool import (
        _HANDLERS as _RELAY_PLUGIN_HANDLERS,
    )
    from .tools.relay_plugin_tool import (
        _SCHEMAS as _RELAY_PLUGIN_SCHEMAS,
    )

    # Register 14 android_* tools
    for tool_name, schema in _SCHEMAS.items():
        ctx.register_tool(
            name=tool_name,
            toolset="android",
            schema=schema,
            handler=_HANDLERS[tool_name],
            check_fn=(lambda: True) if tool_name == "android_setup" else _check_requirements,
        )

    # Register 5 desktop_* tools (Phase B client-side tool routing).
    # Per-tool check_fn closure so the relay ping is scoped to THIS tool,
    # not the whole toolset — matches the pattern in desktop_tool.py module
    # level registration (which is a no-op fallback for non-plugin-context
    # imports like smoke tests).
    for tool_name, schema in _DESKTOP_SCHEMAS.items():
        def _make_desktop_check(name: str):
            return lambda: _desktop_check_tool(name)

        ctx.register_tool(
            name=tool_name,
            toolset="desktop",
            schema=schema,
            handler=_DESKTOP_HANDLERS[tool_name],
            check_fn=_make_desktop_check(tool_name),
        )

    # Declarative Android pages live on the Hermes host and need no paired
    # device or relay process. They remain available for authoring/inspection
    # whenever this plugin is loaded.
    for tool_name, schema in _RELAY_PLUGIN_SCHEMAS.items():
        ctx.register_tool(
            name=tool_name,
            toolset="relay",
            schema=schema,
            handler=_RELAY_PLUGIN_HANDLERS[tool_name],
            check_fn=lambda: True,
        )

    # Register the in-session `/relay` slash command (status/devices/pair) and
    # the `on_session_start` lifecycle hook. Both are self-guarded internally,
    # and wrapped here too so a missing register_command/register_hook on an
    # older hermes-agent build cannot block tool/CLI registration below.
    try:
        from .slash import register_slash_commands

        register_slash_commands(ctx)
    except Exception:
        # Slash commands are additive; never let them break plugin load.
        pass

    try:
        from .hooks import register_hooks

        register_hooks(ctx)
    except Exception:
        # The lifecycle hook is best-effort; never block plugin load.
        pass

    # Register the "phone" platform so the agent can proactively push to the
    # paired device (`send_message target=phone`, cron `deliver=phone`).
    # Additive + off-by-default (PHONE_ENABLED gate): guarded so an older
    # hermes-agent without register_platform — or a host where gateway.* is
    # unavailable at import — can't block tool/CLI registration above.
    try:
        from .phone_platform import register_phone_platform

        register_phone_platform(ctx)
    except (AttributeError, ImportError):
        # Older hermes-agent (no register_platform) — platform not registered.
        # Tools/CLI above still work.
        pass
    except Exception:
        # Don't silently swallow — a real registration failure (e.g. an
        # upstream PlatformEntry signature drift) should be visible, not lost
        # at DEBUG. The platform is still additive; tool/CLI registration above
        # is unaffected.
        logger.warning("Phone platform registration failed; continuing", exc_info=True)

    # Prefer the modern profile-keyed ownership ledger. Older Hermes hosts
    # retain the guarded process wrapper as a compatibility fallback.
    try:
        from .enhancements.context_injection import register_context_sections

        native_context_registered = register_context_sections(ctx)
    except Exception:
        # A current host exposing the API must not fall back to an unowned
        # process-global mutation after a real registration failure.
        native_context_registered = True
        logger.warning(
            "Relay system-prompt section registration failed; continuing without it",
            exc_info=True,
        )

    if not native_context_registered:
        try:
            from .enhancements import apply_phase

            apply_phase("plugin_load")
        except Exception:
            logger.debug("Relay plugin-load enhancements failed; continuing", exc_info=True)

    # Start the services this install's role requires: the embedded relay host
    # for host/both, the Python device client for client/both.  Role defaults to
    # host, so an upgraded install keeps its previous behaviour.  Failures are
    # contained here: tools/CLI registration above must still complete.
    try:
        start_role_services()
    except Exception:
        logger.warning("Role service startup failed; continuing", exc_info=True)

    # Register plugin-native CLI sub-commands: hermes pair + hermes relay.
    # Wrapped in try/except so the plugin still works on older hermes-agent
    # versions that do not expose register_cli_command.
    try:
        from .cli import (
            pair_command,
            register_cli,
            register_relay_cli,
            relay_command,
        )

        ctx.register_cli_command(
            name="pair",
            help="Generate QR code for pairing mobile devices with the Hermes API server",
            setup_fn=register_cli,
            handler_fn=pair_command,
            description=(
                "Reads API server config (host, port, key) and renders a scannable "
                "QR code in the terminal. Use to connect Hermes-Relay Android or any "
                "mobile client to your gateway."
            ),
        )

        ctx.register_cli_command(
            name="relay",
            help="Run the Hermes-Relay WSS server (terminal + bridge channels)",
            setup_fn=register_relay_cli,
            handler_fn=relay_command,
            description=(
                "Runs the embedded WSS relay server that the Hermes-Relay Android "
                "app connects to for the terminal and bridge channels. Chat still "
                "goes direct to the Hermes API server."
            ),
        )
    except (AttributeError, ImportError):
        # Older hermes-agent (v0.7.0 and earlier) — CLI commands not registered.
        # The tools above still work; users can fall back to `python -m plugin.relay`.
        pass
