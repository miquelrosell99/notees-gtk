"""REST API client layer for the Notees GTK client."""

from notees_gtk.core.api.client import (
    MAX_BATCH_SIZE,
    RELAY_V2_BASE,
    AuthResult,
    NoteesClient,
    RelayStats,
    SnapshotMeta,
    SnapshotUploadResult,
    WorkspaceRef,
)
from notees_gtk.core.api.errors import (
    ApiError,
    AuthenticationError,
    ForbiddenError,
    NetworkError,
    QuarantinedError,
    RateLimitedError,
    ServerError,
    classify_response,
)
from notees_gtk.core.api.ws import (
    DEFAULT_RECONNECT_DELAYS,
    HelloInfo,
    ProtocolVersionError,
    RealtimeClient,
    RealtimeProtocolError,
    build_ws_url,
)

__all__ = [
    "MAX_BATCH_SIZE",
    "RELAY_V2_BASE",
    "ApiError",
    "AuthResult",
    "AuthenticationError",
    "DEFAULT_RECONNECT_DELAYS",
    "ForbiddenError",
    "HelloInfo",
    "NetworkError",
    "NoteesClient",
    "ProtocolVersionError",
    "QuarantinedError",
    "RateLimitedError",
    "RealtimeClient",
    "RealtimeProtocolError",
    "RelayStats",
    "ServerError",
    "SnapshotMeta",
    "SnapshotUploadResult",
    "WorkspaceRef",
    "build_ws_url",
    "classify_response",
]
