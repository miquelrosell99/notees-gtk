"""Pydantic wire models for the Notees relay sync protocol v2.

Mirrors ``v2/packages/protocol/src/envelope.ts`` and the v2 ``WIRE.md``
(camelCase everywhere on the wire, ``protocolVersion: 2`` mandatory,
first-class ``deviceId``, optional ``client`` provenance claim, and the M3
E2EE payload slot ``{"$e": {iv, ct}}``). The envelope schema accepts only
camelCase keys (``populate_by_name`` is deliberately off) and forbids extra
keys — the fail-loud parity of the zod ``.strict()`` schemas. ``seq`` never
appears inside an envelope; it rides on catch-up responses and WS frames.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from notees_gtk.core.protocol.clock import Clock, Hlc
from notees_gtk.core.protocol.ids import new_uuid7
from notees_gtk.core.protocol.op_types import KNOWN_OP_TYPES
from notees_gtk.core.protocol.payloads import validate_payload

__all__ = [
    "MAX_ENVELOPE_SIZE_BYTES",
    "PROTOCOL_VERSION",
    "WS_PROTOCOL_VERSION",
    "BatchRequest",
    "BatchResponse",
    "CatchUpPaginatedResponse",
    "CatchUpRequest",
    "RelayEnvelope",
    "WsAckMessage",
    "WsBatchMessage",
    "WsErrorMessage",
    "WsHelloMessage",
    "WsOpsMessage",
    "check_payload_size",
    "new_envelope",
]

#: Version of the relay envelope schema (``envelope.ts`` PROTOCOL_VERSION).
#: Mandatory on every envelope; receivers fail loud on a newer version.
PROTOCOL_VERSION = 2

#: Version of the WebSocket message framing (``WIRE.md`` §2). Versioned
#: independently of PROTOCOL_VERSION; a newer framing version fails loud.
WS_PROTOCOL_VERSION = 2

#: Maximum serialized payload size per envelope (WIRE.md §3).
MAX_ENVELOPE_SIZE_BYTES = 1_000_000

#: Provenance claim shape from ``envelope.ts`` clientClaimSchema.
_CLIENT_CLAIM_RE = re.compile(r"^[a-z][a-z0-9-]*(:[a-z0-9-]+)?$")


def check_payload_size(payload: dict[str, Any]) -> None:
    """Reject a payload whose JSON encoding exceeds the envelope size limit.

    Args:
        payload: Operation payload to be sent inside an envelope.

    Raises:
        ValueError: If the UTF-8 byte count of the JSON-serialized payload
            exceeds ``MAX_ENVELOPE_SIZE_BYTES``.
    """
    if len(json.dumps(payload).encode("utf-8")) > MAX_ENVELOPE_SIZE_BYTES:
        raise ValueError(f"Payload exceeds maximum payload size of {MAX_ENVELOPE_SIZE_BYTES} bytes")


class RelayEnvelope(BaseModel):
    """Routing metadata plus a plaintext or E2EE-slot operation payload.

    Envelope fields are unencrypted so the server can route operations, enforce
    permissions, and serve catch-up queries without accessing payload contents.
    The wire format is camelCase-only and rejects unknown keys, matching the
    zod ``envelopeSchema`` (``.strict()``) in ``v2/packages/protocol``.
    """

    model_config = ConfigDict(alias_generator=to_camel, extra="forbid")

    id: str = Field(default_factory=new_uuid7)
    protocol_version: int
    workspace_id: str
    actor_id: str
    device_id: str = Field(min_length=1, max_length=128)
    client: str | None = None
    hlc: Hlc
    affected_node_ids: list[str] = Field(default_factory=list, max_length=10_000)
    op_type: str = Field(min_length=1)
    timestamp: datetime
    payload: dict[str, Any]

    @field_validator("protocol_version")
    @classmethod
    def _validate_protocol_version(cls, value: int) -> int:
        """Fail loudly on missing or non-v2 envelope versions (WIRE.md §3)."""
        if value > PROTOCOL_VERSION:
            raise ValueError(
                f"Unsupported protocol_version {value}: this client speaks v{PROTOCOL_VERSION} "
                "and must fail loud on newer envelopes"
            )
        if value != PROTOCOL_VERSION:
            raise ValueError(f"Unsupported protocol_version {value}: this client speaks v{PROTOCOL_VERSION}")
        return value

    @field_validator("client")
    @classmethod
    def _validate_client_claim(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) > 64 or not _CLIENT_CLAIM_RE.match(value):
            raise ValueError("client must look like 'web', 'cli', or 'agent:<id>'")
        return value

    @field_validator("timestamp")
    @classmethod
    def _normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("payload")
    @classmethod
    def _validate_payload_shape(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Validate the M3 E2EE slot shape only — contents stay opaque.

        A payload carrying the ``$e`` key must be exactly ``{"$e": {iv, ct}}``
        with string ``iv``/``ct``; anything else is a plaintext object, free-form.
        """
        if "$e" not in value:
            return value
        if set(value) != {"$e"}:
            raise ValueError('encrypted payload must be exactly {"$e": {...}} with no sibling keys')
        slot = value["$e"]
        if not isinstance(slot, dict) or set(slot) != {"iv", "ct"}:
            raise ValueError('encrypted payload slot must have shape {"$e": {iv, ct}}')
        if not isinstance(slot["iv"], str) or not isinstance(slot["ct"], str):
            raise ValueError('encrypted payload "iv" and "ct" must be strings')
        return value

    def known_op_type(self) -> bool:
        """Return whether ``op_type`` is in the v2 producer registry.

        The envelope accepts any non-empty op type (``envelope.ts`` validates
        only ``min(1)``); unknown types are rejected at relay ingest with a 422
        ``validation_failed``. Producers should consult ``KNOWN_OP_TYPES``.
        """
        return self.op_type in KNOWN_OP_TYPES


