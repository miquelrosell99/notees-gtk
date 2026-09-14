"""Synchronous REST client for a Notees server.

Covers the endpoints the sync engine drives: authentication (with the 2FA
second step), workspace listing, and the relay endpoints from ``protocol/SPEC.md``
§4 (batch submit, catch-up, snapshot probe/download, stats). All paths are
relative to the server base URL; envelopes travel camelCase per the protocol
models, everything else snake_case per the SPEC.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

from notees_gtk.core.api.errors import NetworkError, classify_response
from notees_gtk.core.protocol.clock import Hlc
from notees_gtk.core.protocol.models import (
    BatchRequest,
    CatchUpPaginatedResponse,
    CatchUpRequest,
    RelayEnvelope,
    check_payload_size,
)

__all__ = [
    "MAX_BATCH_SIZE",
    "AuthResult",
    "NoteesClient",
    "SnapshotMeta",
    "TwoFactorRequired",
    "WorkspaceRef",
]

#: Maximum envelopes per ``POST /batch`` submission (SPEC §6).
MAX_BATCH_SIZE = 1000

#: 2FA-gated login: the password step returns this pre-auth token + purpose instead of tokens.
_TWO_FA_GATE_FIELDS = ("preauth_token", "purpose")


class AuthResult(BaseModel):
    """Successful authentication: bearer token plus the server user record."""

    access_token: str
    token_type: str
    user: dict[str, Any]


class TwoFactorRequired(Exception):  # noqa: N818 — name fixed by the task brief (control-flow signal, not an error)
    """Login requires a second factor; exchange the pre-auth token with a TOTP code."""

    def __init__(self, preauth_token: str, purpose: str) -> None:
        self.preauth_token = preauth_token
        self.purpose = purpose
        super().__init__(f"Two-factor authentication required (purpose={purpose!r})")


class WorkspaceRef(BaseModel):
    """Workspace summary entry from ``GET /api/workspaces/`` (``PaginatedResponse.items``)."""

    uuid: str
    name: str
    is_active: bool


class SnapshotMeta(BaseModel):
    """Snapshot metadata from ``GET /api/relay/snapshot`` (SPEC §4.3)."""

    snapshot_id: str
    workspace_id: str
    hlc: Hlc
    has_snapshot: bool
    restore_epoch: int
    up_to_seq: int | None = None


class NoteesClient:
    """Synchronous REST client for a Notees server.

    Args:
        base_url: Server base URL; a trailing slash is stripped.
        token: Optional bearer token applied to every request.
        transport: Optional httpx transport (e.g. ``httpx.MockTransport`` in tests).
        timeout: Per-request timeout in seconds.

    Attributes:
        base_url: The normalized base URL requests are sent against.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.Client(base_url=self.base_url, transport=transport, timeout=timeout, headers=headers)

    def login(self, email: str, password: str, *, remember_me: bool = True, totp: str | None = None) -> AuthResult:
        """Log in and store the issued bearer token for subsequent requests.

        When the account has 2FA enabled the password step answers with a
        pre-auth token instead of session tokens: raises
        :class:`TwoFactorRequired` unless ``totp`` is given, in which case the
        code is exchanged at ``/api/auth/2fa/verify`` and the resulting tokens
        are used as for a plain login.

        Args:
            email: Account email.
            password: Account password.
            remember_me: Passed through to the login endpoint.
            totp: TOTP (or backup) code for the 2FA second step.

        Returns:
            The validated authentication result.

        Raises:
            TwoFactorRequired: 2FA is enabled and no ``totp`` code was supplied.
            ApiError: The login (or verify) request failed.
        """
        data = self._post_json("/api/auth/login", {"email": email, "password": password, "remember_me": remember_me})
        if all(field in data for field in _TWO_FA_GATE_FIELDS):
            if totp is None:
                raise TwoFactorRequired(preauth_token=data["preauth_token"], purpose=data["purpose"])
            data = self._post_json("/api/auth/2fa/verify", {"preauth_token": data["preauth_token"], "code": totp})
        result = AuthResult.model_validate(data)
        self._client.headers["Authorization"] = f"Bearer {result.access_token}"
        return result

    def list_workspaces(self) -> list[WorkspaceRef]:
        """List the current user's workspaces.

        The trailing slash is load-bearing: without it the server SPA fallback
        answers 404 before Starlette's redirect can run. Workspaces arrive
        wrapped in a ``PaginatedResponse`` and are unwrapped from ``items``.
        """
        data = self._get_json("/api/workspaces/")
        return [WorkspaceRef.model_validate(item) for item in data["items"]]

    def submit_batch(self, envelopes: list[RelayEnvelope]) -> list[str]:
        """Submit a batch of envelopes to the relay, returning the server-saved ids.

        Duplicate envelope ids are silently ignored server-side, so
        ``saved_ids`` may omit ids that were sent (retry-safe). Client-side
        pre-checks (SPEC §6 limits) raise ``ValueError`` before any request is
        made: at most :data:`MAX_BATCH_SIZE` envelopes, each payload at most
        :data:`MAX_ENVELOPE_SIZE_BYTES` bytes serialized.

        Args:
            envelopes: Envelopes to submit.

        Returns:
            The envelope ids the server stored for this batch.

        Raises:
            ValueError: A client-side pre-check failed.
            ApiError: The server rejected the batch.
        """
        if len(envelopes) > MAX_BATCH_SIZE:
            raise ValueError(f"Batch of {len(envelopes)} envelopes exceeds MAX_BATCH_SIZE ({MAX_BATCH_SIZE})")
        for envelope in envelopes:
            check_payload_size(envelope.payload)
        body = BatchRequest(envelopes=envelopes).model_dump(mode="json", by_alias=True)
        data = self._post_json("/api/relay/batch", body)
        return list(data["saved_ids"])

    def catch_up(self, workspace_id: str, after_seq: int = 0, limit: int = 1000) -> CatchUpPaginatedResponse:
        """Fetch one page of envelopes newer than the seq cursor (SPEC §4.2).

        Args:
            workspace_id: Workspace to catch up on.
            after_seq: Exclusive seq cursor; ``0`` fetches from the beginning.
            limit: Page size, clamped server-side to [1, 10,000].
        """
        body = CatchUpRequest(workspace_id=workspace_id, after_seq=after_seq, limit=limit).model_dump(mode="json")
        data = self._post_json("/api/relay/catch-up", body)
        return CatchUpPaginatedResponse.model_validate(data)

    def snapshot_probe(self, workspace_id: str) -> SnapshotMeta:
        """Probe the newest snapshot's metadata without downloading it (SPEC §4.3)."""
        data = self._get_json("/api/relay/snapshot", params={"workspace_id": workspace_id})
        return SnapshotMeta.model_validate(data)

    def snapshot_data(self, workspace_id: str) -> bytes:
        """Download the newest snapshot blob as raw bytes (SPEC §4.3.1)."""
        response = self._send("GET", "/api/relay/snapshot/data", params={"workspace_id": workspace_id})
        return response.content

    def stats(self, workspace_id: str) -> dict[str, Any]:
        """Return the relay stats document for a workspace (SPEC §4.6)."""
        data = self._get_json("/api/relay/stats", params={"workspace_id": workspace_id})
        return dict(data)

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Send one request, translating transport failures and error statuses."""
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise NetworkError(str(exc)) from exc
        classify_response(response)
        return response

    def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return self._send("GET", path, params=params).json()

    def _post_json(self, path: str, body: dict[str, Any]) -> Any:
        return self._send("POST", path, json=body).json()
