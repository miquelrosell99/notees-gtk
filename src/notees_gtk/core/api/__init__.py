"""REST API client layer for the Notees GTK client."""

from notees_gtk.core.api.client import (
    MAX_BATCH_SIZE,
    RELAY_V2_BASE,
    AuthResult,
    NoteesClient,
    RelayStats,
    SnapshotMeta,
    SnapshotUploadResult,
    TwoFactorRequired,
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

__all__ = [
    "MAX_BATCH_SIZE",
    "RELAY_V2_BASE",
    "ApiError",
    "AuthResult",
    "AuthenticationError",
    "ForbiddenError",
    "NetworkError",
    "NoteesClient",
    "QuarantinedError",
    "RateLimitedError",
    "RelayStats",
    "ServerError",
    "SnapshotMeta",
    "SnapshotUploadResult",
    "TwoFactorRequired",
    "WorkspaceRef",
    "classify_response",
]
