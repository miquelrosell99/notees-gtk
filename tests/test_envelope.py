"""Validation tests for the relay wire models and ``new_envelope``."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from notees_gtk.core.protocol.clock import Clock, Hlc
from notees_gtk.core.protocol.models import (
    MAX_ENVELOPE_SIZE_BYTES,
    RelayEnvelope,
    check_payload_size,
    new_envelope,
)

VALID_ENVELOPE: dict[str, Any] = {
    "id": "018f0000-0000-7000-8000-000000000101",
    "protocolVersion": 1,
    "workspaceId": "018f0000-0000-7000-8000-000000000001",
    "actorId": "018f0000-0000-7000-8000-0000000000aa",
    "hlc": {"physical": 1767225600000, "logical": 0},
    "affectedNodeIds": ["018f0000-0000-7000-8000-0000000000b1"],
    "opType": "node.updateContent",
    "timestamp": "2026-01-01T00:00:00Z",
    "payload": {"nodeId": "018f0000-0000-7000-8000-0000000000b1", "content": "<p>Hello</p>"},
}


class TestOpTypeValidation:
    def test_unknown_op_type_rejected(self) -> None:
        raw = {**VALID_ENVELOPE, "opType": "bogus.op"}
        with pytest.raises(ValidationError, match="Unknown op_type"):
            RelayEnvelope.model_validate(raw)

    def test_known_op_types_accepted(self) -> None:
        for op_type in ["node.create", "node.updateContent", "plugin.op", "user.favorite.reorder"]:
            raw = {**VALID_ENVELOPE, "opType": op_type}
            assert RelayEnvelope.model_validate(raw).op_type == op_type


class TestHlcValidation:
    def test_negative_physical_rejected(self) -> None:
        raw = {**VALID_ENVELOPE, "hlc": {"physical": -1, "logical": 0}}
        with pytest.raises(ValidationError):
            RelayEnvelope.model_validate(raw)

    def test_negative_logical_rejected(self) -> None:
        raw = {**VALID_ENVELOPE, "hlc": {"physical": 0, "logical": -1}}
        with pytest.raises(ValidationError):
            RelayEnvelope.model_validate(raw)

    def test_hlc_instance_accepted(self) -> None:
        raw = {**VALID_ENVELOPE, "hlc": Hlc(physical=1, logical=2)}
        assert RelayEnvelope.model_validate(raw).hlc == Hlc(physical=1, logical=2)


class TestTimestampValidation:
    def test_naive_timestamp_rejected(self) -> None:
        raw = {**VALID_ENVELOPE, "timestamp": "2026-01-01T00:00:00"}
        with pytest.raises(ValidationError, match="timezone-aware"):
            RelayEnvelope.model_validate(raw)

    def test_non_utc_timestamp_normalized_to_utc(self) -> None:
        raw = {**VALID_ENVELOPE, "timestamp": "2026-01-01T05:30:00+05:30"}
        parsed = RelayEnvelope.model_validate(raw)
        assert parsed.timestamp == datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        assert parsed.model_dump(mode="json", by_alias=True)["timestamp"] == "2026-01-01T00:00:00Z"

    def test_timestamp_optional(self) -> None:
        raw = {key: value for key, value in VALID_ENVELOPE.items() if key != "timestamp"}
        assert RelayEnvelope.model_validate(raw).timestamp is None


class TestPayloadSize:
    def _payload_of_size(self, size_bytes: int) -> dict[str, Any]:
        empty = json.dumps({"data": ""})
        return {"data": "x" * (size_bytes - len(empty))}

    def test_payload_at_limit_accepted(self) -> None:
        check_payload_size(self._payload_of_size(MAX_ENVELOPE_SIZE_BYTES))

    def test_payload_over_limit_rejected(self) -> None:
        with pytest.raises(ValueError, match="exceeds maximum payload size"):
            check_payload_size(self._payload_of_size(MAX_ENVELOPE_SIZE_BYTES + 1))

    def test_size_counts_utf8_bytes(self) -> None:
        payload: dict[str, Any] = {"data": "é" * (MAX_ENVELOPE_SIZE_BYTES // 2)}
        with pytest.raises(ValueError):
            check_payload_size(payload)


class TestNewEnvelope:
    def _make(self, **overrides: Any) -> RelayEnvelope:
        defaults: dict[str, Any] = {
            "workspace_id": "ws-1",
            "actor_id": "actor-1",
            "op_type": "node.create",
            "payload": {"nodeId": "n-1", "kind": "page"},
            "clock": Clock("device-a"),
            "now_ms": lambda: 1767225600000,
        }
        defaults.update(overrides)
        return new_envelope(**defaults)

    def test_stamps_uuid7_id(self) -> None:
        envelope = self._make()
        parsed = uuid.UUID(envelope.id)
        assert parsed.version == 7

    def test_stamps_hlc_from_clock(self) -> None:
        clock = Clock("device-a")
        envelope = self._make(clock=clock, now_ms=lambda: 1767225600000)
        assert envelope.hlc == Hlc(physical=1767225600000, logical=0)
        second = self._make(clock=clock, now_ms=lambda: 1767225600000)
        assert second.hlc == Hlc(physical=1767225600000, logical=1)

    def test_stamps_utc_timestamp(self) -> None:
        before = datetime.now(UTC)
        envelope = self._make()
        after = datetime.now(UTC)
        assert envelope.timestamp is not None
        assert envelope.timestamp.tzinfo is not None
        assert before - timedelta(seconds=1) <= envelope.timestamp <= after + timedelta(seconds=1)

    def test_affected_node_ids_default_empty(self) -> None:
        assert self._make().affected_node_ids == []

    def test_affected_node_ids_sequence_accepted(self) -> None:
        envelope = self._make(affected_node_ids=("n-1", "n-2"))
        assert envelope.affected_node_ids == ["n-1", "n-2"]

    def test_protocol_version_defaults_to_one(self) -> None:
        assert self._make().protocol_version == 1

    def test_rejects_unknown_op_type(self) -> None:
        with pytest.raises(ValidationError, match="Unknown op_type"):
            self._make(op_type="bogus.op")

    def test_rejects_oversized_payload(self) -> None:
        payload = {"data": "x" * (MAX_ENVELOPE_SIZE_BYTES + 1)}
        with pytest.raises(ValueError, match="exceeds maximum payload size"):
            self._make(payload=payload)
