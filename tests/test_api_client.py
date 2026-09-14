"""Tests for the Notees REST API client.

All HTTP traffic is faked with ``httpx.MockTransport``: handlers assert the
exact request path and body (per ``protocol/SPEC.md`` §4 and the Notees auth
router) and never touch the network.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from notees_gtk.core.api import (
    MAX_BATCH_SIZE,
    AuthenticationError,
    AuthResult,
    ForbiddenError,
    NetworkError,
    NoteesClient,
    QuarantinedError,
    RateLimitedError,
    ServerError,
    SnapshotMeta,
    TwoFactorRequired,
    WorkspaceRef,
    classify_response,
)
from notees_gtk.core.api.errors import ApiError
from notees_gtk.core.protocol.models import (
    MAX_ENVELOPE_SIZE_BYTES,
    BatchRequest,
    CatchUpPaginatedResponse,
    RelayEnvelope,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BASE_URL = "https://notees.test"
WORKSPACE_ID = "018f0000-0000-7000-8000-000000000001"
ACTOR_ID = "018f0000-0000-7000-8000-0000000000aa"

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


def _make_client(handler: Handler) -> tuple[NoteesClient, list[httpx.Request]]:
    """Build a client over MockTransport, recording every request sent."""
    requests: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    return NoteesClient(BASE_URL, transport=httpx.MockTransport(recording)), requests


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


class TestLogin:
    def test_success_returns_auth_result_and_stores_bearer(self) -> None:
        def login_handler(request: httpx.Request) -> httpx.Response:
            assert _json_body(request) == {
                "email": "ada@example.com",
                "password": "hunter2",
                "remember_me": True,
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

    def test_remember_me_false_is_sent(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert _json_body(request)["remember_me"] is False
            return httpx.Response(200, json=LOGIN_OK)

        client, _ = _make_client(_router({("POST", "/api/auth/login"): handler}))
        client.login("ada@example.com", "hunter2", remember_me=False)

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
            assert request.url.path == "/api/relay/batch"
            assert _json_body(request) == fixture
            return httpx.Response(200, json={"saved_count": 2, "saved_ids": saved_ids})

        client, _ = _make_client(_router({("POST", "/api/relay/batch"): handler}))
        result = client.submit_batch(_batch_envelopes())
        assert result == saved_ids

    def test_duplicate_ids_may_be_omitted_from_saved_ids(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"saved_count": 1, "saved_ids": ["018f0000-0000-7000-8000-000000000101"]})

        client, _ = _make_client(_router({("POST", "/api/relay/batch"): handler}))
        result = client.submit_batch(_batch_envelopes())
        assert result == ["018f0000-0000-7000-8000-000000000101"]

    def test_more_than_max_batch_size_rejected_before_wire(self) -> None:
        env = _batch_envelopes()[0]
        envelopes = [env.model_copy(update={"id": f"018f0000-0000-7000-8000-{i:012d}"}) for i in range(MAX_BATCH_SIZE + 1)]

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("request must not hit the transport")

        client, requests = _make_client(handler)
        with pytest.raises(ValueError, match="exceeds"):
            client.submit_batch(envelopes)
        assert requests == []

    def test_oversized_envelope_rejected_before_wire(self) -> None:
        env = RelayEnvelope(
            workspace_id=WORKSPACE_ID,
            actor_id=ACTOR_ID,
            hlc={"physical": 0, "logical": 0},
            op_type="node.create",
            payload={"blob": "x" * (MAX_ENVELOPE_SIZE_BYTES + 1)},
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

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/relay/catch-up"
            assert _json_body(request) == fixture_request
            return httpx.Response(200, json=fixture_response)

        client, _ = _make_client(_router({("POST", "/api/relay/catch-up"): handler}))
        page = client.catch_up(WORKSPACE_ID, after_seq=42, limit=1000)

        assert isinstance(page, CatchUpPaginatedResponse)
        assert [envelope.id for envelope in page.envelopes] == ["018f0000-0000-7000-8000-000000000102"]
        assert page.next_after_seq is None
        assert page.has_more is False
        assert page.restore_epoch == 0
        assert page.total_remaining == 1

    def test_defaults_to_cold_start_cursor(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert _json_body(request) == {"workspace_id": WORKSPACE_ID, "after_seq": 0, "limit": 1000}
            return httpx.Response(200, json={"envelopes": [], "next_after_seq": None, "has_more": False, "restore_epoch": 0, "total_remaining": 0})

        client, _ = _make_client(_router({("POST", "/api/relay/catch-up"): handler}))
        page = client.catch_up(WORKSPACE_ID)
        assert page.envelopes == []
        assert page.has_more is False


class TestSnapshots:
    def test_probe_maps_snapshot_meta(self) -> None:
        probe: dict[str, Any] = {
            "snapshot_id": "s1",
            "workspace_id": WORKSPACE_ID,
            "hlc": {"physical": 1767225600000, "logical": 7},
            "has_snapshot": True,
            "restore_epoch": 3,
            "up_to_seq": 77,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/relay/snapshot"
            assert request.url.params["workspace_id"] == WORKSPACE_ID
            return httpx.Response(200, json=probe)

        client, _ = _make_client(_router({("GET", "/api/relay/snapshot"): handler}))
        meta = client.snapshot_probe(WORKSPACE_ID)

        assert isinstance(meta, SnapshotMeta)
        assert meta.snapshot_id == "s1"
        assert meta.workspace_id == WORKSPACE_ID
        assert meta.hlc.physical == 1767225600000
        assert meta.hlc.logical == 7
        assert meta.has_snapshot is True
        assert meta.restore_epoch == 3
        assert meta.up_to_seq == 77

    def test_probe_without_snapshot_allows_null_up_to_seq(self) -> None:
        probe: dict[str, Any] = {
            "snapshot_id": "",
            "workspace_id": WORKSPACE_ID,
            "hlc": {"physical": 0, "logical": 0},
            "has_snapshot": False,
            "restore_epoch": 0,
            "up_to_seq": None,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=probe)

        client, _ = _make_client(_router({("GET", "/api/relay/snapshot"): handler}))
        meta = client.snapshot_probe(WORKSPACE_ID)
        assert meta.has_snapshot is False
        assert meta.up_to_seq is None

    def test_snapshot_data_returns_raw_bytes(self) -> None:
        blob = b"SQLite format 3\x00fake-snapshot"

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/relay/snapshot/data"
            assert request.url.params["workspace_id"] == WORKSPACE_ID
            return httpx.Response(200, content=blob, headers={"Content-Type": "application/octet-stream"})

        client, _ = _make_client(_router({("GET", "/api/relay/snapshot/data"): handler}))
        assert client.snapshot_data(WORKSPACE_ID) == blob

    def test_stats_returns_raw_dict(self) -> None:
        stats: dict[str, Any] = {
            "workspace_id": WORKSPACE_ID,
            "envelope_count": 42,
            "envelope_size_bytes": 1234,
            "snapshot_count": 1,
            "latest_snapshot_hlc": {"physical": 1767225600000, "logical": 7},
            "compacted_segment_count": 0,
            "compacted_operation_count": 0,
            "max_hlc": {"physical": 1767225600000, "logical": 9},
            "restore_epoch": 3,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/relay/stats"
            assert request.url.params["workspace_id"] == WORKSPACE_ID
            return httpx.Response(200, json=stats)

        client, _ = _make_client(_router({("GET", "/api/relay/stats"): handler}))
        assert client.stats(WORKSPACE_ID) == stats


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

        client, _ = _make_client(_router({("POST", "/api/relay/catch-up"): handler}))
        with pytest.raises(expected) as exc_info:
            client.catch_up(WORKSPACE_ID)

        assert exc_info.value.status == status
        assert exc_info.value.detail == detail

    def test_rate_limited_parses_retry_after(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"detail": "Too many requests"}, headers={"Retry-After": "2.5"})

        client, _ = _make_client(_router({("POST", "/api/relay/batch"): handler}))
        with pytest.raises(RateLimitedError) as exc_info:
            client.submit_batch(_batch_envelopes())

        assert exc_info.value.status == 429
        assert exc_info.value.retry_after == 2.5

    def test_rate_limited_without_retry_after(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"detail": "slow down"})

        client, _ = _make_client(_router({("GET", "/api/relay/stats"): handler}))
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

        client, _ = _make_client(_router({("POST", "/api/relay/batch"): handler}))
        with pytest.raises(QuarantinedError) as exc_info:
            client.submit_batch(_batch_envelopes())
        assert "envelopes" in exc_info.value.detail

    def test_detail_falls_back_to_raw_body(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        client, _ = _make_client(_router({("GET", "/api/relay/stats"): handler}))
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
