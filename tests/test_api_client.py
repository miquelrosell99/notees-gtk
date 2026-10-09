"""Tests for the Notees REST API client (relay protocol v2).

All HTTP traffic is faked with ``httpx.MockTransport``: handlers assert the
exact request path, headers, and body (per ``packages/protocol/WIRE.md``
for the relay and the Notees auth router for login) and never touch the
network.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import normalize_json

from notees_gtk.core.api import (
    MAX_BATCH_SIZE,
    RELAY_V2_BASE,
    AuthenticationError,
    AuthResult,
    ForbiddenError,
    NetworkError,
    NoteesClient,
    QuarantinedError,
    RateLimitedError,
    RelayStats,
    ServerError,
    SnapshotMeta,
    SnapshotUploadResult,
    TwoFactorRequired,
    WorkspaceRef,
    classify_response,
)
from notees_gtk.core.api.errors import ApiError
from notees_gtk.core.protocol.clock import Hlc
from notees_gtk.core.protocol.models import (
    MAX_ENVELOPE_SIZE_BYTES,
    BatchRequest,
    CatchUpPaginatedResponse,
    RelayEnvelope,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BASE_URL = "https://notees.test"
WORKSPACE_ID = "0192a000-0000-7000-8000-000000000001"
ACTOR_ID = "0192a000-0000-7000-8000-000000000002"

LOGIN_OK: dict[str, Any] = {
    "access_token": "at-1",
    "refresh_token": "rt-1",
    "token_type": "bearer",
    "user": {"id": "u1", "email": "ada@example.com", "role": "user"},
}
TWO_FA_GATE: dict[str, Any] = {"requires_2fa": True, "preauth_token": "pre-1", "purpose": "verify"}

Handler = Callable[[httpx.Request], httpx.Response]


def _router(routes: dict[tuple[str, str], Handler]) -> Handler:
    """Dispatch MockTransport requests by (method, path), failing on the unexpected."""

    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if key not in routes:
            raise AssertionError(f"unexpected request: {request.method} {request.url}")
        return routes[key](request)

    return handler


def _make_client(handler: Handler, **kwargs: Any) -> tuple[NoteesClient, list[httpx.Request]]:
    """Build a client over MockTransport, recording every request sent."""
    requests: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    return NoteesClient(BASE_URL, transport=httpx.MockTransport(recording), **kwargs), requests


def _batch_envelopes() -> list[RelayEnvelope]:
    raw = json.loads((FIXTURES / "batch-request.json").read_text())
    return BatchRequest.model_validate(raw).envelopes


def _json_body(request: httpx.Request) -> Any:
    return json.loads(request.content)


class TestBaseUrl:
    def test_trailing_slash_is_stripped(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json={"items": [], "total": 0, "page": 1, "page_size": 50})

        client = NoteesClient(f"{BASE_URL}/", transport=httpx.MockTransport(handler))
        client.list_workspaces()
        assert seen == [f"{BASE_URL}/api/workspaces/"]

    def test_token_sets_bearer_header_upfront(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("Authorization", ""))
            return httpx.Response(200, json={"items": [], "total": 0, "page": 1, "page_size": 50})

        client = NoteesClient(BASE_URL, token="tok", transport=httpx.MockTransport(handler))
        client.list_workspaces()
        assert seen == ["Bearer tok"]


class TestApiKeyAuth:
    def test_api_key_sets_x_api_key_header(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("X-API-Key", ""))
            return httpx.Response(
                200,
                json={
                    "envelopeCount": 0,
                    "snapshotCount": 0,
                    "compactedOperationCount": 0,
                    "maxHlc": {"physical": 0, "logical": 0},
                    "restoreEpoch": 0,
                    "latestSnapshotHlc": None,
                },
            )

        client = NoteesClient(BASE_URL, api_key="secret-key", transport=httpx.MockTransport(handler))
        client.stats(WORKSPACE_ID)
        assert seen == ["secret-key"]

    def test_api_key_header_present_without_login(self) -> None:
        """The v2 relay is key-only: no login round-trip happens."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["X-API-Key"] == "secret-key"
            assert "Authorization" not in request.headers
            return httpx.Response(200, json={"savedCount": 0, "savedIds": []})

        client, _ = _make_client(_router({("POST", f"{RELAY_V2_BASE}/batch"): handler}), api_key="secret-key")
        assert client.submit_batch([]) == []

    def test_ws_token_prefers_api_key_then_bearer(self) -> None:
        assert NoteesClient(BASE_URL, transport=httpx.MockTransport(_no_http), api_key="k", token="t").ws_token() == "k"
        assert NoteesClient(BASE_URL, transport=httpx.MockTransport(_no_http), token="t").ws_token() == "t"
        assert NoteesClient(BASE_URL, transport=httpx.MockTransport(_no_http)).ws_token() is None

    def test_login_replaces_bearer_and_keeps_api_key(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=LOGIN_OK)

        client, _ = _make_client(_router({("POST", "/api/auth/login"): handler}), api_key="k")
        client.login("ada@example.com", "hunter2")
        assert client._client.headers["Authorization"] == "Bearer at-1"
        assert client._client.headers["X-API-Key"] == "k"


