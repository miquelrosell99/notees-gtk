"""Validation tests for the v2 relay wire models and ``new_envelope``.

Covers the WIRE.md §3 / envelope.ts invariants: ``protocolVersion: 2``
mandatory (missing rejected, newer fails loud), ``deviceId`` (1–128) and
``timestamp`` mandatory, the M3 E2EE slot ``{"$e": {iv, ct}}`` shape,
camelCase-only keys, extra keys forbidden, and the ``wsProtocolVersion``
framing on WS hello/ops frames.
"""

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
    WsAckMessage,
    WsBatchMessage,
    WsErrorMessage,
    WsHelloMessage,
    WsOpsMessage,
    check_payload_size,
    new_envelope,
)
from notees_gtk.core.protocol.op_types import KNOWN_OP_TYPES

VALID_ENVELOPE: dict[str, Any] = {
    "id": "0192a000-0000-7000-8000-0000000000f1",
    "protocolVersion": 2,
    "workspaceId": "0192a000-0000-7000-8000-000000000001",
    "actorId": "0192a000-0000-7000-8000-000000000002",
    "deviceId": "gtk-device-0001",
    "client": "gtk",
    "hlc": {"physical": 1727200000000, "logical": 0},
    "affectedNodeIds": ["0192a000-0000-7000-8000-000000000010"],
    "opType": "object.create",
    "timestamp": "2026-09-24T12:00:00.000Z",
    "payload": {"objectId": "0192a000-0000-7000-8000-000000000010", "nodeType": "page", "classIds": []},
}


class TestOpTypeRegistry:
    def test_registry_matches_v2_op_types_ts(self) -> None:
        """Exact parity with OP_PAYLOAD_SCHEMAS keys in v2 op-types.ts."""
        assert (
            frozenset(
                {
                    "object.create",
                    "object.update",
                    "object.delete",
                    "object.move",
                    "class.create",
                    "class.update",
                    "class.delete",
                    "class.setExtends",
                    "propertySchema.create",
                    "propertySchema.update",
                    "propertySchema.delete",
                    "property.set",
                    "property.unset",
                    "asset.attach",
                    "asset.detach",
                    "collection.member.add",
                    "collection.member.remove",
                }
            )
            == KNOWN_OP_TYPES
        )

    @pytest.mark.parametrize("op_type", sorted(KNOWN_OP_TYPES))
    def test_known_op_types_accepted(self, op_type: str) -> None:
        raw = {**VALID_ENVELOPE, "opType": op_type}
        envelope = RelayEnvelope.model_validate(raw)
        assert envelope.op_type == op_type
        assert envelope.known_op_type() is True

    def test_empty_op_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RelayEnvelope.model_validate({**VALID_ENVELOPE, "opType": ""})

    def test_unknown_op_type_passes_envelope_fails_ingest(self) -> None:
        """envelope.ts validates only min(1); the relay rejects unknown ops at
        ingest with 422 validation_failed (quarantine path). The wire model
        stays permissive so older envelopes remain parseable."""
        envelope = RelayEnvelope.model_validate({**VALID_ENVELOPE, "opType": "legacy.nodeCreate"})
        assert envelope.known_op_type() is False


class TestProtocolVersion:
    def test_v2_accepted(self) -> None:
        assert RelayEnvelope.model_validate(VALID_ENVELOPE).protocol_version == 2

    def test_missing_version_rejected(self) -> None:
        raw = {key: value for key, value in VALID_ENVELOPE.items() if key != "protocolVersion"}
        with pytest.raises(ValidationError, match="protocolVersion"):
            RelayEnvelope.model_validate(raw)

    def test_v3_rejected_fail_loud(self) -> None:
        with pytest.raises(ValidationError, match="Unsupported protocol_version 3"):
            RelayEnvelope.model_validate({**VALID_ENVELOPE, "protocolVersion": 3})

    def test_older_version_rejected(self) -> None:
        """v2 is a clean break: v1 envelopes are not parseable."""
        with pytest.raises(ValidationError, match="Unsupported protocol_version 1"):
            RelayEnvelope.model_validate({**VALID_ENVELOPE, "protocolVersion": 1})


class TestDeviceId:
    def test_missing_device_id_rejected(self) -> None:
        raw = {key: value for key, value in VALID_ENVELOPE.items() if key != "deviceId"}
        with pytest.raises(ValidationError, match="deviceId|device_id"):
            RelayEnvelope.model_validate(raw)

    def test_empty_device_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RelayEnvelope.model_validate({**VALID_ENVELOPE, "deviceId": ""})

    def test_device_id_over_128_chars_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RelayEnvelope.model_validate({**VALID_ENVELOPE, "deviceId": "d" * 129})

    def test_device_id_at_128_chars_accepted(self) -> None:
        envelope = RelayEnvelope.model_validate({**VALID_ENVELOPE, "deviceId": "d" * 128})
        assert envelope.device_id == "d" * 128


