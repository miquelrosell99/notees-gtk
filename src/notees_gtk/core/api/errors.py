"""HTTP error taxonomy for the Notees REST API client.

Maps non-2xx HTTP responses onto typed exceptions so the sync engine can
react by class: 401/403 trigger re-auth, other 4xx quarantine the offending
operations, 429 backs off, 5xx retries, and transport failures surface as
:class:`NetworkError` with no response attached.

v2 relay endpoints (WIRE.md) answer with a machine-readable error
envelope — ``{"error": {"code", "message", "status"}}`` — whose stable codes
take priority over status-code heuristics:

- ``unauthenticated`` → :class:`AuthenticationError`
- ``forbidden`` → :class:`ForbiddenError`
- ``validation_failed`` / ``not_found`` / ``conflict`` → :class:`QuarantinedError`
  (retrying the request unchanged will fail)
- ``rate_limited`` → :class:`RateLimitedError`
- ``idempotency_replay`` → :class:`ServerError` (transient: the original
  request is still in flight; the retry backoff makes the dedupe harmless)

Legacy FastAPI ``{"detail": ...}`` bodies (login/2FA, older routers) are
still parsed, so one client speaks to both generations.
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
        code: Stable machine code from the v2 error envelope (``None`` when
            the server answered with a legacy body or no JSON).
    """

    def __init__(self, detail: str, *, status: int | None = None, code: str | None = None) -> None:
        self.status = status
        self.detail = detail
        self.code = code
        message = f"HTTP {status}: {detail}" if status is not None else detail
        super().__init__(message)


class AuthenticationError(ApiError):
    """401 / ``unauthenticated`` — credentials rejected or session expired; re-authenticate."""

    def __init__(self, detail: str, *, status: int | None = 401, code: str | None = None) -> None:
        super().__init__(detail, status=status, code=code)


class ForbiddenError(ApiError):
    """403 / ``forbidden`` — authenticated but lacking permission for the workspace/operation."""

    def __init__(self, detail: str, *, status: int | None = 403, code: str | None = None) -> None:
        super().__init__(detail, status=status, code=code)


class QuarantinedError(ApiError):
    """Other 4xx — the request itself is invalid; retrying it unchanged will fail.

    Maps to the mobile client's quarantine path: the offending operations are
    parked and surfaced, never retried blindly.
    """

    def __init__(self, detail: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(detail, status=status, code=code)


class RateLimitedError(ApiError):
    """429 / ``rate_limited`` — wait :attr:`retry_after` seconds (when advertised) before retrying."""

    def __init__(
        self,
        detail: str,
        *,
        status: int | None = 429,
        retry_after: float | None = None,
        code: str | None = None,
    ) -> None:
        self.retry_after = retry_after
        super().__init__(detail, status=status, code=code)


class ServerError(ApiError):
    """5xx — transient server fault; safe to retry with backoff."""

    def __init__(self, detail: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(detail, status=status, code=code)


class NetworkError(ApiError):
    """No response at all — DNS, TCP, TLS, or timeout failures from the transport."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail, status=None)


#: Stable v2 error codes (WIRE.md) mapped onto the exception taxonomy.
_ERROR_CODE_MAP: dict[str, type[ApiError]] = {
    "unauthenticated": AuthenticationError,
    "forbidden": ForbiddenError,
    "validation_failed": QuarantinedError,
    "not_found": QuarantinedError,
    "conflict": QuarantinedError,
    "rate_limited": RateLimitedError,
    "idempotency_replay": ServerError,
}


def _extract_error(response: httpx.Response) -> tuple[str | None, str]:
    """Pull ``(code, detail)`` out of an error response body.

    Prefers the v2 error envelope ``{"error": {"code", "message", "status"}}``;
    falls back to legacy FastAPI ``{"detail": ...}`` bodies, then to the raw
    response text.
    """
    try:
        body: Any = response.json()
    except ValueError:
        return None, response.text or f"HTTP {response.status_code}"
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            machine_code = code if isinstance(code, str) else None
            message = error.get("message")
            if isinstance(message, str) and message:
                return machine_code, message
            return machine_code, machine_code or f"HTTP {response.status_code}"
        detail = body.get("detail")
        if detail is None:
            return None, response.text or f"HTTP {response.status_code}"
        if isinstance(detail, str):
            return None, detail
        # FastAPI 422 validation errors carry a list of loc/msg/type dicts.
        return None, json.dumps(detail)
    return None, response.text or f"HTTP {response.status_code}"


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
    code, detail = _extract_error(response)
    mapped = _ERROR_CODE_MAP.get(code) if code else None
    if mapped is not None:
        if mapped is RateLimitedError:
            raise RateLimitedError(detail, status=status, retry_after=_parse_retry_after(response), code=code)
        raise mapped(detail, status=status, code=code)
    if status == 401:
        raise AuthenticationError(detail, status=status, code=code)
    if status == 403:
        raise ForbiddenError(detail, status=status, code=code)
    if status == 429:
        raise RateLimitedError(detail, status=status, retry_after=_parse_retry_after(response), code=code)
    if status < 500:
        raise QuarantinedError(detail, status=status, code=code)
    raise ServerError(detail, status=status, code=code)