class BatchRequest(BaseModel):
    """A batch of operation envelopes submitted by a client (WIRE.md §1)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    envelopes: list[RelayEnvelope]


class BatchResponse(BaseModel):
    """Acknowledgement of a committed batch: ``{"savedCount", "savedIds"}``.

    Duplicate envelope ids are silently ignored server-side (idempotent
    retry), so ``saved_ids`` may omit ids that were sent.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    saved_count: int
    saved_ids: list[str]


class CatchUpRequest(BaseModel):
    """Request operations for a workspace newer than the seq cursor.

    ``after_seq`` is the server-assigned sequence number of the last envelope
    the client has applied (0 for a cold start); it is an exclusive lower
    bound. The server seq is the sole ordering authority. Wire fields are
    camelCase (WIRE.md §1).
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    workspace_id: str
    after_seq: int = Field(default=0, ge=0)
    limit: int = 1000


class CatchUpPaginatedResponse(BaseModel):
    """A paginated page of operation envelopes for catch-up sync.

    Own fields are camelCase on the wire (WIRE.md §1); the nested envelopes
    are v2 envelopes. ``restore_epoch`` change ⇒ wipe local state and resync.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    envelopes: list[RelayEnvelope]
    next_after_seq: int | None = None
    has_more: bool = False
    restore_epoch: int = 0
    # Number of envelopes with seq greater than the request's after_seq
    # (including this page) — lets clients render global catch-up progress.
    total_remaining: int = 0


def _validate_framing_version(value: int) -> int:
    """Fail loudly on newer WS framing versions (WIRE.md §2)."""
    if value > WS_PROTOCOL_VERSION:
        raise ValueError(f"Unsupported wsProtocolVersion {value}: this client speaks WS framing v{WS_PROTOCOL_VERSION}")
    return value


