"""Synchronous REST client for a Notees server (relay protocol v2).

Covers the endpoints the sync engine drives: v2 relay endpoints under
``/api/relay/v2`` (WIRE.md: batch submit, catch-up, snapshot probe/download/
upload, stats) plus the object/assets API (CAS upload/download/info with the
plan-05 encrypted-upload fields), the workspace listing and key routes, and
the login path (kept for servers that still issue bearer sessions; the v2
relay authenticates with a single-user API key, ``X-API-Key``). All relay
request/response bodies are camelCase JSON; envelopes travel inside them per
the protocol models.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from notees_gtk.core.api.errors import NetworkError, QuarantinedError, classify_response
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
    "AssetInfo",
    "AssetUploadResult",
    "AuthResult",
    "CardsAlgorithm",
    "DEFAULT_CARDS_ALGORITHM",
    "NoteesClient",
    "RelayStats",
    "SnapshotMeta",
    "SnapshotUploadResult",
    "UserPrefs",
    "WorkspaceRef",
    "normalize_user_prefs",
]

#: Maximum envelopes per ``POST /batch`` submission (WIRE.md).
MAX_BATCH_SIZE = 1000

#: Base path of the v2 relay API (WIRE.md).
RELAY_V2_BASE = "/api/relay/v2"

#: The spaced-repetition scheduler for the memorization-cards surface
#: (``prefs.ts`` ``CardsAlgorithm`` parity).
CardsAlgorithm = Literal["sm2", "fsrs"]

#: The algorithm when neither the prefs store nor the device cache carried
#: one (``prefs.ts`` ``DEFAULT_CARDS_ALGORITHM`` parity).
DEFAULT_CARDS_ALGORITHM: CardsAlgorithm = "fsrs"


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
    ``createdAt``, the relay ``envelopeCount`` / ``latestSeq`` stats, and
    ``encrypted`` (a workspace_key row exists — the workspace is
    client-encrypted); the ref carries what the chrome addresses workspaces
    by.
    """

    id: str
    name: str | None = None
    encrypted: bool = False


class SnapshotMeta(BaseModel):
    """Snapshot metadata from ``GET /api/relay/v2/snapshot`` (WIRE.md).

    Wire shape: ``snapshotId``, ``hlc``, ``hasSnapshot``, ``restoreEpoch``,
    ``upToSeq`` (null when no snapshot exists), ``sealed``. ``workspaceId``
    deliberately does not ride along — the client asked for exactly one
    workspace. ``sealed`` declares what the blob IS: ``True`` = a sealed
    image (a keyed client sealed it); ``False`` = a plaintext derived store
    (a keyless client upload, or the server's own self-heal snapshot) — the
    restore contract in :meth:`SyncEngine.maybe_restore_snapshot` keys off it.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    snapshot_id: str
    hlc: Hlc
    has_snapshot: bool
    restore_epoch: int
    up_to_seq: int | None
    sealed: bool = False


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


class AssetInfo(BaseModel):
    """Asset metadata from ``GET /api/assets/:id/info``.

    Wire shape: ``assetId``, ``hash``, ``mimeType``, ``size``, ``originalName``,
    ``refs`` (the CAS ref count), and — plan 05 — ``iv``: the b64u 12-byte GCM
    nonce of a sealed upload, ``None`` for plaintext assets (server metadata,
    never op-log state). The mime type of a sealed asset is the DECLARED type
    (sniffing is skipped on encrypted uploads).
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    asset_id: str
    hash: str
    mime_type: str
    size: int
    original_name: str
    refs: int
    iv: str | None = None


