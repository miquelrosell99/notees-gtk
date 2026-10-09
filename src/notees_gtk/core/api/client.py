"""Synchronous REST client for a Notees server (relay protocol v2).

Covers the endpoints the sync engine drives: v2 relay endpoints under
``/api/relay/v2`` (WIRE.md: batch submit, catch-up, snapshot probe/download/
upload, stats) plus the workspace listing and the login path (kept for
servers that still issue bearer sessions; the v2 relay authenticates with
a single-user API key, ``X-API-Key``). All relay request/response bodies are
camelCase JSON; envelopes travel inside them per the protocol models.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from notees_gtk.core.api.errors import NetworkError, classify_response
from notees_gtk.core.protocol.clock import Hlc
from notees_gtk.core.protocol.models import (
    BatchRequest,
    BatchResponse,
    CatchUpPaginatedResponse,
    CatchUpRequest,
    RelayEnvelope,
    check_payload_size,
)

__all__ = [
    "MAX_BATCH_SIZE",
    "AuthResult",
    "NoteesClient",
    "RelayStats",
    "SnapshotMeta",
    "SnapshotUploadResult",
    "WorkspaceRef",
]

#: Maximum envelopes per ``POST /batch`` submission (WIRE.md).
MAX_BATCH_SIZE = 1000

#: Base path of the v2 relay API (WIRE.md).
RELAY_V2_BASE = "/api/relay/v2"


class AuthResult(BaseModel):
    """Successful authentication: bearer token plus the server user record.

    Mirrors the server's login response — ``token``, ``expiresAt`` (epoch
    millis), ``user``, and the password-derived ``kdf`` record (kept on the
    model; consumers that don't need it ignore it). Reads stay additive-tolerant
    (the server may extend the response); missing ``token``/``expiresAt`` fail
    validation, which is what catches a drifted contract.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    token: str
    expires_at: int
    user: dict[str, Any]
    kdf: dict[str, Any] | None = None


class WorkspaceRef(BaseModel):
    """Workspace membership entry from ``GET /api/workspaces``.

    The server sends ``id``, ``name`` (null when unnamed), ``role``,
    ``createdAt``, and the relay ``envelopeCount`` / ``latestSeq`` stats; the
    ref carries what the chrome addresses workspaces by.
    """

    id: str
    name: str | None = None


class SnapshotMeta(BaseModel):
    """Snapshot metadata from ``GET /api/relay/v2/snapshot`` (WIRE.md).

    Wire shape: ``snapshotId``, ``hlc``, ``hasSnapshot``, ``restoreEpoch``,
    ``upToSeq`` (null when no snapshot exists). ``workspaceId`` deliberately
    does not ride along — the client asked for exactly one workspace.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    snapshot_id: str
    hlc: Hlc
    has_snapshot: bool
    restore_epoch: int
    up_to_seq: int | None


class SnapshotUploadResult(BaseModel):
    """Acknowledgement of ``PUT /snapshot/data`` (201): the server's checkpoint."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    snapshot_id: str
    workspace_id: str
    up_to_hlc: Hlc
    up_to_seq: int


class RelayStats(BaseModel):
    """Relay statistics from ``GET /api/relay/v2/stats`` (WIRE.md)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    envelope_count: int
    snapshot_count: int
    compacted_operation_count: int
    max_hlc: Hlc
    restore_epoch: int
    latest_snapshot_hlc: Hlc | None


class NoteesClient:
    """Synchronous REST client for a Notees server.

    Args:
        base_url: Server base URL; a trailing slash is stripped.
        token: Optional bearer token applied to every request (login path).
        api_key: Optional single-user API key (``X-API-Key``) applied to every
            request; this is the v2 relay auth mode and works without login.
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
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._api_key = api_key
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if api_key:
            headers["X-API-Key"] = api_key
        self._client = httpx.Client(base_url=self.base_url, transport=transport, timeout=timeout, headers=headers)

    # ------------------------------------------------------------------- auth

    def login(self, email: str, password: str) -> AuthResult:
        """Log in and store the issued bearer token for subsequent requests.

        The v2 relay does not need this: an API key passed to the constructor
        (or persisted via ``config_store.save_api_key``) authenticates every
        relay endpoint directly.

        The login body is exactly ``{email, password}``: the server schema is
        strict (extra keys are rejected with 422), and session lifetime is
        server-owned — 30-day sessions with sliding renewal, with no
        remember-me or 2FA fields (the Flutter client documents the same
        contract).

        Args:
            email: Account email.
            password: Account password.

        Returns:
            The validated authentication result.

        Raises:
            ApiError: The login request failed.
        """
        data = self._post_json("/api/auth/login", {"email": email, "password": password})
        result = AuthResult.model_validate(data)
        self._token = result.token
        self._client.headers["Authorization"] = f"Bearer {result.token}"
        return result
    def ws_token(self) -> str | None:
        """Return the credential for the WebSocket ``?token=`` parameter.

        Prefers the API key (v2 relay auth); falls back to the bearer token
        from login. ``None`` when the client is unauthenticated.
        """
        return self._api_key or self._token

    # -------------------------------------------------------------- workspaces

    def list_workspaces(self) -> list[WorkspaceRef]:
        """List the current user's workspaces.

        ``GET /api/workspaces`` answers ``{ "workspaces": [...] }`` — one entry
        per membership with ``id``, ``name`` (null when unnamed), ``role``,
        ``createdAt``, and the relay ``envelopeCount`` / ``latestSeq`` stats.
        """
        data = self._get_json("/api/workspaces")
        return [WorkspaceRef.model_validate(item) for item in data["workspaces"]]
    # ------------------------------------------------------------------- relay

    def submit_batch(self, envelopes: list[RelayEnvelope]) -> list[str]:
        """Submit a batch of envelopes to the relay, returning the server-saved ids.

        Duplicate envelope ids are silently ignored server-side, so
        ``savedIds`` may omit ids that were sent (retry-safe). Client-side
        pre-checks (WIRE.md limits) raise ``ValueError`` before any request
        is made: at most :data:`MAX_BATCH_SIZE` envelopes, each payload at most
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
        data = self._post_json(f"{RELAY_V2_BASE}/batch", body)
        return list(BatchResponse.model_validate(data).saved_ids)

    def catch_up(self, workspace_id: str, after_seq: int = 0, limit: int = 1000) -> CatchUpPaginatedResponse:
        """Fetch one page of envelopes newer than the seq cursor (WIRE.md).

        Args:
            workspace_id: Workspace to catch up on.
            after_seq: Exclusive seq cursor; ``0`` fetches from the beginning.
            limit: Page size, clamped server-side to [1, 10,000].
        """
        body = CatchUpRequest(workspace_id=workspace_id, after_seq=after_seq, limit=limit).model_dump(
            mode="json", by_alias=True
        )
        data = self._post_json(f"{RELAY_V2_BASE}/catch-up", body)
        return CatchUpPaginatedResponse.model_validate(data)

    def snapshot_probe(self, workspace_id: str) -> SnapshotMeta:
        """Probe the newest snapshot's metadata without downloading it (WIRE.md)."""
        data = self._get_json(f"{RELAY_V2_BASE}/snapshot", params={"workspaceId": workspace_id})
        return SnapshotMeta.model_validate(data)

    def snapshot_data(self, workspace_id: str) -> bytes:
        """Download the newest snapshot blob as raw bytes (WIRE.md)."""
        response = self._send("GET", f"{RELAY_V2_BASE}/snapshot/data", params={"workspaceId": workspace_id})
        return response.content

    def upload_snapshot(self, workspace_id: str, data: bytes, *, hlc: Hlc) -> SnapshotUploadResult:
        """Upload a client-produced snapshot blob (WIRE.md).

        Args:
            workspace_id: Workspace the snapshot belongs to.
            data: Raw snapshot bytes (a serialized derived-state SQLite db).
            hlc: The HLC up to which the snapshot covers the operation log;
                sent as the ``physical``/``logical`` query parameters.

        Returns:
            The server's acknowledgement (snapshot id + covered seq).
        """
        response = self._send(
            "PUT",
            f"{RELAY_V2_BASE}/snapshot/data",
            params={"workspaceId": workspace_id, "physical": hlc.physical, "logical": hlc.logical},
            content=data,
        )
        return SnapshotUploadResult.model_validate(response.json())

    def stats(self, workspace_id: str) -> RelayStats:
        """Return the relay stats document for a workspace (WIRE.md)."""
        data = self._get_json(f"{RELAY_V2_BASE}/stats", params={"workspaceId": workspace_id})
        return RelayStats.model_validate(data)

    # ----------------------------------------------------------------- plumbing

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
