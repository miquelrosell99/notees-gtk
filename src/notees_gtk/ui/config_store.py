"""Persistence for :class:`ClientConfig` and the per-install device id.

Simple JSON under the XDG config directory (``~/.config/notees-gtk/`` by
default) — no new dependencies. Pure module (no GTK imports) so it stays
importable and testable on headless machines.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from notees_gtk.config import ClientConfig
from notees_gtk.core.protocol.ids import new_uuid7

__all__ = [
    "APP_DIR_NAME",
    "DEFAULT_SERVER_URL",
    "GTK_ACTOR_ID",
    "config_dir",
    "data_dir",
    "default_config",
    "ensure_device_id",
    "load_actor_id",
    "load_api_key",
    "load_config",
    "save_actor_id",
    "save_api_key",
    "save_config",
]

#: Directory name used under ``$XDG_CONFIG_HOME`` / ``$XDG_DATA_HOME``.
APP_DIR_NAME = "notees-gtk"

#: Default server offered by the login form.
DEFAULT_SERVER_URL = "http://localhost:8001"

#: Envelope actor id for this client — the fixed system-actor block
#: (``01920000-0000-7000-8000-…``): a1 is the web frontend's default actor,
#: a2..a5 are server-side migration actors; a6 is the GTK client.
GTK_ACTOR_ID = "01920000-0000-7000-8000-0000000000a6"

_CONFIG_FILE_NAME = "config.json"
_DEVICE_FILE_NAME = "device_id"


def config_dir() -> Path:
    """Return the client config directory, honoring ``XDG_CONFIG_HOME``."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / APP_DIR_NAME


def data_dir() -> Path:
    """Return the default client data directory, honoring ``XDG_DATA_HOME``."""
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / APP_DIR_NAME


def default_config() -> ClientConfig:
    """Return the config used when nothing has been persisted yet."""
    return ClientConfig(server_url=DEFAULT_SERVER_URL, data_dir=data_dir())


def _read_raw() -> dict[str, Any]:
    """Read the config JSON object; ``{}`` when missing or malformed."""
    try:
        raw = json.loads((config_dir() / _CONFIG_FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_raw(payload: dict[str, Any]) -> None:
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / _CONFIG_FILE_NAME
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    # The file carries the bearer token — never leave it group/world-readable.
    os.chmod(path, 0o600)


def load_config() -> ClientConfig:
    """Load the persisted config, falling back to defaults when unreadable."""
    raw = _read_raw()
    fallback = default_config()
    server_url = raw.get("server_url")
    token = raw.get("token")
    api_key = raw.get("api_key")
    stored_data_dir = raw.get("data_dir")
    return ClientConfig(
        server_url=server_url if isinstance(server_url, str) and server_url else fallback.server_url,
        data_dir=Path(stored_data_dir) if isinstance(stored_data_dir, str) and stored_data_dir else fallback.data_dir,
        token=token if isinstance(token, str) and token else None,
        api_key=api_key if isinstance(api_key, str) and api_key else None,
    )


def save_config(config: ClientConfig) -> None:
    """Persist ``config``, preserving keys written by other helpers."""
    raw = _read_raw()
    raw.update(
        {
            "server_url": config.server_url,
            "data_dir": str(config.data_dir),
            "token": config.token,
            "api_key": config.api_key,
        }
    )
    _write_raw(raw)


def save_actor_id(actor_id: str) -> None:
    """Persist the envelope ``actor_id`` this install stamps envelopes with."""
    raw = _read_raw()
    raw["actor_id"] = actor_id
    _write_raw(raw)


def load_actor_id() -> str | None:
    """Return the persisted actor id, or ``None`` when unknown."""
    value = _read_raw().get("actor_id")
    return value if isinstance(value, str) and value else None


def save_api_key(api_key: str) -> None:
    """Persist the single-user API key used for v2 relay auth (``X-API-Key``).

    The config file is written owner-only (0600) by :func:`_write_raw`; the
    key is a secret and must never leave that protection.
    """
    raw = _read_raw()
    raw["api_key"] = api_key
    _write_raw(raw)


def load_api_key() -> str | None:
    """Return the persisted API key, or ``None`` when unconfigured."""
    value = _read_raw().get("api_key")
    return value if isinstance(value, str) and value else None


def ensure_device_id() -> str:
    """Return the stable per-install device id, creating it on first use.

    The HLC clock is bound to a device identifier; persisting one per install
    keeps causality tracking meaningful across restarts.
    """
    path = config_dir() / _DEVICE_FILE_NAME
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if existing:
        return existing
    value = new_uuid7()
    config_dir().mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    return value