class AssetUploadResult(BaseModel):
    """Acknowledgement of ``POST /api/assets`` (201).

    Carries the same metadata as :class:`AssetInfo` plus the optional
    ``objectId`` when the upload attached to an object, and ``iv`` (plan 05):
    the b64u nonce just stored as asset metadata on an encrypted upload,
    ``None`` on a plaintext one.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    asset_id: str
    hash: str
    mime_type: str
    size: int
    original_name: str
    refs: int
    iv: str | None = None
    object_id: str | None = None


def _as_id_list(input_value: Any) -> tuple[str, ...]:
    """The prefs string-list filter (``prefs.ts`` ``normalizeUserPrefs``
    ``asIds`` port): a list keeps its string entries, anything else is empty."""
    if not isinstance(input_value, list):
        return ()
    return tuple(entry for entry in input_value if isinstance(entry, str))


@dataclass(frozen=True)
class UserPrefs:
    """Per-user UI prefs (the ``prefs.ts`` ``UserPrefs`` port): favorites /
    recents are server-side UI state, ``cards_algorithm`` is the
    memorization-cards scheduler. Tuples keep the record immutable."""

    favorites: tuple[str, ...]
    recents: tuple[str, ...]
    cards_algorithm: CardsAlgorithm


def normalize_user_prefs(body: Any) -> UserPrefs:
    """Server body → :class:`UserPrefs` (the ``prefs.ts:123``
    ``normalizeUserPrefs`` port).

    Field grammar is tolerant exactly like the web's normalize (list filters
    keep strings, ``cardsAlgorithm`` accepts only ``"sm2"``/anything-else →
    ``"fsrs"``), but a body that is not a JSON object at all fails loud —
    the response is never half-read.

    Raises:
        ValueError: the body is not a JSON object.
    """
    if not isinstance(body, dict):
        raise ValueError(f"malformed prefs body: expected a JSON object, got {type(body).__name__}")
    raw_algorithm = body.get("cardsAlgorithm")
    cards_algorithm: CardsAlgorithm = "sm2" if raw_algorithm == "sm2" else DEFAULT_CARDS_ALGORITHM
    return UserPrefs(
        favorites=_as_id_list(body.get("favorites")),
        recents=_as_id_list(body.get("recents")),
        cards_algorithm=cards_algorithm,
    )


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
        """Return the credential for the WebSocket ``Authorization: Bearer``
        upgrade header.

        Prefers the API key (v2 relay auth); falls back to the bearer token
        from login. ``None`` when the client is unauthenticated.
        """
        return self._api_key or self._token

    def unlock(self, email: str, password: str) -> dict[str, Any]:
        """Re-derive the master key in-session (``POST /api/auth/unlock`` —
        the W2 unlock re-prompt port).

        The caller already holds a live credential (a remembered session or
        an API key), so this endpoint mints NO session — it answers ``{kdf}``
        only. It is a password oracle all the same: the SAME per-IP counter
        and per-account lockout as login, so a 429 carries the server's
        retry detail and a wrong password answers 401.

        Args:
            email: Account email.
            password: Account password.

        Returns:
            The validated ``kdf`` record body (feed to
            :class:`notees_gtk.core.protocol.crypto.KdfRecord`).

        Raises:
            AuthenticationError: 401 ``invalid_credentials``.
            RateLimitedError: 429 — the per-IP counter or the account
                lockout (``account_locked``; the detail carries the retry).
            ApiError: The request failed otherwise.
            ValueError: The server answered a malformed body.
        """
        data = self._post_json("/api/auth/unlock", {"email": email, "password": password})
        kdf = data.get("kdf") if isinstance(data, dict) else None
        if not isinstance(kdf, dict):
            raise ValueError(f"malformed unlock body: expected {{kdf}}, got {data!r}")
        return kdf

    # -------------------------------------------------------------- workspaces

    def list_workspaces(self) -> list[WorkspaceRef]:
        """List the current user's workspaces.

        ``GET /api/workspaces`` answers ``{ "workspaces": [...] }`` — one entry
        per membership with ``id``, ``name`` (null when unnamed), ``role``,
        ``createdAt``, and the relay ``envelopeCount`` / ``latestSeq`` stats.
        """
        data = self._get_json("/api/workspaces")
        return [WorkspaceRef.model_validate(item) for item in data["workspaces"]]

    # -------------------------------------------------------- workspace key

    def fetch_workspace_key(self, workspace_id: str) -> str | None:
        """Fetch the workspace's E2EE wrapped key (``GET /workspaces/:id/key``).

        The server stores ONE opaque wrapped-key string per workspace (the
        client wrapped the workspace key with its account master key; the
        server never sees either key). Keys are user custody: an API-key
        principal (operator) is 403 on this route, as is a non-member.

        Returns:
            The wrapped key string, or ``None`` on 404 — either no key is
            stored yet (the caller ensures one with :meth:`store_workspace_key`)
            or the membership does not exist (existence is never revealed).

        Raises:
            ForbiddenError: Authenticated principal may not read the key.
            ApiError: The request failed otherwise.
        """
        try:
            data = self._get_json(f"/api/workspaces/{workspace_id}/key")
        except QuarantinedError as exc:
            if exc.status == 404:
                return None
            raise
        wrapped = data.get("wrappedKey") if isinstance(data, dict) else None
        if not isinstance(wrapped, str) or not wrapped:
            raise ValueError(f"malformed workspace key body: expected {{wrappedKey}}, got {data!r}")
        return wrapped

    def store_workspace_key(self, workspace_id: str, wrapped_key: str) -> str:
        """Ensure the workspace's wrapped key (``PUT /workspaces/:id/key``).

        First write wins: the server NEVER overwrites an existing key row —
        a concurrent sibling's PUT that lands first answers 409, and the
        loser must adopt the winner's wrapped key (a blind upsert would
        permanently split the workspace's keys). This method encodes that
        rule: on 409 it re-GETs and returns the stored (winner's) key.

        Args:
            workspace_id: Workspace to key.
            wrapped_key: The b64u ``iv ‖ tag ‖ ct`` record to store when the
                workspace is not yet keyed.

        Returns:
            The wrapped key now authoritatively stored — ``wrapped_key`` on
            a first write, the winner's on a 409 adopt.

        Raises:
            ForbiddenError: Authenticated principal may not write the key.
            ApiError: The request failed otherwise.
        """
        try:
            self._send("PUT", f"/api/workspaces/{workspace_id}/key", json={"wrappedKey": wrapped_key})
        except QuarantinedError as exc:
            if exc.status != 409:
                raise
            winner = self.fetch_workspace_key(workspace_id)
            if winner is None:  # pragma: no cover — a 409 implies the row exists
                raise QuarantinedError("409 without a stored key", status=409) from exc
            return winner
        return wrapped_key

    # ------------------------------------------------------------------ prefs

    def get_prefs(self) -> UserPrefs:
        """Fetch the account's per-user UI prefs (``GET /api/me/prefs``, the
        ``client-server.ts:54`` port — header auth as the other methods).

        Raises:
            ValueError: The server answered a malformed body.
            ApiError: The request failed (offline/unreachable surfaces as
                :class:`NetworkError` — the caller falls back to the device
                cache, the web's honest-offline posture).
        """
        return normalize_user_prefs(self._get_json("/api/me/prefs"))

    def patch_prefs(self, *, cards_algorithm: CardsAlgorithm) -> UserPrefs:
        """Merge-patch the account's prefs (``PUT /api/me/prefs``, the
        ``client-server.ts`` ``patchPrefs`` port); this slice carries the
        ``cardsAlgorithm`` scalar the hub selector writes. The server's
        normalized answer is returned.

        Raises:
            ValueError: ``cards_algorithm`` is not ``"sm2"``/``"fsrs"``.
            ApiError: The request failed.
        """
        if cards_algorithm not in ("sm2", "fsrs"):
            raise ValueError(f"cards_algorithm must be 'sm2' or 'fsrs', got {cards_algorithm!r}")
        data = self._send("PUT", "/api/me/prefs", json={"cardsAlgorithm": cards_algorithm}).json()
        return normalize_user_prefs(data)

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

    def upload_snapshot(self, workspace_id: str, data: bytes, *, hlc: Hlc, sealed: bool = False) -> SnapshotUploadResult:
        """Upload a client-produced snapshot blob (WIRE.md).

        Args:
            workspace_id: Workspace the snapshot belongs to.
            data: Raw snapshot bytes (a serialized derived-state SQLite db,
                or a sealed image — see ``sealed``).
            hlc: The HLC up to which the snapshot covers the operation log;
                sent as the ``physical``/``logical`` query parameters.
            sealed: Declare the body a SEALED image (the engine's crypto
                branch is the only sealer) via ``sealed=1``; absent means a
                plaintext derived store. Sealed uploads skip the server's
                sanity gate (ciphertext is opaque) — the flag rides the
                snapshot row and is served back as ``sealed`` in the probe.

        Returns:
            The server's acknowledgement (snapshot id + covered seq).
        """
        params: dict[str, Any] = {"workspaceId": workspace_id, "physical": hlc.physical, "logical": hlc.logical}
        if sealed:
            params["sealed"] = "1"
        response = self._send("PUT", f"{RELAY_V2_BASE}/snapshot/data", params=params, content=data)
        return SnapshotUploadResult.model_validate(response.json())

    def upload_snapshot_part(self, workspace_id: str, upload: str, index: int, data: bytes) -> None:
        """Stage one part of a multipart snapshot upload (WIRE.md).

        Optional transport — large checkpoints only; the single
        :meth:`upload_snapshot` stays the default path. ``upload`` is a
        client-generated uuid naming the multipart upload; ``index`` is
        zero-based and each index stages exactly once (a duplicate index is
        rejected 422). The part body is binary; 201 acknowledges
        ``{upload, index}``.
        """
        if index < 0:
            raise ValueError(f"part index must be non-negative, got {index}")
        if not data:
            raise ValueError("snapshot part must be a non-empty binary body")
        params = {"workspaceId": workspace_id, "upload": upload, "index": index}
        self._send("PUT", f"{RELAY_V2_BASE}/snapshot/data/part", params=params, content=data)

    def commit_snapshot(
        self,
        workspace_id: str,
        upload: str,
        *,
        parts: int,
        hlc: Hlc,
        sealed: bool = False,
    ) -> SnapshotUploadResult:
        """Commit a multipart snapshot upload (WIRE.md).

        Concatenates the upload's parts 0..N-1 in order and stores the
        result as the checkpoint — the same sanity gate (sealed uploads are
        exempt) and the same ``sealed`` flag as the single PUT; the 201 body
        mirrors it. An unknown upload is 404; a ``parts`` count that does
        not match the staged indexes is 422; a gate failure is 422 with the
        staged parts left in place.

        Args:
            workspace_id: Workspace the checkpoint belongs to.
            upload: The multipart upload id previously staged via
                :meth:`upload_snapshot_part`.
            parts: The number of parts claimed; must match the staged
                indexes exactly.
            hlc: The HLC up to which the snapshot covers the operation log.
            sealed: Declare the committed image sealed (``sealed=1``).

        Returns:
            The server's acknowledgement (snapshot id + covered seq).
        """
        if parts <= 0:
            raise ValueError(f"parts must be positive, got {parts}")
        params: dict[str, Any] = {
            "workspaceId": workspace_id,
            "upload": upload,
            "parts": parts,
            "physical": hlc.physical,
            "logical": hlc.logical,
        }
        if sealed:
            params["sealed"] = "1"
        response = self._send("POST", f"{RELAY_V2_BASE}/snapshot/data/commit", params=params)
        return SnapshotUploadResult.model_validate(response.json())

    def stats(self, workspace_id: str) -> RelayStats:
        """Return the relay stats document for a workspace (WIRE.md)."""
        data = self._get_json(f"{RELAY_V2_BASE}/stats", params={"workspaceId": workspace_id})
        return RelayStats.model_validate(data)

    # ----------------------------------------------------------------- assets

    def post_asset(
        self,
        data: bytes,
        *,
        filename: str,
        mime_type: str | None = None,
        iv: str | None = None,
        object_id: str | None = None,
        workspace_id: str | None = None,
    ) -> AssetUploadResult:
        """Upload bytes to CAS (``POST /api/assets``, multipart).

        Plaintext path (``iv is None``): today's shape — one file part (+
        optional ``objectId`` text field); the server magic-byte-sniffs the
        type. Encrypted path (plan 05): ``data`` is the sealed ``tag ‖ ct``
        blob (see :func:`notees_gtk.core.protocol.crypto.seal_asset_if_keyed`)
        and the upload DECLARES itself — ``encrypted``, the declared
        ``mimeType``, and the b64u ``iv`` multipart fields — so the server
        skips sniffing and stores the declared type + the iv as asset
        metadata. A plaintext client cannot smuggle a declared type past
        sniffing: without ``encrypted`` the fields are ignored.

        Args:
            data: The bytes to store (plaintext or the sealed blob).
            filename: The file part's name (``originalName`` server-side).
            mime_type: Declared type — REQUIRED when ``iv`` is given.
            iv: b64u 12-byte nonce of a sealed upload, else ``None``.
            object_id: Optional node to ``asset.attach`` the upload to.
            workspace_id: Optional ``X-Workspace-Id`` (else the server default).

        Raises:
            ValueError: ``iv`` given without a declared ``mime_type``.
            ApiError: The server rejected the upload.
        """
        fields: dict[str, str] = {}
        if iv is not None:
            if not mime_type:
                raise ValueError("an encrypted asset upload must declare its mime_type")
            fields["encrypted"] = "1"
            fields["mimeType"] = mime_type
            fields["iv"] = iv
        if object_id is not None:
            fields["objectId"] = object_id
        headers = {"X-Workspace-Id": workspace_id} if workspace_id is not None else None
        response = self._send(
            "POST",
            "/api/assets",
            files={"file": (filename, data)},
            data=fields or None,
            headers=headers,
        )
        return AssetUploadResult.model_validate(response.json())

    def get_asset(self, asset_id: str, *, workspace_id: str | None = None) -> bytes:
        """Download an asset's raw stored bytes (``GET /api/assets/:id``).

        The bytes are exactly what CAS stores — plaintext or the sealed
        ``tag ‖ ct`` blob; openness is the caller's job via the asset info's
        ``iv`` (see :func:`notees_gtk.core.protocol.crypto.open_asset_if_sealed`).
        """
        headers = {"X-Workspace-Id": workspace_id} if workspace_id is not None else None
        response = self._send("GET", f"/api/assets/{asset_id}", headers=headers)
        return response.content

    def get_asset_info(self, asset_id: str) -> AssetInfo:
        """Fetch an asset's metadata (``GET /api/assets/:id/info``).

        ``iv`` present means the bytes are sealed — download + decrypt needs
        the workspace key, and without one the caller's honest-fallback
        surface applies (never a throw).
        """
        data = self._get_json(f"/api/assets/{asset_id}/info")
        return AssetInfo.model_validate(data)

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
