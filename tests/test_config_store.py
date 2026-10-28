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
    assert fallback.api_key is None


def test_save_config_persists_api_key(config_home: Path) -> None:
    config = ClientConfig(
        server_url="http://server:8001",
        data_dir=config_home / "data",
        api_key="secret-api-key",
    )
    config_store.save_config(config)
    assert config_store.load_config().api_key == "secret-api-key"


def test_api_key_round_trip_via_helpers(config_home: Path) -> None:
    """save_api_key/load_api_key persist alongside the rest of the config."""
    config = ClientConfig(server_url="http://server:8001", data_dir=config_home / "data", token="t")
    config_store.save_config(config)
    config_store.save_api_key("secret-api-key")
    assert config_store.load_api_key() == "secret-api-key"
    # The rest of the config survives the key write.
    loaded = config_store.load_config()
    assert loaded.server_url == "http://server:8001"
    assert loaded.token == "t"
    assert loaded.api_key == "secret-api-key"


def test_api_key_file_is_mode_600(config_home: Path) -> None:
    """The API key is a secret: the config file must stay owner-only."""
    config_store.save_api_key("secret-api-key")
    path = config_home / config_store.APP_DIR_NAME / "config.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8"))["api_key"] == "secret-api-key"


def test_load_api_key_none_when_unconfigured(config_home: Path) -> None:
    assert config_store.load_api_key() is None


def test_cards_algorithm_round_trip(config_home: Path) -> None:
    config_store.save_cards_algorithm("sm2")
    assert config_store.load_cards_algorithm() == "sm2"
    config_store.save_cards_algorithm("fsrs")
    assert config_store.load_cards_algorithm() == "fsrs"


def test_cards_algorithm_missing_key_is_the_fsrs_default(config_home: Path) -> None:
    assert config_store.load_cards_algorithm() == "fsrs"


def test_cards_algorithm_garbled_key_is_the_fsrs_default(config_home: Path) -> None:
    """The honest offline answer: a garbled cache never half-reads."""
    path = config_home / config_store.APP_DIR_NAME / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"cards_algorithm": "supermemo"}\n', encoding="utf-8")
    assert config_store.load_cards_algorithm() == "fsrs"


def test_cards_algorithm_rejects_a_non_grammar_value(config_home: Path) -> None:
    with pytest.raises(ValueError, match="sm2"):
        config_store.save_cards_algorithm("leitner")  # type: ignore[arg-type]
    assert config_store.load_cards_algorithm() == "fsrs"


def test_cards_algorithm_survives_other_config_writes(config_home: Path) -> None:
    """The scalar persists beside the actor id through unrelated writes."""
    config_store.save_cards_algorithm("sm2")
    config_store.save_actor_id("actor-uuid")
    assert config_store.load_cards_algorithm() == "sm2"
    assert config_store.load_actor_id() == "actor-uuid"
