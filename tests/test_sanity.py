"""Sanity checks for the project skeleton."""

from pathlib import Path

from notees_gtk.config import ClientConfig


def test_client_config_defaults() -> None:
    config = ClientConfig(server_url="https://example.com", data_dir=Path("/tmp/notees"))

    assert config.server_url == "https://example.com"
    assert config.data_dir == Path("/tmp/notees")
    assert config.token is None
