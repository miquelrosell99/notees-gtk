"""Tests for the realtime WS client (``notees_gtk.core.api.ws``).

Every test runs against an in-process scripted relay: a ``websockets`` sync
server on a background thread pushes frames into the client and records the
frames the client sends. Reconnect backoff is shortened to milliseconds.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import WsRelayStub, wait_until
from websockets.sync.server import ServerConnection

from notees_gtk.core.api import (
    HelloInfo,
    NoteesClient,
    ProtocolVersionError,
    RealtimeClient,
    RealtimeProtocolError,
    build_ws_url,
)
from notees_gtk.core.protocol.models import RelayEnvelope

FIXTURES = Path(__file__).resolve().parent / "fixtures"
WORKSPACE_ID = "ws-a"
FAST_RECONNECT = (0.05, 0.1, 0.2)


def _no_http(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected HTTP request: {request.url}")


HELLO = {"type": "hello", "wsProtocolVersion": 2, "restoreEpoch": 3, "latestSeq": 42}
OPS = {
    "type": "ops",
    "wsProtocolVersion": 2,
    "envelopes": [json.loads((FIXTURES / "wire" / "envelope-minimal.json").read_text())],
    "seqs": {"0192a000-0000-7000-8000-0000000000f1": 43},
}


def make_client(stub: WsRelayStub, **callbacks: Any) -> RealtimeClient:
    http = NoteesClient(f"http://127.0.0.1:{stub.port}", api_key="secret-key", transport=httpx.MockTransport(_no_http))
    return RealtimeClient(http, WORKSPACE_ID, reconnect_delays=FAST_RECONNECT, **callbacks)


class TestUrlBuilding:
    def test_http_becomes_ws(self) -> None:
        url = build_ws_url("http://notees.test:8001/", WORKSPACE_ID, "tok en")
        assert url == "ws://notees.test:8001/api/relay/v2/ws/ws-a?token=tok%20en"

    def test_https_becomes_wss(self) -> None:
        url = build_ws_url("https://notees.test", WORKSPACE_ID, "k")
        assert url == "wss://notees.test/api/relay/v2/ws/ws-a?token=k"

    def test_workspace_id_is_quoted(self) -> None:
        url = build_ws_url("http://h", "ws/odd", "k")
        assert url == "ws://h/api/relay/v2/ws/ws%2Fodd?token=k"


class TestLifecycle:
    def test_requires_a_credential(self) -> None:
        http = NoteesClient("http://127.0.0.1:1", transport=httpx.MockTransport(_no_http))
        with pytest.raises(ValueError, match="no API key"):
            RealtimeClient(http, WORKSPACE_ID)

    def test_stop_before_start_is_safe(self) -> None:
        http = NoteesClient("http://127.0.0.1:1", api_key="k", transport=httpx.MockTransport(_no_http))
        RealtimeClient(http, WORKSPACE_ID).stop()

    def test_stop_closes_cleanly_and_never_reconnects(self) -> None:
        with WsRelayStub(on_connect=lambda conn, _i: conn.send(json.dumps(HELLO))) as stub:
            client = make_client(stub)
            client.start()
            assert wait_until(lambda: stub.connection_count() == 1)
            client.stop()
            assert not client.running
            time.sleep(0.5)  # well beyond the fast reconnect schedule
            assert stub.connection_count() == 1  # NO reconnect
            assert stub.close_codes == [1000]  # clean close code

    def test_hello_dispatches_and_updates_state(self) -> None:
        with WsRelayStub(on_connect=lambda conn, _i: conn.send(json.dumps(HELLO))) as stub:
            hellos: list[HelloInfo] = []
            client = make_client(stub, on_hello=hellos.append)
            client.start()
            try:
                assert wait_until(lambda: len(hellos) == 1)
                assert hellos[0] == HelloInfo(latest_seq=42, restore_epoch=3)
                assert client.latest_seq == 42
                assert client.restore_epoch == 3
                assert client.failed is False
            finally:
                client.stop()
            assert stub.close_codes and stub.close_codes[-1] == 1000


class TestFrameDispatch:
    def test_ops_dispatches_envelopes_and_seqs(self) -> None:
        ops_calls: list[tuple[list[RelayEnvelope], dict[str, int]]] = []
        with WsRelayStub(
            on_connect=lambda conn, _i: (conn.send(json.dumps(HELLO)), conn.send(json.dumps(OPS)))
        ) as stub:
            client = make_client(stub, on_ops=lambda envs, seqs: ops_calls.append((envs, seqs)))
            client.start()
            try:
                assert wait_until(lambda: len(ops_calls) == 1)
                envelopes, seqs = ops_calls[0]
                assert [env.id for env in envelopes] == ["0192a000-0000-7000-8000-0000000000f1"]
                assert envelopes[0].op_type == "object.create"
                assert seqs == {"0192a000-0000-7000-8000-0000000000f1": 43}
            finally:
                client.stop()

    def test_ack_dispatches_saved_ids(self) -> None:
        acks: list[list[str]] = []
        frame = {"type": "ack", "savedIds": ["id-1", "id-2"]}
        with WsRelayStub(
            on_connect=lambda conn, _i: (conn.send(json.dumps(HELLO)), conn.send(json.dumps(frame)))
        ) as stub:
            client = make_client(stub, on_ack=acks.append)
            client.start()
            try:
                assert wait_until(lambda: acks == [["id-1", "id-2"]])
            finally:
                client.stop()

    def test_error_frame_reaches_error_callback_and_connection_stays(self) -> None:
        errors: list[BaseException] = []
        ops_calls: list[object] = []
        with WsRelayStub(
            on_connect=lambda conn, _i: (
                conn.send(json.dumps(HELLO)),
                conn.send(json.dumps({"type": "error", "message": "bad batch"})),
                conn.send(json.dumps(OPS)),
            )
        ) as stub:
            client = make_client(stub, on_error=errors.append, on_ops=lambda envs, _seqs: ops_calls.append(envs))
            client.start()
            try:
                assert wait_until(lambda: len(ops_calls) == 1)
                assert [str(error) for error in errors] == ["bad batch"]
            finally:
                client.stop()

    def test_unknown_frame_type_is_ignored(self) -> None:
        errors: list[BaseException] = []
        ops_calls: list[object] = []
        with WsRelayStub(
            on_connect=lambda conn, _i: (
                conn.send(json.dumps(HELLO)),
                conn.send(json.dumps({"type": "mystery", "data": 1})),
                conn.send(json.dumps(OPS)),
            )
        ) as stub:
            client = make_client(stub, on_error=errors.append, on_ops=lambda envs, _seqs: ops_calls.append(envs))
            client.start()
            try:
                assert wait_until(lambda: len(ops_calls) == 1)
                assert errors == []
            finally:
                client.stop()

    def test_malformed_json_answers_error_callback_and_connection_stays(self) -> None:
        errors: list[BaseException] = []
        ops_calls: list[object] = []
        with WsRelayStub(
            on_connect=lambda conn, _i: (
                conn.send(json.dumps(HELLO)),
                conn.send("not json {"),
                conn.send(json.dumps(OPS)),
            )
        ) as stub:
            client = make_client(stub, on_error=errors.append, on_ops=lambda envs, _seqs: ops_calls.append(envs))
            client.start()
            try:
                assert wait_until(lambda: len(ops_calls) == 1)
                assert len(errors) == 1
                assert isinstance(errors[0], RealtimeProtocolError)
            finally:
                client.stop()

    def test_invalid_envelope_in_ops_drops_the_frame_and_stays(self) -> None:
        errors: list[BaseException] = []
        ops_calls: list[object] = []
        bad_ops = {"type": "ops", "wsProtocolVersion": 2, "envelopes": [{"id": "not-an-envelope"}], "seqs": {}}
        with WsRelayStub(
            on_connect=lambda conn, _i: (
                conn.send(json.dumps(HELLO)),
                conn.send(json.dumps(bad_ops)),
                conn.send(json.dumps(OPS)),
            )
        ) as stub:
            client = make_client(stub, on_error=errors.append, on_ops=lambda envs, _seqs: ops_calls.append(envs))
            client.start()
            try:
                assert wait_until(lambda: len(ops_calls) == 1)  # only the good frame
                assert len(errors) == 1 and isinstance(errors[0], RealtimeProtocolError)
            finally:
                client.stop()

    def test_raising_consumer_callback_never_kills_the_reader(self) -> None:
        ops_calls: list[object] = []

        def boom_on_hello(_info: HelloInfo) -> None:
            raise RuntimeError("consumer bug")

        with WsRelayStub(
            on_connect=lambda conn, _i: (conn.send(json.dumps(HELLO)), conn.send(json.dumps(OPS)))
        ) as stub:
            client = make_client(stub, on_hello=boom_on_hello, on_ops=lambda envs, _seqs: ops_calls.append(envs))
            client.start()
            try:
                assert wait_until(lambda: len(ops_calls) == 1)
            finally:
                client.stop()


class TestFailLoud:
    def test_newer_framing_on_hello_closes_and_never_reconnects(self) -> None:
        errors: list[BaseException] = []
        hello_v3 = {**HELLO, "wsProtocolVersion": 3}
        with WsRelayStub(on_connect=lambda conn, _i: conn.send(json.dumps(hello_v3))) as stub:
            client = make_client(stub, on_error=errors.append)
            client.start()
            try:
                assert wait_until(lambda: client.failed)
                assert len(errors) == 1
                assert isinstance(errors[0], ProtocolVersionError)
                assert "version 3" in str(errors[0])
            finally:
                client.stop()
            connections = stub.connection_count()
            time.sleep(0.5)  # well beyond the fast reconnect schedule
            assert stub.connection_count() == connections  # NO reconnect

    def test_newer_framing_on_ops_closes_and_never_reconnects(self) -> None:
        errors: list[BaseException] = []
        ops_v3 = {**OPS, "wsProtocolVersion": 3}
        with WsRelayStub(
            on_connect=lambda conn, _i: (conn.send(json.dumps(HELLO)), conn.send(json.dumps(ops_v3)))
        ) as stub:
            client = make_client(stub, on_error=errors.append)
            client.start()
            try:
                assert wait_until(lambda: client.failed)
                assert isinstance(errors[-1], ProtocolVersionError)
            finally:
                client.stop()
            connections = stub.connection_count()
            time.sleep(0.5)
            assert stub.connection_count() == connections


class TestReconnect:
    def test_reconnect_after_kill_delivers_later_ops(self) -> None:
        ops_calls: list[tuple[list[RelayEnvelope], dict[str, int]]] = []

        def on_connect(conn: ServerConnection, index: int) -> None:
            conn.send(json.dumps(HELLO))
            if index == 2:  # Fresh connection after the kill: later ops arrive here.
                conn.send(json.dumps(OPS))

        with WsRelayStub(on_connect=on_connect) as stub:
            client = make_client(stub, on_ops=lambda envs, seqs: ops_calls.append((envs, seqs)))
            client.start()
            try:
                assert wait_until(lambda: stub.connection_count() == 1)
                stub.close_all(code=1011)
                assert wait_until(lambda: len(ops_calls) == 1)
                assert stub.connection_count() == 2
            finally:
                client.stop()

    def test_backoff_resets_after_successful_hello(self) -> None:
        # The attempt counter resets on every hello; the observable contract is
        # that repeated quick kills keep reconnecting (never exhausting, never
        # giving up) — three kill/reconnect cycles land three ops frames.
        counter = {"ops": 0}

        def on_connect(conn: ServerConnection, _index: int) -> None:
            conn.send(json.dumps(HELLO))
            conn.send(json.dumps(OPS))

        with WsRelayStub(on_connect=on_connect) as stub:
            client = make_client(stub, on_ops=lambda *_args: counter.__setitem__("ops", counter["ops"] + 1))
            client.start()
            try:
                assert wait_until(lambda: counter["ops"] == 1)
                for expected in (2, 3):
                    stub.close_all(code=1011)
                    assert wait_until(lambda bound=expected: counter["ops"] >= bound)
                assert stub.connection_count() == 3
            finally:
                client.stop()


class TestSend:
    def test_send_batch_writes_a_batch_frame(self) -> None:
        envelope = json.loads((FIXTURES / "wire" / "envelope-minimal.json").read_text())
        with WsRelayStub(on_connect=lambda conn, _i: conn.send(json.dumps(HELLO))) as stub:
            client = make_client(stub)
            client.start()
            try:
                assert wait_until(lambda: stub.connection_count() == 1)
                assert client.send_batch([RelayEnvelope.model_validate(envelope)]) is True
                assert wait_until(lambda: len(stub.received) == 1)
                frame = stub.received[0]
                assert frame["type"] == "batch"
                assert frame["envelopes"][0]["id"] == envelope["id"]
            finally:
                client.stop()

    def test_send_batch_without_connection_returns_false(self) -> None:
        envelope = RelayEnvelope.model_validate(json.loads((FIXTURES / "wire" / "envelope-minimal.json").read_text()))
        with WsRelayStub() as stub:
            client = make_client(stub)
            assert client.send_batch([envelope]) is False