class TestLogin:
    def test_success_returns_auth_result_and_stores_bearer(self) -> None:
        def login_handler(request: httpx.Request) -> httpx.Response:
            # The server schema is strict {email, password} — extra keys 422
            # (the old remember_me field blocked login until it was dropped).
            assert _json_body(request) == {
                "email": "ada@example.com",
                "password": "hunter2",
            }
            return httpx.Response(200, json=LOGIN_OK)

        def workspaces_handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == "Bearer at-1"
            return httpx.Response(200, json={"items": [], "total": 0, "page": 1, "page_size": 50})

        client, requests = _make_client(
            _router({("POST", "/api/auth/login"): login_handler, ("GET", "/api/workspaces/"): workspaces_handler})
        )
        result = client.login("ada@example.com", "hunter2")

        assert isinstance(result, AuthResult)
        assert result.access_token == "at-1"
        assert result.token_type == "bearer"
        assert result.user == LOGIN_OK["user"]

        client.list_workspaces()
        assert len(requests) == 2

    def test_login_body_is_strict_email_password(self) -> None:
        """No extra keys ride the login body — the server rejects them with 422."""

        def handler(request: httpx.Request) -> httpx.Response:
            body = _json_body(request)
            assert set(body) == {"email", "password"}
            return httpx.Response(200, json=LOGIN_OK)

        client, _ = _make_client(_router({("POST", "/api/auth/login"): handler}))
        client.login("ada@example.com", "hunter2")

    def test_wrong_password_raises_authentication_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"detail": "Invalid email or password"})

        client, _ = _make_client(_router({("POST", "/api/auth/login"): handler}))
        with pytest.raises(AuthenticationError) as exc_info:
            client.login("ada@example.com", "wrong")

        assert exc_info.value.status == 401
        assert exc_info.value.detail == "Invalid email or password"

    def test_two_factor_gate_raises_without_totp(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(200, json=TWO_FA_GATE)

        client, _ = _make_client(_router({("POST", "/api/auth/login"): handler}))
        with pytest.raises(TwoFactorRequired) as exc_info:
            client.login("ada@example.com", "hunter2")

        assert exc_info.value.preauth_token == "pre-1"
        assert exc_info.value.purpose == "verify"
        assert calls == ["/api/auth/login"]

    def test_two_factor_verify_completes_login_when_totp_supplied(self) -> None:
        def verify_handler(request: httpx.Request) -> httpx.Response:
            assert _json_body(request) == {"preauth_token": "pre-1", "code": "123456"}
            return httpx.Response(200, json=LOGIN_OK)

        client, requests = _make_client(
            _router(
                {
                    ("POST", "/api/auth/login"): lambda request: httpx.Response(200, json=TWO_FA_GATE),
                    ("POST", "/api/auth/2fa/verify"): verify_handler,
                }
            )
        )
        result = client.login("ada@example.com", "hunter2", totp="123456")

        assert isinstance(result, AuthResult)
        assert result.access_token == "at-1"
        assert [req.url.path for req in requests] == ["/api/auth/login", "/api/auth/2fa/verify"]

        # The bearer issued by the verify call is used for subsequent requests.
        def workspaces_handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == "Bearer at-1"
            return httpx.Response(200, json={"items": [], "total": 0, "page": 1, "page_size": 50})

        client2, _ = _make_client(
            _router(
                {
                    ("POST", "/api/auth/login"): lambda request: httpx.Response(200, json=TWO_FA_GATE),
                    ("POST", "/api/auth/2fa/verify"): lambda request: httpx.Response(200, json=LOGIN_OK),
                    ("GET", "/api/workspaces/"): workspaces_handler,
                }
            )
        )
        client2.login("ada@example.com", "hunter2", totp="123456")
        client2.list_workspaces()


class TestWorkspaces:
    def test_trailing_slash_and_items_unwrap(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/workspaces/"
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"uuid": "w1", "name": "Personal", "is_active": True},
                        {"uuid": "w2", "name": "Archive", "is_active": False},
                    ],
                    "total": 2,
                    "page": 1,
                    "page_size": 50,
                },
            )

        client, requests = _make_client(_router({("GET", "/api/workspaces/"): handler}))
        workspaces = client.list_workspaces()

        assert workspaces == [
            WorkspaceRef(uuid="w1", name="Personal", is_active=True),
            WorkspaceRef(uuid="w2", name="Archive", is_active=False),
        ]
        assert len(requests) == 1

    def test_empty_items(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"items": [], "total": 0, "page": 1, "page_size": 50})

        client, _ = _make_client(_router({("GET", "/api/workspaces/"): handler}))
        assert client.list_workspaces() == []