class WsHelloMessage(BaseModel):
    """Server greeting sent right after the relay WebSocket connects.

    Advertises the workspace restore epoch plus the highest server-assigned
    sequence number, which clients compare against their stored seq cursor to
    decide whether a catch-up is needed before accepting live ``ops``.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    type: Literal["hello"] = "hello"
    ws_protocol_version: int = WS_PROTOCOL_VERSION
    restore_epoch: int = 0
    latest_seq: int = 0

    @field_validator("ws_protocol_version")
    @classmethod
    def _check_framing_version(cls, value: int) -> int:
        return _validate_framing_version(value)


class WsOpsMessage(BaseModel):
    """Batch of envelopes broadcast to workspace subscribers (WIRE.md §2).

    ``seqs`` maps envelope id → server-assigned seq so receivers can advance
    their seq cursor from live frames alone. It lives on the frame, not inside
    the envelopes — the envelope schema carries no ``seq``.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    type: Literal["ops"] = "ops"
    ws_protocol_version: int = WS_PROTOCOL_VERSION
    envelopes: list[RelayEnvelope]
    seqs: dict[str, int] = Field(default_factory=dict)

    @field_validator("ws_protocol_version")
    @classmethod
    def _check_framing_version(cls, value: int) -> int:
        return _validate_framing_version(value)


class WsBatchMessage(BaseModel):
    """Client → server frame submitting a batch over the socket.

    Same path and limits as HTTP ``POST /batch``; the server answers with an
    ``ack`` frame. Carries no framing version (WIRE.md §2).
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    type: Literal["batch"] = "batch"
    envelopes: list[RelayEnvelope]


class WsAckMessage(BaseModel):
    """Server → client confirmation listing the ids it stored for the batch."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    type: Literal["ack"] = "ack"
    saved_ids: list[str] = Field(default_factory=list)


class WsErrorMessage(BaseModel):
    """Server → client error frame (bad frame, invalid batch, ...)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    type: Literal["error"] = "error"
    message: str


def _wall_clock_ms() -> int:
    """Return the current wall-clock time in milliseconds."""
    return time.time_ns() // 1_000_000


def new_envelope(
    *,
    workspace_id: str,
    actor_id: str,
    device_id: str,
    op_type: str,
    payload: dict[str, Any],
    clock: Clock,
    client: str | None = "gtk",
    affected_node_ids: Sequence[str] = (),
    now_ms: Callable[[], int] = _wall_clock_ms,
) -> RelayEnvelope:
    """Build a new outbound v2 envelope, stamping id, HLC, and timestamp.

    Args:
        workspace_id: Workspace the operation belongs to.
        actor_id: Public id of the producing user.
        device_id: Stable per-install device id (``config_store.ensure_device_id()``);
            the HLC clock should be bound to the same id.
        op_type: Operation type; producers should stick to ``KNOWN_OP_TYPES``
            (unknown types are rejected at ingest with 422).
        payload: Operation payload; rejected when it exceeds the serialized
            size limit (WIRE.md §3) or deviates from the op's strict payload
            schema (the relay's 422 ``validation_failed`` gate, client-side).
            May be the E2EE slot ``{"$e": ...}`` (shape-checked only).
        clock: Local HLC clock; advanced with ``now_ms()`` to stamp causality.
        client: Optional provenance claim (``"gtk"`` by default).
        affected_node_ids: Node ids the operation touches.
        now_ms: Wall-clock milliseconds source for the HLC advance.

    Returns:
        A validated :class:`RelayEnvelope` ready for submission.
    """
    check_payload_size(payload)
    if op_type not in KNOWN_OP_TYPES:
        raise ValueError(f"Unknown op_type: {op_type!r}")
    if "$e" not in payload:
        # Producer-side half of the relay's 422 gate: the strict op payload
        # schemas (op-types.ts parity) reject retired/renamed keys before the
        # envelope can enter the outbox.
        validate_payload(op_type, payload)
    data: dict[str, Any] = {
        "protocolVersion": PROTOCOL_VERSION,
        "workspaceId": workspace_id,
        "actorId": actor_id,
        "deviceId": device_id,
        "opType": op_type,
        "payload": payload,
        "hlc": clock.advance(now_ms()).model_dump(),
        "affectedNodeIds": list(affected_node_ids),
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if client is not None:
        data["client"] = client
    return RelayEnvelope.model_validate(data)