class TestClientClaim:
    def test_client_optional(self) -> None:
        raw = {key: value for key, value in VALID_ENVELOPE.items() if key != "client"}
        assert RelayEnvelope.model_validate(raw).client is None

    @pytest.mark.parametrize("client", ["gtk", "web", "cli", "agent:scraper-1"])
    def test_valid_claims_accepted(self, client: str) -> None:
        envelope = RelayEnvelope.model_validate({**VALID_ENVELOPE, "client": client})
        assert envelope.client == client

    @pytest.mark.parametrize("client", ["GTK", "9lives", "has space", "bad:Claim", "x" * 65])
    def test_invalid_claims_rejected(self, client: str) -> None:
        with pytest.raises(ValidationError, match="client"):
            RelayEnvelope.model_validate({**VALID_ENVELOPE, "client": client})


class TestTimestampValidation:
    def test_missing_timestamp_rejected(self) -> None:
        raw = {key: value for key, value in VALID_ENVELOPE.items() if key != "timestamp"}
        with pytest.raises(ValidationError, match="timestamp"):
            RelayEnvelope.model_validate(raw)

    def test_naive_timestamp_rejected(self) -> None:
        raw = {**VALID_ENVELOPE, "timestamp": "2026-09-24T12:00:00"}
        with pytest.raises(ValidationError, match="timezone-aware"):
            RelayEnvelope.model_validate(raw)

    def test_non_utc_timestamp_normalized_to_utc(self) -> None:
        raw = {**VALID_ENVELOPE, "timestamp": "2026-09-24T17:30:00+05:30"}
        parsed = RelayEnvelope.model_validate(raw)
        assert parsed.timestamp == datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)
        assert parsed.model_dump(mode="json", by_alias=True)["timestamp"] == "2026-09-24T12:00:00Z"


class TestHlcValidation:
    def test_negative_physical_rejected(self) -> None:
        raw = {**VALID_ENVELOPE, "hlc": {"physical": -1, "logical": 0}}
        with pytest.raises(ValidationError):
            RelayEnvelope.model_validate(raw)

    def test_negative_logical_rejected(self) -> None:
        raw = {**VALID_ENVELOPE, "hlc": {"physical": 0, "logical": -1}}
        with pytest.raises(ValidationError):
            RelayEnvelope.model_validate(raw)

    def test_hlc_extra_keys_rejected(self) -> None:
        """hlcSchema is .strict() — only physical/logical."""
        raw = {**VALID_ENVELOPE, "hlc": {"physical": 0, "logical": 0, "wall": 1}}
        with pytest.raises(ValidationError):
            RelayEnvelope.model_validate(raw)

    def test_hlc_instance_accepted(self) -> None:
        raw = {**VALID_ENVELOPE, "hlc": Hlc(physical=1, logical=2)}
        assert RelayEnvelope.model_validate(raw).hlc == Hlc(physical=1, logical=2)


class TestEncryptedPayloadSlot:
    def test_encrypted_slot_accepted(self) -> None:
        raw = {**VALID_ENVELOPE, "payload": {"$e": {"iv": "aXY=", "ct": "Y3R4"}}}
        envelope = RelayEnvelope.model_validate(raw)
        assert envelope.payload == {"$e": {"iv": "aXY=", "ct": "Y3R4"}}

    def test_slot_with_sibling_keys_rejected(self) -> None:
        raw = {**VALID_ENVELOPE, "payload": {"$e": {"iv": "aXY=", "ct": "Y3R4"}, "extra": 1}}
        with pytest.raises(ValidationError, match="sibling"):
            RelayEnvelope.model_validate(raw)

    def test_slot_missing_ct_rejected(self) -> None:
        raw = {**VALID_ENVELOPE, "payload": {"$e": {"iv": "aXY="}}}
        with pytest.raises(ValidationError, match='{"\\$e": {iv, ct}}'):
            RelayEnvelope.model_validate(raw)

    def test_slot_non_string_members_rejected(self) -> None:
        raw = {**VALID_ENVELOPE, "payload": {"$e": {"iv": 1, "ct": "Y3R4"}}}
        with pytest.raises(ValidationError, match="strings"):
            RelayEnvelope.model_validate(raw)

    def test_plaintext_payload_untouched(self) -> None:
        payload = {"objectId": "x", "name": "$e not a slot", "nested": {"iv": 1, "ct": 2}}
        envelope = RelayEnvelope.model_validate({**VALID_ENVELOPE, "payload": payload})
        assert envelope.payload == payload


class TestExtraKeysForbidden:
    def test_seq_inside_envelope_rejected(self) -> None:
        """seq rides on catch-up responses / WS frames — never inside an envelope."""
        with pytest.raises(ValidationError):
            RelayEnvelope.model_validate({**VALID_ENVELOPE, "seq": 7})

    def test_unknown_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RelayEnvelope.model_validate({**VALID_ENVELOPE, "futureField": True})