class TestSubmitBatch:
    def test_body_matches_fixture_shape_and_returns_saved_ids(self) -> None:
        fixture = json.loads((FIXTURES / "batch-request.json").read_text())
        saved_ids = [envelope["id"] for envelope in fixture["envelopes"]]

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == f"{RELAY_V2_BASE}/batch"
            assert normalize_json(_json_body(request)) == normalize_json(fixture)
            return httpx.Response(200, json={"savedCount": len(saved_ids), "savedIds": saved_ids})

        client, _ = _make_client(_router({("POST", f"{RELAY_V2_BASE}/batch"): handler}))
        result = client.submit_batch(_batch_envelopes())
        assert result == saved_ids

    def test_duplicate_ids_may_be_omitted_from_saved_ids(self) -> None:
        first_id = "0192a000-0000-7000-8000-0000000000f2"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"savedCount": 1, "savedIds": [first_id]})

        client, _ = _make_client(_router({("POST", f"{RELAY_V2_BASE}/batch"): handler}))
        result = client.submit_batch(_batch_envelopes())
        assert result == [first_id]

    def test_more_than_max_batch_size_rejected_before_wire(self) -> None:
        env = _batch_envelopes()[0]
        envelopes = [
            env.model_copy(update={"id": f"0192a000-0000-7000-8000-{i:012d}"}) for i in range(MAX_BATCH_SIZE + 1)
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("request must not hit the transport")

        client, requests = _make_client(handler)
        with pytest.raises(ValueError, match="exceeds"):
            client.submit_batch(envelopes)
        assert requests == []

    def test_oversized_envelope_rejected_before_wire(self) -> None:
        env = RelayEnvelope(
            protocolVersion=3,
            workspaceId=WORKSPACE_ID,
            actorId=ACTOR_ID,
            deviceId="api-test-device",
            hlc={"physical": 0, "logical": 0},
            opType="object.create",
            payload={"blob": "x" * (MAX_ENVELOPE_SIZE_BYTES + 1)},
            timestamp="2026-01-01T00:00:00Z",
        )

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("request must not hit the transport")

        client, requests = _make_client(handler)
        with pytest.raises(ValueError, match="exceeds maximum payload size"):
            client.submit_batch([env])
        assert requests == []


class TestCatchUp:
    def test_parses_fixture_response(self) -> None:
        fixture_request = json.loads((FIXTURES / "catch-up-request.json").read_text())
        fixture_response = json.loads((FIXTURES / "catch-up-response.json").read_text())
        workspace_id = fixture_request["workspaceId"]

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == f"{RELAY_V2_BASE}/catch-up"
            assert _json_body(request) == fixture_request
            return httpx.Response(200, json=fixture_response)

        client, _ = _make_client(_router({("POST", f"{RELAY_V2_BASE}/catch-up"): handler}))
        page = client.catch_up(workspace_id, after_seq=42, limit=1000)

        assert isinstance(page, CatchUpPaginatedResponse)
        assert [envelope.id for envelope in page.envelopes] == ["0192a000-0000-7000-8000-000000000101"]
        assert page.next_after_seq is None
        assert page.has_more is False
        assert page.restore_epoch == 0
        assert page.total_remaining == 1

    def test_defaults_to_cold_start_cursor(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert _json_body(request) == {"workspaceId": WORKSPACE_ID, "afterSeq": 0, "limit": 1000}
            return httpx.Response(
                200,
                json={
                    "envelopes": [],
                    "nextAfterSeq": None,
                    "hasMore": False,
                    "restoreEpoch": 0,
                    "totalRemaining": 0,
                },
            )

        client, _ = _make_client(_router({("POST", f"{RELAY_V2_BASE}/catch-up"): handler}))
        page = client.catch_up(WORKSPACE_ID)
        assert page.envelopes == []
        assert page.has_more is False


class TestSnapshots:
    def test_probe_maps_v2_snapshot_meta(self) -> None:
        probe: dict[str, Any] = {
            "snapshotId": "0192a000-0000-7000-8000-0000000000e1",
            "hlc": {"physical": 1767225600000, "logical": 7},
            "hasSnapshot": True,
            "restoreEpoch": 3,
            "upToSeq": 77,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == f"{RELAY_V2_BASE}/snapshot"
            assert request.url.params["workspaceId"] == WORKSPACE_ID
            return httpx.Response(200, json=probe)

        client, _ = _make_client(_router({("GET", f"{RELAY_V2_BASE}/snapshot"): handler}))
        meta = client.snapshot_probe(WORKSPACE_ID)

        assert isinstance(meta, SnapshotMeta)
        assert meta.snapshot_id == "0192a000-0000-7000-8000-0000000000e1"
        assert meta.hlc.physical == 1767225600000
        assert meta.hlc.logical == 7
        assert meta.has_snapshot is True
        assert meta.restore_epoch == 3
        assert meta.up_to_seq == 77

    def test_probe_without_snapshot_allows_null_up_to_seq(self) -> None:
        probe: dict[str, Any] = {
            "snapshotId": "",
            "hlc": {"physical": 0, "logical": 0},
            "hasSnapshot": False,
            "restoreEpoch": 0,
            "upToSeq": None,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=probe)

        client, _ = _make_client(_router({("GET", f"{RELAY_V2_BASE}/snapshot"): handler}))
        meta = client.snapshot_probe(WORKSPACE_ID)
        assert meta.has_snapshot is False
        assert meta.up_to_seq is None

    def test_snapshot_data_returns_raw_bytes(self) -> None:
        blob = b"SQLite format 3\x00fake-snapshot"

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == f"{RELAY_V2_BASE}/snapshot/data"
            assert request.url.params["workspaceId"] == WORKSPACE_ID
            return httpx.Response(200, content=blob, headers={"Content-Type": "application/octet-stream"})

        client, _ = _make_client(_router({("GET", f"{RELAY_V2_BASE}/snapshot/data"): handler}))
        assert client.snapshot_data(WORKSPACE_ID) == blob

    def test_upload_snapshot_puts_raw_bytes(self) -> None:
        blob = b"SQLite format 3\x00client-snapshot"

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "PUT"
            assert request.url.path == f"{RELAY_V2_BASE}/snapshot/data"
            assert request.url.params["workspaceId"] == WORKSPACE_ID
            assert request.url.params["physical"] == "1767225600000"
            assert request.url.params["logical"] == "7"
            assert request.content == blob
            return httpx.Response(
                201,
                json={
                    "snapshotId": "0192a000-0000-7000-8000-0000000000e2",
                    "workspaceId": WORKSPACE_ID,
                    "upToHlc": {"physical": 1767225600000, "logical": 7},
                    "upToSeq": 99,
                },
            )

        client, _ = _make_client(_router({("PUT", f"{RELAY_V2_BASE}/snapshot/data"): handler}))
        result = client.upload_snapshot(WORKSPACE_ID, blob, hlc=Hlc(physical=1767225600000, logical=7))

        assert isinstance(result, SnapshotUploadResult)
        assert result.snapshot_id == "0192a000-0000-7000-8000-0000000000e2"
        assert result.workspace_id == WORKSPACE_ID
        assert result.up_to_hlc == Hlc(physical=1767225600000, logical=7)
        assert result.up_to_seq == 99

    def test_stats_maps_v2_shape(self) -> None:
        stats: dict[str, Any] = {
            "envelopeCount": 42,
            "snapshotCount": 1,
            "compactedOperationCount": 5,
            "maxHlc": {"physical": 1767225600000, "logical": 9},
            "restoreEpoch": 3,
            "latestSnapshotHlc": {"physical": 1767225600000, "logical": 7},
        }

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == f"{RELAY_V2_BASE}/stats"
            assert request.url.params["workspaceId"] == WORKSPACE_ID
            return httpx.Response(200, json=stats)

        client, _ = _make_client(_router({("GET", f"{RELAY_V2_BASE}/stats"): handler}))
        result = client.stats(WORKSPACE_ID)

        assert isinstance(result, RelayStats)
        assert result.envelope_count == 42
        assert result.snapshot_count == 1
        assert result.compacted_operation_count == 5
        assert result.max_hlc == Hlc(physical=1767225600000, logical=9)
        assert result.restore_epoch == 3
        assert result.latest_snapshot_hlc == Hlc(physical=1767225600000, logical=7)

    def test_stats_without_snapshot_has_null_latest_hlc(self) -> None:
        stats: dict[str, Any] = {
            "envelopeCount": 0,
            "snapshotCount": 0,
            "compactedOperationCount": 0,
            "maxHlc": {"physical": 0, "logical": 0},
            "restoreEpoch": 0,
            "latestSnapshotHlc": None,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=stats)

        client, _ = _make_client(_router({("GET", f"{RELAY_V2_BASE}/stats"): handler}))
        assert client.stats(WORKSPACE_ID).latest_snapshot_hlc is None


def _no_http(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected HTTP request: {request.url}")


class TestErrorTaxonomy:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (403, ForbiddenError),
            (422, QuarantinedError),
            (500, ServerError),
        ],
    )
    def test_status_maps_to_subclass(self, status: int, expected: type[ApiError]) -> None:
        detail = "nope"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"detail": detail})

        client, _ = _make_client(_router({("POST", f"{RELAY_V2_BASE}/catch-up"): handler}))
        with pytest.raises(expected) as exc_info:
            client.catch_up(WORKSPACE_ID)

        assert exc_info.value.status == status
        assert exc_info.value.detail == detail

    @pytest.mark.parametrize(
        ("code", "status", "expected"),
        [
            ("unauthenticated", 401, AuthenticationError),
            ("forbidden", 403, ForbiddenError),
            ("validation_failed", 422, QuarantinedError),
            ("not_found", 404, QuarantinedError),
            ("conflict", 409, QuarantinedError),
            ("rate_limited", 429, RateLimitedError),
            ("idempotency_replay", 409, ServerError),
        ],
    )
    def test_v2_error_envelope_codes_map_to_taxonomy(self, code: str, status: int, expected: type[ApiError]) -> None:
        """WIRE.md stable codes take priority over status heuristics."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status, json={"error": {"code": code, "message": f"machine {code}", "status": status}}
            )

        client, _ = _make_client(_router({("POST", f"{RELAY_V2_BASE}/batch"): handler}))
        with pytest.raises(expected) as exc_info:
            client.submit_batch(_batch_envelopes())

        assert exc_info.value.code == code
        assert exc_info.value.detail == f"machine {code}"
        assert exc_info.value.status == status

    def test_idempotency_replay_is_retryable_server_error(self) -> None:
        """The engine retries ServerError; envelope-id dedupe makes the retry harmless."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                409, json={"error": {"code": "idempotency_replay", "message": "original in flight", "status": 409}}
            )

        client, _ = _make_client(_router({("POST", f"{RELAY_V2_BASE}/batch"): handler}))
        with pytest.raises(ServerError) as exc_info:
            client.submit_batch(_batch_envelopes())
        assert exc_info.value.code == "idempotency_replay"

    def test_unknown_code_falls_back_to_status(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(418, json={"error": {"code": "teapot", "message": "short and stout", "status": 418}})

        client, _ = _make_client(_router({("GET", f"{RELAY_V2_BASE}/stats"): handler}))
        with pytest.raises(QuarantinedError) as exc_info:
            client.stats(WORKSPACE_ID)
        assert exc_info.value.code == "teapot"
        assert exc_info.value.status == 418

    def test_rate_limited_parses_retry_after(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429,
                json={"error": {"code": "rate_limited", "message": "Too many requests", "status": 429}},
                headers={"Retry-After": "2.5"},
            )

        client, _ = _make_client(_router({("POST", f"{RELAY_V2_BASE}/batch"): handler}))
        with pytest.raises(RateLimitedError) as exc_info:
            client.submit_batch(_batch_envelopes())

        assert exc_info.value.status == 429
        assert exc_info.value.retry_after == 2.5
        assert exc_info.value.code == "rate_limited"

    def test_rate_limited_without_retry_after(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"detail": "slow down"})

        client, _ = _make_client(_router({("GET", f"{RELAY_V2_BASE}/stats"): handler}))
        with pytest.raises(RateLimitedError) as exc_info:
            client.stats(WORKSPACE_ID)
        assert exc_info.value.retry_after is None

    def test_transport_error_becomes_network_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client, _ = _make_client(handler)
        with pytest.raises(NetworkError) as exc_info:
            client.list_workspaces()

        assert exc_info.value.status is None
        assert isinstance(exc_info.value.__cause__, httpx.ConnectError)

    def test_422_list_detail_is_serialized(self) -> None:
        fastapi_422 = {"detail": [{"loc": ["body", "envelopes"], "msg": "field required", "type": "value_error"}]}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(422, json=fastapi_422)

        client, _ = _make_client(_router({("POST", f"{RELAY_V2_BASE}/batch"): handler}))
        with pytest.raises(QuarantinedError) as exc_info:
            client.submit_batch(_batch_envelopes())
        assert "envelopes" in exc_info.value.detail

    def test_detail_falls_back_to_raw_body(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        client, _ = _make_client(_router({("GET", f"{RELAY_V2_BASE}/stats"): handler}))
        with pytest.raises(ServerError) as exc_info:
            client.stats(WORKSPACE_ID)
        assert exc_info.value.detail == "boom"


class TestClassifyResponse:
    def test_success_returns_none(self) -> None:
        assert classify_response(httpx.Response(200, json={})) is None
        assert classify_response(httpx.Response(204)) is None

    def test_retry_after_non_numeric_becomes_none(self) -> None:
        response = httpx.Response(429, json={"detail": "slow"}, headers={"Retry-After": "soon"})
        with pytest.raises(RateLimitedError) as exc_info:
            classify_response(response)
        assert exc_info.value.retry_after is None

    def test_subclass_default_statuses(self) -> None:
        assert AuthenticationError("x").status == 401
        assert ForbiddenError("x").status == 403
        assert RateLimitedError("x", retry_after=1.0).status == 429
        assert NetworkError("x").status is None
