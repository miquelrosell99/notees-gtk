"""HTTP error taxonomy for the Notees REST API client.

Maps non-2xx HTTP responses onto typed exceptions so the sync engine can
react by class: 401/403 trigger re-auth, other 4xx quarantine the offending
operations, 429 backs off, 5xx retries, and transport failures surface as
:class:`NetworkError` with no response attached.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx

__all__ = [
    "ApiError",
    "AuthenticationError",
    "ForbiddenError",
    "NetworkError",
    "QuarantinedError",
    "RateLimitedError",
    "ServerError",
    "classify_response",
]


class ApiError(Exception):
    """Base class for all Notees API errors.

    Attributes:
        status: HTTP status code, or ``None`` when no response was received
            (transport-level failures such as :class:`NetworkError`).
        detail: Human-readable error detail extracted from the response body.
    """

    def __init__(self, detail: str, *, status: int | None = None) -> None:
        self.status = status
        self.detail = detail
        message = f"HTTP {status}: {detail}" if status is not None else detail
        super().__init__(message)


class AuthenticationError(ApiError):
    """401 — credentials rejected or session expired; re-authenticate."""

    def __init__(self, detail: str, *, status: int | None = 401) -> None:
        super().__init__(detail, status=status)


class ForbiddenError(ApiError):
    """403 — authenticated but lacking permission for the workspace/operation."""

    def __init__(self, detail: str, *, status: int | None = 403) -> None:
        super().__init__(detail, status=status)


class QuarantinedError(ApiError):
    """Other 4xx — the request itself is invalid; retrying it unchanged will fail.

    Maps to the mobile client's quarantine path: the offending operations are
    parked and surfaced, never retried blindly.
    """

    def __init__(self, detail: str, *, status: int | None = None) -> None:
        super().__init__(detail, status=status)


class RateLimitedError(ApiError):
    """429 — rate limited; wait :attr:`retry_after` seconds (when advertised) before retrying."""

    def __init__(self, detail: str, *, status: int | None = 429, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(detail, status=status)


class ServerError(ApiError):
    """5xx — transient server fault; safe to retry with backoff."""

    def __init__(self, detail: str, *, status: int | None = None) -> None:
        super().__init__(detail, status=status)


class NetworkError(ApiError):
    """No response at all — DNS, TCP, TLS, or timeout failures from the transport."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail, status=None)


def _extract_detail(response: httpx.Response) -> str:
    """Pull a human-readable detail out of a FastAPI error response."""
    try:
        body: Any = response.json()
    except ValueError:
        return response.text or f"HTTP {response.status_code}"
    detail = body.get("detail") if isinstance(body, dict) else None
    if detail is None:
        return response.text or f"HTTP {response.status_code}"
    if isinstance(detail, str):
        return detail
    # FastAPI 422 validation errors carry a list of loc/msg/type dicts.
    return json.dumps(detail)


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Parse the Retry-After header as seconds; non-numeric values yield ``None``."""
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def classify_response(response: httpx.Response) -> None:
    """Raise the matching :class:`ApiError` subclass for a non-2xx response.

    Args:
        response: The HTTP response to classify.

    Raises:
        ApiError: Always, when the status is not 2xx. Returns ``None``
            silently for successful responses.
    """
    status = response.status_code
    if 200 <= status < 300:
        return
    detail = _extract_detail(response)
    if status == 401:
        raise AuthenticationError(detail, status=status)
    if status == 403:
        raise ForbiddenError(detail, status=status)
    if status == 429:
        raise RateLimitedError(detail, status=status, retry_after=_parse_retry_after(response))
    if status < 500:
        raise QuarantinedError(detail, status=status)
    raise ServerError(detail, status=status)
