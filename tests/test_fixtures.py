"""Validate protocol fixtures against the GTK client's v2 wire models.

Two fixture sets live under ``tests/fixtures/``:

- the repo's own wire fixtures (batch/catch-up/WS frames), and
- ``v2/`` — a verbatim port of ``v2/packages/protocol/fixtures/`` from the
  Notees monorepo. That port is the cross-implementation parity anchor: every
  envelope the TypeScript reference produces must parse through these models
  and round-trip field-by-field.

Each fixture is parsed by the model that owns that wire shape, serialized back
with the wire casing, and compared (timestamps compared as instants — pydantic
serializes ``...06.400Z`` with microsecond precision, same moment). If a model
and its fixture drift (renamed field, changed default, changed casing), this
test fails.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import normalize_json
from pydantic import BaseModel, ValidationError

from notees_gtk.core.protocol.models import (
    PROTOCOL_VERSION,
    WS_PROTOCOL_VERSION,
    BatchRequest,
    CatchUpPaginatedResponse,
    CatchUpRequest,
    RelayEnvelope,
    WsAckMessage,
    WsBatchMessage,
    WsErrorMessage,
    WsHelloMessage,
    WsOpsMessage,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# fixture path (relative to FIXTURES_DIR) -> (model, serialize with camelCase aliases)
FIXTURE_MODELS: dict[str, tuple[type[BaseModel], bool]] = {
    "batch-request.json": (BatchRequest, True),
    "catch-up-request.json": (CatchUpRequest, True),
    "catch-up-response.json": (CatchUpPaginatedResponse, True),
    "ws-hello.json": (WsHelloMessage, True),
    "ws-ops.json": (WsOpsMessage, True),
    "ws-batch.json": (WsBatchMessage, True),
    "ws-ack.json": (WsAckMessage, True),
    "ws-error.json": (WsErrorMessage, True),
}

# v2/ parity port: fixtures holding a single envelope document.
V2_SINGLE_ENVELOPE_FIXTURES = [
    "v2/envelope-minimal.json",
    "v2/object-create.json",
]

# v2/ parity port: fixtures holding {"comment": ..., "envelopes": [...]}.
V2_ENVELOPE_LIST_FIXTURES = [
    "v2/class-extends-cycle.json",
    "v2/class-extends-m2m.json",
    "v2/class-property-defaults.json",
    "v2/class-unassign.json",
    "v2/object-color.json",
    "v2/object-move-before.json",
    "v2/object-move.json",
    "v2/property-set-lww.json",
    "v2/typed-link-mark.json",
    "v2/typed-link-mark-deleted.json",
]


def _round_trip(raw: Any, serialized: Any, fixture_name: str) -> None:
    assert normalize_json(serialized) == normalize_json(raw), (
        f"{fixture_name} drifted from the wire models:\n"
        f"fixture:    {json.dumps(raw, sort_keys=True, default=str)}\n"
        f"serialized: {json.dumps(serialized, sort_keys=True, default=str)}"
    )


@pytest.mark.parametrize("fixture_name", sorted(FIXTURE_MODELS))
def test_fixture_round_trips_through_model(fixture_name: str) -> None:
    model, by_alias = FIXTURE_MODELS[fixture_name]
    raw = json.loads((FIXTURES_DIR / fixture_name).read_text())
    parsed = model.model_validate(raw)
    _round_trip(raw, parsed.model_dump(mode="json", by_alias=by_alias), fixture_name)


@pytest.mark.parametrize("fixture_name", V2_SINGLE_ENVELOPE_FIXTURES)
def test_v2_single_envelope_fixture_round_trips(fixture_name: str) -> None:
    """The monorepo's single-envelope fixtures parse and re-serialize exactly."""
    raw = json.loads((FIXTURES_DIR / fixture_name).read_text())
    parsed = RelayEnvelope.model_validate(raw)
    _round_trip(raw, parsed.model_dump(mode="json", by_alias=True), fixture_name)


@pytest.mark.parametrize("fixture_name", V2_ENVELOPE_LIST_FIXTURES)
def test_v2_envelope_list_fixture_round_trips(fixture_name: str) -> None:
    """The monorepo's multi-envelope fixtures: every envelope parses and re-serializes."""
    raw = json.loads((FIXTURES_DIR / fixture_name).read_text())
    envelopes = raw["envelopes"]
    parsed = [RelayEnvelope.model_validate(item) for item in envelopes]
    serialized = [env.model_dump(mode="json", by_alias=True) for env in parsed]
    _round_trip(envelopes, serialized, fixture_name)


def test_every_fixture_file_is_covered() -> None:
    """A fixture file that is not round-tripped here silently rots."""
    on_disk = {str(path.relative_to(FIXTURES_DIR)) for path in FIXTURES_DIR.rglob("*.json")}
    covered = set(FIXTURE_MODELS) | set(V2_SINGLE_ENVELOPE_FIXTURES) | set(V2_ENVELOPE_LIST_FIXTURES)
    assert on_disk == covered, (
        f"fixture files without a model mapping: {on_disk - covered}; mappings without a file: {covered - on_disk}"
    )


def test_v2_envelopes_declare_protocol_version_three() -> None:
    for fixture_name in V2_SINGLE_ENVELOPE_FIXTURES:
        raw = json.loads((FIXTURES_DIR / fixture_name).read_text())
        assert raw["protocolVersion"] == PROTOCOL_VERSION


def test_envelope_protocol_version_is_mandatory() -> None:
    raw = json.loads((FIXTURES_DIR / "v2/envelope-minimal.json").read_text())
    without_version = {key: value for key, value in raw.items() if key != "protocolVersion"}
    with pytest.raises(ValidationError, match="protocolVersion"):
        RelayEnvelope.model_validate(without_version)


def test_envelope_rejects_snake_case_field_names() -> None:
    """v2 is camelCase-only (envelope.ts .strict()); snake keys must fail loud."""
    raw = json.loads((FIXTURES_DIR / "v2/envelope-minimal.json").read_text())
    snake = {
        "id": raw["id"],
        "protocol_version": raw["protocolVersion"],
        "workspace_id": raw["workspaceId"],
        "actor_id": raw["actorId"],
        "device_id": raw["deviceId"],
        "hlc": raw["hlc"],
        "affected_node_ids": raw["affectedNodeIds"],
        "op_type": raw["opType"],
        "timestamp": raw["timestamp"],
        "payload": raw["payload"],
    }
    with pytest.raises(ValidationError):
        RelayEnvelope.model_validate(snake)


def test_ws_frames_declare_framing_version_two() -> None:
    hello = json.loads((FIXTURES_DIR / "ws-hello.json").read_text())
    ops = json.loads((FIXTURES_DIR / "ws-ops.json").read_text())
    assert hello["wsProtocolVersion"] == WS_PROTOCOL_VERSION
    assert ops["wsProtocolVersion"] == WS_PROTOCOL_VERSION
    assert "protocolVersion" not in hello
    assert "protocolVersion" not in ops
