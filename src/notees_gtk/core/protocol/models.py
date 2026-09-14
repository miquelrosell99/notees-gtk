"""Pydantic wire models for the Notees relay sync protocol.

Mirrors ``app/relay/models.py`` from the Notees backend (Pydantic v2,
``populate_by_name=True``, camelCase aliases where the wire uses camelCase).
``CatchUpRequest`` and ``CatchUpPaginatedResponse`` stay snake_case: SPEC §4
keeps request/response bodies snake_case unless they contain envelopes.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from notees_gtk.core.protocol.clock import Clock, Hlc
from notees_gtk.core.protocol.ids import new_uuid7
from notees_gtk.core.protocol.op_types import KNOWN_OP_TYPES

__all__ = [
    "MAX_ENVELOPE_SIZE_BYTES",
    "PROTOCOL_VERSION",
    "WS_PROTOCOL_VERSION",
    "BatchRequest",
    "CatchUpPaginatedResponse",
    "CatchUpRequest",
    "RelayEnvelope",
    "WsHelloMessage",
    "WsOpsMessage",
    "check_payload_size",
    "new_envelope",
]

#: Version of the relay sync protocol described in the Notees ``protocol/SPEC.md``.
#: Bump only on breaking wire changes; additive optional fields do not
#: require a bump (see the versioning policy in the spec).
PROTOCOL_VERSION = 1

#: Version of the WebSocket message framing (hello/ops/ack/error). Kept separate
#: from PROTOCOL_VERSION (the envelope schema); changing WS message shapes bumps
#: only this version.
WS_PROTOCOL_VERSION = 2

#: Maximum serialized payload size per envelope (SPEC §6).
MAX_ENVELOPE_SIZE_BYTES = 1_000_000


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
    """Routing metadata plus a plaintext operation payload.

    Envelope fields are unencrypted so the server can route operations, enforce
    permissions, and serve catch-up queries without accessing payload contents.
    The wire format uses camelCase keys to match the other clients; the
    snake_case names remain valid for local callers and tests.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str = Field(default_factory=new_uuid7)
    protocol_version: int = Field(default=PROTOCOL_VERSION)
    workspace_id: str
    actor_id: str
    hlc: Hlc
    affected_node_ids: list[str] = Field(default_factory=list)
    op_type: str
    timestamp: datetime | None = None
    payload: dict[str, Any]

    @field_validator("op_type")
    @classmethod
    def _validate_op_type(cls, value: str) -> str:
        if value not in KNOWN_OP_TYPES:
            raise ValueError(f"Unknown op_type: {value!r}")
        return value

    @field_validator("timestamp")
    @classmethod
    def _normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)


class BatchRequest(BaseModel):
    """A batch of operation envelopes submitted by a client."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    envelopes: list[RelayEnvelope]


class CatchUpRequest(BaseModel):
    """Request operations for a workspace newer than the given seq cursor.

    ``after_seq`` is the server-assigned sequence number of the last envelope
    the client has applied (0 for a cold start). Client-supplied HLCs are no
    longer accepted as the catch-up cursor: the server sequence is the
    authoritative order. Wire fields are snake_case (SPEC §4).
    """

    workspace_id: str
    after_seq: int = Field(default=0, ge=0)
    limit: int = 1000


class CatchUpPaginatedResponse(BaseModel):
    """A paginated page of operation envelopes for catch-up sync.

    Own fields stay snake_case on the wire (SPEC §4); only the nested
    envelopes are camelCase.
    """

    envelopes: list[RelayEnvelope]
    next_after_seq: int | None = None
    has_more: bool = False
    restore_epoch: int = 0
    # Number of envelopes with seq greater than the request's after_seq
    # (including this page) — lets clients render global catch-up progress.
    total_remaining: int = 0


class WsHelloMessage(BaseModel):
    """Server greeting sent right after the relay WebSocket connects.

    Lets clients fail fast on an incompatible framing version instead of
    mid-sync, and advertises the workspace restore epoch plus the highest
    server-assigned sequence number, which clients compare against their
    stored seq cursor to decide whether a catch-up is needed before
    accepting live ``ops`` (see ``protocol/SPEC.md`` §5).
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    type: Literal["hello"] = "hello"
    protocol_version: int = WS_PROTOCOL_VERSION
    restore_epoch: int = 0
    latest_seq: int = 0


class WsOpsMessage(BaseModel):
    """Batch of envelopes broadcast to workspace subscribers.

    One frame per saved batch instead of one frame per envelope: receivers get
    an atomic batch to apply, and a `type` discriminator removes the need to
    shape-sniff bare envelopes against control messages.

    ``seqs`` maps envelope id → server-assigned seq so receivers can advance
    their seq cursor from live frames alone. It lives on the frame, not inside
    the envelopes, so the envelope schema (protocolVersion 1) stays unchanged.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    type: Literal["ops"] = "ops"
    protocol_version: int = WS_PROTOCOL_VERSION
    envelopes: list[RelayEnvelope]
    seqs: dict[str, int] = Field(default_factory=dict)


def _wall_clock_ms() -> int:
    """Return the current wall-clock time in milliseconds."""
    return time.time_ns() // 1_000_000


def new_envelope(
    *,
    workspace_id: str,
    actor_id: str,
    op_type: str,
    payload: dict[str, Any],
    clock: Clock,
    affected_node_ids: Sequence[str] = (),
    now_ms: Callable[[], int] = _wall_clock_ms,
) -> RelayEnvelope:
    """Build a new outbound envelope, stamping id, HLC, and timestamp.

    Args:
        workspace_id: Workspace the operation belongs to.
        actor_id: Public id of the producing user/device.
        op_type: Operation type; must be in ``KNOWN_OP_TYPES``.
        payload: Operation payload; rejected when it exceeds the serialized
            size limit (SPEC §6).
        clock: Local HLC clock; advanced with ``now_ms()`` to stamp causality.
        affected_node_ids: Node ids the operation touches.
        now_ms: Wall-clock milliseconds source for the HLC advance.

    Returns:
        A validated :class:`RelayEnvelope` ready for submission.
    """
    check_payload_size(payload)
    return RelayEnvelope(
        workspace_id=workspace_id,
        actor_id=actor_id,
        op_type=op_type,
        payload=payload,
        hlc=clock.advance(now_ms()),
        affected_node_ids=list(affected_node_ids),
        timestamp=datetime.now(UTC),
    )
