"""Tests for the E2EE payload-slot decryption port (core/protocol/crypto.py).

The byte-pinned fixture ``tests/fixtures/wire/envelope-encrypted.json`` is the
contract: its ``crypto.workspaceKey`` (b64u 32 bytes) opens both envelopes,
and the opened payloads must deep-equal the expected plaintexts (UTF-8 JSON of
the payload object, AAD = ``{id}.{workspaceId}``, tag-first ``ct``). The
negatives pin the tag-first layout — a port that feeds ``ct ‖ tag`` to the
GCM primitive (the WebCrypto seal layout) fails here.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from notees_gtk.core.protocol.crypto import (
    b64u_decode,
    decrypt_envelope,
    decrypt_payload,
    envelope_aad,
)
from notees_gtk.core.protocol.models import RelayEnvelope

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "wire" / "envelope-encrypted.json"

EXPECTED_PAYLOADS: dict[str, dict[str, Any]] = {
    "0192a000-0000-7000-8000-0000000000fa": {
        "objectId": "0192a000-0000-7000-8000-0000000000fc",
        "contentAst": [{"type": "text", "text": "Encrypted fixture page"}],
        "isEmbedded": False,
        "classIds": [],
    },
    "0192a000-0000-7000-8000-0000000000fb": {
        "objectId": "0192a000-0000-7000-8000-0000000000fc",
        "icon": "book",
    },
}


@pytest.fixture()
def fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text())


@pytest.fixture()
def workspace_key(fixture: dict[str, Any]) -> bytes:
    key = b64u_decode(fixture["crypto"]["workspaceKey"])
    assert len(key) == 32
    return key


@pytest.fixture()
def envelopes(fixture: dict[str, Any]) -> list[RelayEnvelope]:
    return [RelayEnvelope.model_validate(item) for item in fixture["envelopes"]]


def test_b64u_decode_unpadded_urlsafe() -> None:
    encoded = base64.urlsafe_b64encode(bytes([251, 255, 190, 239, 0, 1, 2, 3])).rstrip(b"=").decode()
    assert b64u_decode(encoded) == bytes([251, 255, 190, 239, 0, 1, 2, 3])


def test_fixture_key_opens_both_envelopes_to_the_expected_plaintexts(
    workspace_key: bytes,
    envelopes: list[RelayEnvelope],
) -> None:
    for envelope in envelopes:
        opened = decrypt_envelope(workspace_key, envelope)
        assert opened.payload == EXPECTED_PAYLOADS[envelope.id]


def test_decrypt_envelope_passes_plaintext_through(envelopes: list[RelayEnvelope]) -> None:
    """The isEncryptedPayload gate: a plaintext envelope is returned unchanged."""
    plain = envelopes[0].model_copy(update={"payload": {"objectId": "x"}})
    assert decrypt_envelope(b"\x00" * 32, plain) is plain


def test_wrong_aad_fails(workspace_key: bytes, envelopes: list[RelayEnvelope]) -> None:
    victim = envelopes[0]
    other_id = "0192a000-0000-7000-8000-0000000000ff"
    tampered = victim.model_copy(update={"id": other_id})
    with pytest.raises(ValueError, match="decryption failed"):
        decrypt_payload(workspace_key, envelope_aad(tampered), victim.payload["$e"])


def test_tampered_ciphertext_fails(workspace_key: bytes, envelopes: list[RelayEnvelope]) -> None:
    slot = envelopes[0].payload["$e"]
    raw = bytearray(b64u_decode(slot["ct"]))
    raw[-1] ^= 0xFF
    tampered = {"iv": slot["iv"], "ct": base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode()}
    with pytest.raises(ValueError, match="decryption failed"):
        decrypt_payload(workspace_key, envelope_aad(envelopes[0]), tampered)


def test_ct_tag_misread_fails(workspace_key: bytes, envelopes: list[RelayEnvelope]) -> None:
    """A port reading the slot as WebCrypto's ``ct ‖ tag`` fails the fixture."""
    slot = envelopes[0].payload["$e"]
    raw = b64u_decode(slot["ct"])
    misread = {
        "iv": slot["iv"],
        "ct": base64.urlsafe_b64encode(raw[16:] + raw[:16]).rstrip(b"=").decode(),
    }
    with pytest.raises(ValueError, match="decryption failed"):
        decrypt_payload(workspace_key, envelope_aad(envelopes[0]), misread)


def test_wrong_key_fails(fixture: dict[str, Any], envelopes: list[RelayEnvelope]) -> None:
    other = b64u_decode(fixture["crypto"]["workspaceKey"])
    wrong = bytes(byte ^ 0xFF for byte in other)
    with pytest.raises(ValueError, match="decryption failed"):
        decrypt_envelope(wrong, envelopes[0])
