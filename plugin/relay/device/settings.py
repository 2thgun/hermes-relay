"""Role and connection settings for the headless relay client.

One plugin install can act as the relay *host* (the machine that serves pair
codes and relays traffic), as a *client* (the controlled computer that runs
``desktop_*`` tools), or as *both* on a single machine.  ``HERMES_RELAY_ROLE``
selects the role; it defaults to ``host`` so an existing install keeps its
current behaviour after upgrading.

Resolution order (first non-empty value wins):

1. Explicit CLI arguments (``--url`` / ``--state`` on the client commands).
2. Process environment (``HERMES_RELAY_ROLE``, ``HERMES_RELAY_URL``,
   ``HERMES_RELAY_CLIENT_STATE``).
3. ``$HERMES_HOME/.env`` with the same variable names (Hermes' documented
   secrets/config file).
4. ``$HERMES_HOME/config.yaml`` → ``plugins.entries.hermes-relay.settings``
   with keys ``role`` (alias ``relay_role``), ``relay_url``, ``client_state``.
5. Built-in defaults: role ``host``, URL unset, state
   ``$HERMES_HOME/relay/client.json``.

The module is deliberately stdlib-only.  A client install does not ship
PyYAML; when it is unavailable the ``config.yaml`` layer is skipped rather
than failing, and environment/``.env`` settings keep working.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .identity import default_store_path, hermes_home

logger = logging.getLogger(__name__)

PLUGIN_ID = "hermes-relay"
DEFAULT_ROLE = "host"
VALID_ROLES = ("host", "client", "both")

ENV_ROLE = "HERMES_RELAY_ROLE"
ENV_URL = "HERMES_RELAY_URL"
ENV_CLIENT_STATE = "HERMES_RELAY_CLIENT_STATE"

#: Plugin-config keys read from ``plugins.entries.hermes-relay.settings``.
CONFIG_ROLE_KEYS = ("role", "relay_role")
CONFIG_URL_KEY = "relay_url"
CONFIG_STATE_KEY = "client_state"

SOURCE_CLI = "cli"
SOURCE_ENV = "env"
SOURCE_DOTENV = "dotenv"
SOURCE_CONFIG = "config"
SOURCE_DEFAULT = "default"


class ConfigError(ValueError):
    """Raised for an invalid role or unreadable connection setting."""


def normalize_role(raw: str | None) -> str:
    """Validate a configured role, defaulting to ``host`` when unset."""
    if raw is None:
        return DEFAULT_ROLE
    text = str(raw).strip().lower()
    if not text:
        return DEFAULT_ROLE
    if text not in VALID_ROLES:
        raise ConfigError(
            f"invalid {ENV_ROLE} value {raw!r}; expected one of {', '.join(VALID_ROLES)}"
        )
    return text


def _read_dotenv(path: Path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines from a Hermes ``.env`` file."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def plugin_settings_path(home: Path | str | None = None) -> Path:
    """Path of the Hermes ``config.yaml`` that holds plugin settings."""
    return (
        Path(home).expanduser() / "config.yaml"
        if home
        else hermes_home() / "config.yaml"
    )


def load_plugin_settings(
    home: Path | str | None = None,
    *,
    config_path: Path | str | None = None,
) -> dict[str, Any]:
    """Read ``plugins.entries.hermes-relay.settings`` from ``config.yaml``.

    Returns an empty mapping when the file, PyYAML, or the section is absent.
    Malformed YAML is logged and ignored: role resolution must not make the
    client unstartable.
    """
    path = (
        Path(config_path).expanduser()
        if config_path is not None
        else plugin_settings_path(home)
    )
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        logger.debug("PyYAML unavailable; reading relay settings from env only")
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError:
        return {}
    except Exception as exc:  # noqa: BLE001 - malformed YAML must not block startup
        logger.warning("Ignoring unreadable config %s: %s", path, exc)
        return {}
    if not isinstance(data, Mapping):
        return {}
    plugins = data.get("plugins")
    if not isinstance(plugins, Mapping):
        return {}
    entries = plugins.get("entries")
    if not isinstance(entries, Mapping):
        return {}
    entry = entries.get(PLUGIN_ID) or entries.get(PLUGIN_ID.replace("-", "_"))
    if not isinstance(entry, Mapping):
        return {}
    settings = entry.get("settings")
    return dict(settings) if isinstance(settings, Mapping) else {}


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True)
class RoleSettings:
    """Resolved role plus the client connection settings."""

    role: str = DEFAULT_ROLE
    relay_url: str | None = None
    client_state: Path = field(default_factory=default_store_path)
    sources: dict[str, str] = field(default_factory=dict)

    @property
    def runs_host(self) -> bool:
        return self.role in ("host", "both")

    @property
    def runs_client(self) -> bool:
        return self.role in ("client", "both")

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "relay_url": self.relay_url,
            "client_state": str(self.client_state),
            "sources": dict(self.sources),
        }


def resolve_settings(
    *,
    role: str | None = None,
    relay_url: str | None = None,
    client_state: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | str | None = None,
    config_path: Path | str | None = None,
    plugin_config: Mapping[str, Any] | None = None,
) -> RoleSettings:
    """Resolve the effective role/URL/state path for this process.

    ``env`` and ``plugin_config`` are injectable for tests; by default the
    process environment, ``$HERMES_HOME/.env``, and ``config.yaml`` are read.
    """
    environ = os.environ if env is None else env
    base = Path(home).expanduser() if home is not None else hermes_home()
    dotenv = _read_dotenv(base / ".env")
    config = (
        dict(plugin_config)
        if plugin_config is not None
        else load_plugin_settings(base, config_path=config_path)
    )
    sources: dict[str, str] = {}

    def pick(
        explicit: Any,
        env_name: str,
        config_keys: tuple[str, ...],
    ) -> tuple[str | None, str | None]:
        value = _clean(explicit)
        if value is not None:
            return value, SOURCE_CLI
        value = _clean(environ.get(env_name))
        if value is not None:
            return value, SOURCE_ENV
        value = _clean(dotenv.get(env_name))
        if value is not None:
            return value, SOURCE_DOTENV
        for key in config_keys:
            value = _clean(config.get(key))
            if value is not None:
                return value, SOURCE_CONFIG
        return None, None

    role_value, role_source = pick(role, ENV_ROLE, CONFIG_ROLE_KEYS)
    resolved_role = normalize_role(role_value)
    sources["role"] = role_source or SOURCE_DEFAULT

    url_value, url_source = pick(relay_url, ENV_URL, (CONFIG_URL_KEY,))
    sources["relay_url"] = url_source or SOURCE_DEFAULT

    state_value, state_source = pick(
        client_state, ENV_CLIENT_STATE, (CONFIG_STATE_KEY,)
    )
    state_path = (
        Path(state_value).expanduser()
        if state_value is not None
        else base / "relay" / "client.json"
    )
    sources["client_state"] = state_source or SOURCE_DEFAULT

    return RoleSettings(
        role=resolved_role,
        relay_url=url_value,
        client_state=state_path,
        sources=sources,
    )


__all__ = [
    "CONFIG_STATE_KEY",
    "CONFIG_URL_KEY",
    "DEFAULT_ROLE",
    "ENV_CLIENT_STATE",
    "ENV_ROLE",
    "ENV_URL",
    "PLUGIN_ID",
    "VALID_ROLES",
    "ConfigError",
    "RoleSettings",
    "load_plugin_settings",
    "normalize_role",
    "plugin_settings_path",
    "resolve_settings",
]
