"""Client-side identity and session storage for the Python relay client.

The controlled computer is the *client* in the relay protocol.  Pairing with a
relay mints a session token that must survive restarts, and every reconnect has
to present the same stable ``device_id`` so the relay can keep targeting this
machine by an exact identity instead of a display name.

Storage lives at ``$HERMES_HOME/relay/client.json`` (mode 0o600, parent
directory 0o700).  This is deliberately separate from the relay server's
``$HERMES_HOME/hermes-relay-sessions.json``: a client install must never be
able to read or rewrite the server's session database.

File shape (version 1)::

    {
      "version": 1,
      "active_url": "wss://relay.example:8767",
      "device_id": "6f1c...-uuid4",
      "device_name": "workstation",
      "hosts": {
        "wss://relay.example:8767": {
          "session_token": "...",
          "server_version": "1.13.1",
          "paired_at": 1762450000.0,
          "expires_at": null,
          "transport_hint": "wss",
          "grants": {"chat": null, "desktop": null}
        }
      }
    }
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

CLIENT_STORE_VERSION = 1
_DEVICE_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class IdentityError(Exception):
    """Raised when the client store cannot be read or written."""


def hermes_home() -> Path:
    """Resolve ``HERMES_HOME`` (falling back to ``~/.hermes``)."""
    raw = os.environ.get("HERMES_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".hermes"


def default_store_path() -> Path:
    return hermes_home() / "relay" / "client.json"


def normalize_relay_url(url: str) -> str:
    """Canonicalize a relay WebSocket URL for storage keys.

    Only ``ws://`` and ``wss://`` URLs are accepted.  The path is preserved
    (a relay may be mounted behind a reverse proxy), a trailing slash is
    dropped, and scheme/host are lowercased.
    """
    raw = (url or "").strip()
    if not raw:
        raise IdentityError("relay URL is required")
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in {"ws", "wss"}:
        raise IdentityError(
            f"unsupported relay URL scheme {parts.scheme!r}; expected ws:// or wss://"
        )
    if not parts.hostname:
        raise IdentityError(f"relay URL {raw!r} has no host")
    netloc = parts.netloc
    if parts.username or parts.password:
        raise IdentityError("relay URL must not embed credentials")
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, netloc.lower(), path, parts.query, ""))


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _coerce_grants(value: Any) -> dict[str, float | None] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, float | None] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            continue
        if item is None:
            out[key] = None
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            out[key] = float(item)
    return out


@dataclass(frozen=True)
class PairingRecord:
    """One relay's minted session token plus its handshake metadata."""

    session_token: str
    server_version: str | None = None
    paired_at: float = 0.0
    expires_at: float | None = None
    transport_hint: str | None = None
    grants: dict[str, float | None] | None = None

    def is_expired(self, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at <= (time.time() if now is None else now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_token": self.session_token,
            "server_version": self.server_version,
            "paired_at": self.paired_at,
            "expires_at": self.expires_at,
            "transport_hint": self.transport_hint,
            "grants": self.grants,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "PairingRecord":
        if not isinstance(value, dict):
            raise IdentityError("stored pairing record is not an object")
        token = value.get("session_token")
        if not isinstance(token, str) or not token:
            raise IdentityError("stored pairing record is missing session_token")
        return cls(
            session_token=token,
            server_version=(
                value["server_version"]
                if isinstance(value.get("server_version"), str)
                else None
            ),
            paired_at=_coerce_float(value.get("paired_at")) or 0.0,
            expires_at=_coerce_float(value.get("expires_at")),
            transport_hint=(
                value["transport_hint"]
                if isinstance(value.get("transport_hint"), str)
                else None
            ),
            grants=_coerce_grants(value.get("grants")),
        )


@dataclass
class ClientIdentity:
    """Durable identity for this client installation.

    ``device_id`` is a random per-install UUIDv4 (the relay's ownership key).
    Hostnames are display metadata and may change between runs.
    """

    device_id: str
    device_name: str
    path: Path
    active_url: str | None = None
    hosts: dict[str, PairingRecord] = field(default_factory=dict)
    _dirty: bool = field(default=False, repr=False, compare=False)

    # ── Loading ──────────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "ClientIdentity":
        """Load the store, creating an in-memory identity if absent.

        A corrupt store raises :class:`IdentityError` rather than silently
        discarding tokens; the caller can move the file aside and re-pair.
        """
        store = Path(path).expanduser() if path is not None else default_store_path()
        if not store.exists():
            return cls(
                device_id=_new_device_id(),
                device_name=_default_device_name(),
                path=store,
                _dirty=True,
            )
        try:
            raw = json.loads(store.read_text(encoding="utf-8"))
        except OSError as exc:
            raise IdentityError(f"cannot read client store {store}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise IdentityError(
                f"client store {store} is not valid JSON ({exc}); move it aside and re-pair"
            ) from exc
        if not isinstance(raw, dict):
            raise IdentityError(f"client store {store} is not a JSON object")

        device_id = raw.get("device_id")
        if not isinstance(device_id, str) or not _DEVICE_ID_RE.match(device_id.lower()):
            device_id = _new_device_id()
        else:
            device_id = device_id.lower()

        device_name = raw.get("device_name")
        if not isinstance(device_name, str) or not device_name.strip():
            device_name = _default_device_name()

        hosts: dict[str, PairingRecord] = {}
        raw_hosts = raw.get("hosts")
        if isinstance(raw_hosts, dict):
            for url, record in raw_hosts.items():
                if not isinstance(url, str):
                    continue
                try:
                    hosts[normalize_relay_url(url)] = PairingRecord.from_dict(record)
                except IdentityError as exc:
                    logger.warning("Ignoring invalid pairing record for %s: %s", url, exc)

        active_url = raw.get("active_url")
        if isinstance(active_url, str):
            try:
                active_url = normalize_relay_url(active_url)
            except IdentityError:
                active_url = None
        else:
            active_url = None

        return cls(
            device_id=device_id,
            device_name=device_name.strip(),
            path=store,
            active_url=active_url,
            hosts=hosts,
            _dirty=False,
        )

    # ── Accessors ────────────────────────────────────────────────────────

    def device_metadata(self) -> dict[str, str]:
        """Fields the relay accepts on ``system/auth`` and expects on
        ``desktop.status``."""
        name = self.device_name or _default_device_name()
        return {
            "device_id": self.device_id,
            "device_name": name,
            "device_hostname": name,
            "device_platform": platform_label(),
            "client_surface": "python",
            "device_form_factor": "desktop",
        }

    def record_for(self, url: str) -> PairingRecord | None:
        try:
            key = normalize_relay_url(url)
        except IdentityError:
            return None
        return self.hosts.get(key)

    def token_for(self, url: str) -> str | None:
        record = self.record_for(url)
        return record.session_token if record is not None else None

    def active_record(self) -> tuple[str, PairingRecord] | None:
        if self.active_url and self.active_url in self.hosts:
            return self.active_url, self.hosts[self.active_url]
        if len(self.hosts) == 1:
            url, record = next(iter(self.hosts.items()))
            return url, record
        return None

    # ── Mutation / persistence ───────────────────────────────────────────

    def remember_pairing(
        self,
        url: str,
        *,
        session_token: str,
        server_version: str | None = None,
        expires_at: float | None = None,
        transport_hint: str | None = None,
        grants: dict[str, float | None] | None = None,
        device_name: str | None = None,
    ) -> PairingRecord:
        key = normalize_relay_url(url)
        record = PairingRecord(
            session_token=session_token,
            server_version=server_version,
            paired_at=time.time(),
            expires_at=expires_at,
            transport_hint=transport_hint,
            grants=grants,
        )
        self.hosts[key] = record
        self.active_url = key
        if device_name and device_name.strip():
            self.device_name = device_name.strip()
        self._dirty = True
        return record

    def set_device_name(self, name: str) -> None:
        cleaned = name.strip()
        if cleaned and cleaned != self.device_name:
            self.device_name = cleaned
            self._dirty = True

    def forget(self, url: str) -> None:
        key = normalize_relay_url(url)
        self.hosts.pop(key, None)
        if self.active_url == key:
            self.active_url = next(iter(self.hosts), None)
        self._dirty = True

    def save(self) -> Path:
        """Atomically persist the store with owner-only permissions."""
        payload = {
            "version": CLIENT_STORE_VERSION,
            "active_url": self.active_url,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "hosts": {url: record.to_dict() for url, record in self.hosts.items()},
        }
        target = Path(self.path)
        directory = target.parent
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)
        except OSError as exc:
            raise IdentityError(f"cannot create client store directory {directory}: {exc}") from exc

        fd: int | None = None
        tmp_path: str | None = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=str(directory)
            )
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                fd = None
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, target)
            tmp_path = None
            os.chmod(target, 0o600)
        except OSError as exc:
            raise IdentityError(f"cannot write client store {target}: {exc}") from exc
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        self._dirty = False
        return target

    def save_if_dirty(self) -> Path | None:
        if not self._dirty:
            return None
        return self.save()


def platform_label() -> str:
    import platform

    name = platform.system() or "unknown"
    release = platform.release()
    return f"{name} {release}".strip()


def hostname() -> str:
    import socket

    try:
        return socket.gethostname().strip() or "python-client"
    except OSError:
        return "python-client"


def _default_device_name() -> str:
    return hostname()


def _new_device_id() -> str:
    return str(uuid.uuid4()).lower()
