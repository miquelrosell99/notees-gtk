"""REST API client layer for the Notees GTK client."""

from notees_gtk.core.api.client import (
    MAX_BATCH_SIZE,
    AuthResult,
    NoteesClient,
    SnapshotMeta,
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
    "ApiError",
    "AuthResult",
    "AuthenticationError",
    "ForbiddenError",
    "NetworkError",
    "NoteesClient",
    "QuarantinedError",
    "RateLimitedError",
    "ServerError",
    "SnapshotMeta",
    "TwoFactorRequired",
    "WorkspaceRef",
    "classify_response",
]
