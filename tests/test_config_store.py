"""Tests for the pure config persistence helpers (``notees_gtk.ui.config_store``)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from notees_gtk.config import ClientConfig
from notees_gtk.ui import config_store


@pytest.fixture
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the config dir on a tmp XDG_CONFIG_HOME."""
    root = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root))
    return root


def test_save_config_writes_mode_600(config_home: Path) -> None:
    """The config file carries the bearer token and must not be world-readable."""
    config = ClientConfig(
        server_url="http://localhost:8001",
        data_dir=config_home / "data",
        token="secret-token",
    )
    config_store.save_config(config)
    path = config_home / config_store.APP_DIR_NAME / "config.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8"))["token"] == "secret-token"


def test_config_round_trip_preserves_actor_id(config_home: Path) -> None:
    config = ClientConfig(server_url="http://server:8001", data_dir=config_home / "data", token="t")
    config_store.save_config(config)
    config_store.save_actor_id("actor-uuid")
    loaded = config_store.load_config()
    assert loaded.server_url == "http://server:8001"
    assert loaded.token == "t"
    assert loaded.data_dir == config_home / "data"
    assert config_store.load_actor_id() == "actor-uuid"


def test_load_config_defaults_when_missing(config_home: Path) -> None:
    fallback = config_store.load_config()
    assert fallback.server_url == config_store.DEFAULT_SERVER_URL
    assert fallback.token is None
