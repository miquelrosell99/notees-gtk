"""Configuration for the Notees GTK client."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ClientConfig:
    """Client configuration for connecting to a Notees server.

    Attributes:
        server_url: Base URL of the Notees server.
        data_dir: Local directory for client data (cache, sync state).
        token: Authentication token for the server, if any.
        api_key: Single-user API key for the v2 relay (``X-API-Key``), if any.
            Takes precedence over the login token for relay endpoints.
    """

    server_url: str
    data_dir: Path
    token: str | None = None
    api_key: str | None = None