class TestCamelCaseOnly:
    def test_snake_case_keys_rejected(self) -> None:
        snake = {
            "id": VALID_ENVELOPE["id"],
            "protocol_version": 2,
            "workspace_id": VALID_ENVELOPE["workspaceId"],
            "actor_id": VALID_ENVELOPE["actorId"],
            "device_id": VALID_ENVELOPE["deviceId"],
            "hlc": VALID_ENVELOPE["hlc"],
            "affected_node_ids": VALID_ENVELOPE["affectedNodeIds"],
            "op_type": VALID_ENVELOPE["opType"],
            "timestamp": VALID_ENVELOPE["timestamp"],
            "payload": VALID_ENVELOPE["payload"],
        }
        with pytest.raises(ValidationError):
            RelayEnvelope.model_validate(snake)


class TestAffectedNodeIds:
    def test_over_10000_rejected(self) -> None:
        raw = {**VALID_ENVELOPE, "affectedNodeIds": ["x"] * 10_001}
        with pytest.raises(ValidationError):
            RelayEnvelope.model_validate(raw)

    def test_defaults_to_empty(self) -> None:
        raw = {key: value for key, value in VALID_ENVELOPE.items() if key != "affectedNodeIds"}
        assert RelayEnvelope.model_validate(raw).affected_node_ids == []


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
            "device_id": "device-a",
            "op_type": "object.create",
            "payload": {"objectId": "n-1", "nodeType": "page"},
            "clock": Clock("device-a"),
            "now_ms": lambda: 1767225600000,
        }
        defaults.update(overrides)
        return new_envelope(**defaults)

    def test_stamps_uuid7_id(self) -> None:
        envelope = self._make()
        parsed = uuid.UUID(envelope.id)
        assert parsed.version == 7

    def test_stamps_device_id(self) -> None:
        assert self._make().device_id == "device-a"
        assert self._make(device_id="other-device").device_id == "other-device"

    def test_client_defaults_to_gtk(self) -> None:
        assert self._make().client == "gtk"

    def test_client_omitted_when_none(self) -> None:
        envelope = self._make(client=None)
        assert envelope.client is None
        assert "client" not in envelope.model_dump(mode="json", by_alias=True, exclude_none=True)

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
        assert envelope.timestamp.tzinfo is not None
        assert before - timedelta(seconds=1) <= envelope.timestamp <= after + timedelta(seconds=1)

    def test_affected_node_ids_sequence_accepted(self) -> None:
        envelope = self._make(affected_node_ids=("n-1", "n-2"))
        assert envelope.affected_node_ids == ["n-1", "n-2"]

    def test_rejects_unknown_op_type(self) -> None:
        """Producer-side fail-fast: the envelope model itself stays permissive."""
        with pytest.raises(ValueError, match="Unknown op_type"):
            self._make(op_type="node.updateContent")

    def test_rejects_oversized_payload(self) -> None:
        payload = {"data": "x" * (MAX_ENVELOPE_SIZE_BYTES + 1)}
        with pytest.raises(ValueError, match="exceeds maximum payload size"):
            self._make(payload=payload)


class TestWsFrames:
    def test_hello_framing_field_renamed(self) -> None:
        message = WsHelloMessage.model_validate(
            {"type": "hello", "wsProtocolVersion": 2, "restoreEpoch": 1, "latestSeq": 9}
        )
        assert message.ws_protocol_version == 2
        assert message.restore_epoch == 1
        assert message.latest_seq == 9
        assert "protocolVersion" not in message.model_dump(mode="json", by_alias=True)

    def test_ops_framing_field_renamed(self) -> None:
        envelope = RelayEnvelope.model_validate(VALID_ENVELOPE)
        message = WsOpsMessage.model_validate(
            {
                "type": "ops",
                "wsProtocolVersion": 2,
                "envelopes": [envelope.model_dump(by_alias=True)],
                "seqs": {envelope.id: 42},
            }
        )
        assert message.ws_protocol_version == 2
        assert message.seqs == {envelope.id: 42}
        assert message.envelopes[0].id == envelope.id

    def test_hello_framing_v3_rejected(self) -> None:
        with pytest.raises(ValidationError, match="wsProtocolVersion 3"):
            WsHelloMessage.model_validate({"type": "hello", "wsProtocolVersion": 3})

    def test_ops_framing_v3_rejected(self) -> None:
        with pytest.raises(ValidationError, match="wsProtocolVersion 3"):
            WsOpsMessage.model_validate({"type": "ops", "wsProtocolVersion": 3, "envelopes": [], "seqs": {}})

    def test_legacy_protocol_version_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            WsHelloMessage.model_validate({"type": "hello", "protocolVersion": 2})

    def test_batch_frame(self) -> None:
        envelope = RelayEnvelope.model_validate(VALID_ENVELOPE)
        message = WsBatchMessage.model_validate({"type": "batch", "envelopes": [envelope.model_dump(by_alias=True)]})
        assert message.envelopes[0].id == envelope.id
        assert "wsProtocolVersion" not in message.model_dump(mode="json", by_alias=True)

    def test_ack_frame(self) -> None:
        message = WsAckMessage.model_validate({"type": "ack", "savedIds": ["id-1", "id-2"]})
        assert message.saved_ids == ["id-1", "id-2"]

    def test_error_frame(self) -> None:
        message = WsErrorMessage.model_validate({"type": "error", "message": "boom"})
        assert message.message == "boom"
