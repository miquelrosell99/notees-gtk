"""Validate protocol fixtures against the GTK client's wire models.

Each JSON file in ``tests/fixtures/`` is parsed by the model that owns that
wire shape, serialized back with the wire casing, and compared field-by-field.
If a model and its fixture drift (renamed field, changed default, changed
casing), this test fails — it is this repo's half of the protocol contract
described in the Notees ``protocol/SPEC.md``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from notees_gtk.core.protocol.models import (
    PROTOCOL_VERSION,
    BatchRequest,
    CatchUpPaginatedResponse,
    CatchUpRequest,
    RelayEnvelope,
    WsHelloMessage,
    WsOpsMessage,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# fixture file -> (model, serialize with camelCase aliases)
FIXTURE_MODELS: dict[str, tuple[type[BaseModel], bool]] = {
    "envelope-minimal.json": (RelayEnvelope, True),
    "batch-request.json": (BatchRequest, True),
    "catch-up-request.json": (CatchUpRequest, False),
    "catch-up-response.json": (CatchUpPaginatedResponse, True),
    "ws-hello.json": (WsHelloMessage, True),
    "ws-ops.json": (WsOpsMessage, True),
}


def _round_trip(fixture_path: Path, model: type[BaseModel], by_alias: bool) -> None:
    raw = json.loads(fixture_path.read_text())
    parsed = model.model_validate(raw)
    serialized = parsed.model_dump(mode="json", by_alias=by_alias)
    assert serialized == raw, (
        f"{fixture_path.name} drifted from {model.__name__}:\n"
        f"fixture:    {json.dumps(raw, sort_keys=True)}\n"
        f"serialized: {json.dumps(serialized, sort_keys=True)}"
    )


@pytest.mark.parametrize("fixture_name", sorted(FIXTURE_MODELS))
def test_fixture_round_trips_through_model(fixture_name: str) -> None:
    model, by_alias = FIXTURE_MODELS[fixture_name]
    fixture_path = FIXTURES_DIR / fixture_name
    assert fixture_path.exists(), f"missing fixture: {fixture_path}"
    _round_trip(fixture_path, model, by_alias)


def test_every_fixture_file_is_covered() -> None:
    """A fixture file that is not round-tripped here silently rots."""
    on_disk = {path.name for path in FIXTURES_DIR.glob("*.json")}
    assert on_disk == set(FIXTURE_MODELS), (
        f"fixture files without a model mapping: {on_disk - set(FIXTURE_MODELS)}; "
        f"mappings without a file: {set(FIXTURE_MODELS) - on_disk}"
    )


def test_envelope_protocol_version_defaults_to_current() -> None:
    raw = json.loads((FIXTURES_DIR / "envelope-minimal.json").read_text())
    assert raw["protocolVersion"] == PROTOCOL_VERSION

    without_version = {key: value for key, value in raw.items() if key != "protocolVersion"}
    parsed = RelayEnvelope.model_validate(without_version)
    assert parsed.protocol_version == PROTOCOL_VERSION


def test_envelope_accepts_snake_case_field_names() -> None:
    """v1 WS framing emitted snake_case envelopes; receivers must accept both."""
    raw = json.loads((FIXTURES_DIR / "envelope-minimal.json").read_text())
    snake = {
        "id": raw["id"],
        "protocol_version": raw["protocolVersion"],
        "workspace_id": raw["workspaceId"],
        "actor_id": raw["actorId"],
        "hlc": raw["hlc"],
        "affected_node_ids": raw["affectedNodeIds"],
        "op_type": raw["opType"],
        "timestamp": raw["timestamp"],
        "payload": raw["payload"],
    }
    parsed = RelayEnvelope.model_validate(snake)
    assert parsed.model_dump(mode="json", by_alias=True) == raw
