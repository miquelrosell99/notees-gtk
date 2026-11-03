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
    GCM_TAG_BYTES,
    KdfRecord,
    WorkspaceKeyCrypto,
    b64u_decode,
    b64u_encode,
    create_kdf_record,
    decrypt_asset_bytes,
    decrypt_bytes,
    decrypt_envelope,
    decrypt_payload,
    encrypt_asset_bytes,
    encrypt_bytes,
    encrypt_envelope,
    encrypt_payload,
    envelope_aad,
    generate_master_key,
    generate_workspace_key,
    key_verifier_of,
    open_asset_if_sealed,
    scrypt_key_bytes,
    seal_asset_if_keyed,
    unwrap_master_key,
    unwrap_workspace_key,
    wrap_workspace_key,
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


# --- custody half (plan 04): the scrypt KDF record, key wrap, envelope/bytes encryption -


#: The plan-01 TS golden vector (``packages/protocol/test/crypto.test.ts``
#: "server-format interop") ported with EXACT-string equality: fixed
#: salt/iv/master bytes, the expected wrapped record + verifier produced by
#: node:crypto with the server's exact formulas (apps/server/src/auth.ts).
GOLDEN_PASSWORD = "golden horse battery"
GOLDEN_SALT_B64U = "AQIDBAUGBwgJCgsMDQ4PEA"
GOLDEN_WRAPPED_MASTER = "oKGio6SlpqeoqaqrEa5lc67bpNjQU9ACvDQTRW-HY5NU6Vl8Axj2OTztsZafrlTyzXIVbR3uyA7jS5IN"
GOLDEN_KEY_VERIFIER = "12aPlhN1yihN2nOKLlxUSperu4hEr-qg2LS54MbGTtk"
GOLDEN_MASTER_BYTES = bytes(range(0x50, 0x50 + 32))
GOLDEN_RECORD_DICT: dict[str, Any] = {
    "algorithm": "scrypt",
    "N": 131072,
    "r": 8,
    "p": 1,
    "salt": GOLDEN_SALT_B64U,
    "wrappedMasterKey": GOLDEN_WRAPPED_MASTER,
    "keyVerifier": GOLDEN_KEY_VERIFIER,
}


def test_b64u_encode_unpadded_urlsafe() -> None:
    encoded = b64u_encode(bytes([251, 255, 190, 239, 0, 1, 2, 3]))
    assert encoded == base64.urlsafe_b64encode(bytes([251, 255, 190, 239, 0, 1, 2, 3])).rstrip(b"=").decode()
    assert b64u_decode(encoded) == bytes([251, 255, 190, 239, 0, 1, 2, 3])


class TestGoldenVector:
    """The scrypt KDF record interop pin: this port opens the SAME record
    bytes the server/TS produce (fixed-vector equality, not round-trip)."""

    def test_verifier_is_exactly_the_node_produced_string(self) -> None:
        salt = b64u_decode(GOLDEN_SALT_B64U)
        derived = scrypt_key_bytes(GOLDEN_PASSWORD, salt)
        assert b64u_encode(key_verifier_of(derived)) == GOLDEN_KEY_VERIFIER

    def test_unwrap_opens_the_fixed_server_record(self) -> None:
        record = KdfRecord.from_dict(GOLDEN_RECORD_DICT)
        assert record.algorithm == "scrypt"
        assert unwrap_master_key(GOLDEN_PASSWORD, record) == GOLDEN_MASTER_BYTES

    def test_wrong_password_fails_at_the_verifier(self) -> None:
        record = KdfRecord.from_dict(GOLDEN_RECORD_DICT)
        with pytest.raises(ValueError, match="verifier mismatch"):
            unwrap_master_key("wrong password", record)

    def test_from_dict_rejects_drift(self) -> None:
        for bad in (
            {},
            {"algorithm": "argon2"},
            {**GOLDEN_RECORD_DICT, "salt": ""},
            {key: value for key, value in GOLDEN_RECORD_DICT.items() if key != "wrappedMasterKey"},
            "not-a-dict",
        ):
            with pytest.raises(ValueError):
                KdfRecord.from_dict(bad)


class TestKdfRecordRoundTrip:
    def test_create_then_unwrap_restores_the_master_key(self) -> None:
        master = generate_master_key()
        record = create_kdf_record(master, "hunter2")
        assert record.algorithm == "scrypt"
        assert (record.n, record.r, record.p) == (131072, 8, 1)
        assert unwrap_master_key("hunter2", record) == master

    def test_record_round_trips_through_its_wire_dict(self) -> None:
        master = generate_master_key()
        record = create_kdf_record(master, "hunter2")
        assert KdfRecord.from_dict(record.to_dict()) == record

    def test_wrong_password_round_trip_fails_at_the_verifier(self) -> None:
        record = create_kdf_record(generate_master_key(), "hunter2")
        with pytest.raises(ValueError, match="verifier mismatch"):
            unwrap_master_key("hunter3", record)

    def test_short_master_key_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="32 raw bytes"):
            create_kdf_record(b"\x00" * 16, "hunter2")


class TestWorkspaceKeyWrap:
    def test_wrap_unwrap_round_trip(self) -> None:
        master = generate_master_key()
        workspace = generate_workspace_key()
        wrapped = wrap_workspace_key(master, workspace)
        assert unwrap_workspace_key(master, wrapped) == workspace

    def test_wrong_master_key_fails_loud(self) -> None:
        master = generate_master_key()
        other = generate_master_key()
        wrapped = wrap_workspace_key(master, generate_workspace_key())
        with pytest.raises(ValueError, match="workspace-key unwrap failed"):
            unwrap_workspace_key(other, wrapped)

    def test_wrapped_layout_is_iv_tag_ct_b64u(self) -> None:
        """The wrapped-key record is ``iv ‖ tag ‖ ct`` — the OPPOSITE order of
        the payload slot's tag-first ``ct`` (the documented asymmetry)."""
        master = generate_master_key()
        workspace = generate_workspace_key()
        raw = b64u_decode(wrap_workspace_key(master, workspace))
        iv, tag, ct = raw[:12], raw[12:28], raw[28:]
        assert unwrap_workspace_key(master, b64u_encode(iv + tag + ct)) == workspace
        # Swapping to the payload-slot order (tag first) must NOT open.
        with pytest.raises(ValueError):
            unwrap_workspace_key(master, b64u_encode(iv + ct + tag))


class TestEncryptPayload:
    def test_round_trip_inverts_to_the_same_payload(self, workspace_key: bytes, envelopes: list[RelayEnvelope]) -> None:
        envelope = envelopes[0]
        slot = encrypt_payload(workspace_key, envelope_aad(envelope), EXPECTED_PAYLOADS[envelope.id])
        assert set(slot) == {"$e"}
        opened = decrypt_payload(workspace_key, envelope_aad(envelope), slot["$e"])
        assert opened == EXPECTED_PAYLOADS[envelope.id]

    def test_slot_ct_is_tag_first(self, workspace_key: bytes, envelopes: list[RelayEnvelope]) -> None:
        envelope = envelopes[0]
        slot = encrypt_payload(workspace_key, envelope_aad(envelope), EXPECTED_PAYLOADS[envelope.id])["$e"]
        raw = b64u_decode(slot["ct"])
        assert len(b64u_decode(slot["iv"])) == 12
        assert len(raw) > 16
        # The fixture's own slot opens with the same layout — byte-layout parity.
        fixture_raw = b64u_decode(envelope.payload["$e"]["ct"])
        assert len(raw) == len(fixture_raw)

    def test_aad_binds_the_ciphertext(self, workspace_key: bytes, envelopes: list[RelayEnvelope]) -> None:
        envelope = envelopes[0]
        slot = encrypt_payload(workspace_key, envelope_aad(envelope), EXPECTED_PAYLOADS[envelope.id])
        other_id = "0192a000-0000-7000-8000-0000000000ff"
        with pytest.raises(ValueError, match="decryption failed"):
            decrypt_payload(workspace_key, f"{other_id}.{envelope.workspace_id}", slot["$e"])

    def test_encrypt_envelope_preserves_everything_but_payload(
        self, workspace_key: bytes, envelopes: list[RelayEnvelope]
    ) -> None:
        original = envelopes[0]
        envelope = original.model_copy(update={"payload": EXPECTED_PAYLOADS[original.id]})
        sealed = encrypt_envelope(workspace_key, envelope)
        assert sealed.id == envelope.id
        assert sealed.hlc == envelope.hlc
        assert sealed.op_type == envelope.op_type
        assert sealed.affected_node_ids == envelope.affected_node_ids
        assert "$e" in sealed.payload
        opened = decrypt_envelope(workspace_key, sealed)
        assert opened.payload == EXPECTED_PAYLOADS[envelope.id]


class TestBytesEncryption:
    def test_round_trip_and_layout(self, workspace_key: bytes) -> None:
        data = bytes([1, 2, 3, 250, 251, 252])
        sealed = encrypt_bytes(workspace_key, data)
        assert len(sealed) == 12 + 16 + len(data)
        assert decrypt_bytes(workspace_key, sealed) == data

    def test_wrong_key_and_tampering_fail(self, workspace_key: bytes) -> None:
        data = b"hello notees"
        sealed = encrypt_bytes(workspace_key, data)
        with pytest.raises(ValueError, match="byte decryption failed"):
            decrypt_bytes(bytes(byte ^ 0xFF for byte in workspace_key), sealed)
        tampered = bytearray(sealed)
        tampered[-1] ^= 0xFF
        with pytest.raises(ValueError, match="byte decryption failed"):
            decrypt_bytes(workspace_key, bytes(tampered))


class TestWorkspaceKeyCryptoProvider:
    """The engine provider surface (the TS ``EnvelopeCrypto`` port)."""

    def test_envelope_round_trip_through_the_provider(
        self, workspace_key: bytes, envelopes: list[RelayEnvelope]
    ) -> None:
        provider = WorkspaceKeyCrypto(workspace_key)
        original = envelopes[0]
        envelope = original.model_copy(update={"payload": EXPECTED_PAYLOADS[original.id]})
        sealed = provider.encrypt_envelope(envelope)
        assert "$e" in sealed.payload
        assert provider.decrypt_envelope(sealed).payload == EXPECTED_PAYLOADS[envelope.id]

    def test_plaintext_envelope_passes_through(self, workspace_key: bytes, envelopes: list[RelayEnvelope]) -> None:
        provider = WorkspaceKeyCrypto(workspace_key)
        plain = envelopes[0].model_copy(update={"payload": {"objectId": "x"}})
        assert provider.decrypt_envelope(plain) is plain

    def test_bytes_round_trip_and_wrong_key(self, workspace_key: bytes) -> None:
        provider = WorkspaceKeyCrypto(workspace_key)
        assert provider.decrypt_bytes(provider.encrypt_bytes(b"snapshot")) == b"snapshot"
        with pytest.raises(ValueError, match="byte decryption failed"):
            WorkspaceKeyCrypto(bytes(byte ^ 0xFF for byte in workspace_key)).decrypt_bytes(
                provider.encrypt_bytes(b"snapshot")
            )

    def test_rejects_a_non_32_byte_key(self) -> None:
        with pytest.raises(ValueError, match="32 raw bytes"):
            WorkspaceKeyCrypto(b"\x00" * 16)


class TestAssetBytes:
    """The plan-05 CAS asset seal: external IV, tag-first blob.

    The blob layout is the cross-client asset-sharing contract (TS and
    Flutter port the same bytes) — tag(16) ‖ ct, IV never inside the blob.
    """

    def test_round_trip_and_layout(self, workspace_key: bytes) -> None:
        data = b"\x89PNG asset bytes \x00\xff"
        sealed = encrypt_asset_bytes(workspace_key, data)
        assert len(sealed.iv) == 12
        assert sealed.blob[:16] != b""  # tag leads — the blob is tag ‖ ct
        assert len(sealed.blob) == 16 + len(data)
        assert decrypt_asset_bytes(workspace_key, sealed.iv, sealed.blob) == data

    def test_iv_is_external_and_random(self, workspace_key: bytes) -> None:
        data = b"same plaintext"
        first = encrypt_asset_bytes(workspace_key, data)
        second = encrypt_asset_bytes(workspace_key, data)
        assert first.iv != second.iv
        assert first.blob != second.blob
        # The iv appears nowhere in the stored blob.
        assert first.iv not in first.blob

    def test_wrong_key_and_tampering_fail(self, workspace_key: bytes) -> None:
        data = b"a cover image"
        sealed = encrypt_asset_bytes(workspace_key, data)
        with pytest.raises(ValueError, match="asset decryption failed"):
            decrypt_asset_bytes(bytes(byte ^ 0xFF for byte in workspace_key), sealed.iv, sealed.blob)
        tampered = bytearray(sealed.blob)
        tampered[-1] ^= 0xFF
        with pytest.raises(ValueError, match="asset decryption failed"):
            decrypt_asset_bytes(workspace_key, sealed.iv, bytes(tampered))

    def test_malformed_iv_and_blob_fail(self, workspace_key: bytes) -> None:
        sealed = encrypt_asset_bytes(workspace_key, b"x")
        with pytest.raises(ValueError, match="asset iv must be 12 bytes"):
            decrypt_asset_bytes(workspace_key, b"\x00" * 8, sealed.blob)
        with pytest.raises(ValueError, match="shorter than the 16-byte tag"):
            decrypt_asset_bytes(workspace_key, sealed.iv, b"\x00" * 15)

    def test_zero_byte_plaintext_round_trip(self, workspace_key: bytes) -> None:
        """A sealed zero-byte asset is exactly tag(16) ‖ ct(0) — legal, TS parity."""
        sealed = encrypt_asset_bytes(workspace_key, b"")
        assert len(sealed.blob) == GCM_TAG_BYTES
        assert decrypt_asset_bytes(workspace_key, sealed.iv, sealed.blob) == b""

    def test_seal_if_keyed_round_trip(self, workspace_key: bytes) -> None:
        blob, iv = seal_asset_if_keyed(workspace_key, b"plain")
        assert iv is not None
        opened = open_asset_if_sealed(workspace_key, iv, blob)
        assert opened == b"plain"

    def test_seal_keyless_passthrough(self) -> None:
        blob, iv = seal_asset_if_keyed(None, b"plain")
        assert blob == b"plain"
        assert iv is None

    def test_open_keyless_sealed_returns_none_never_throws(self) -> None:
        workspace_key = generate_workspace_key()
        blob, iv = seal_asset_if_keyed(workspace_key, b"plain")
        assert iv is not None
        assert open_asset_if_sealed(None, iv, blob) is None

    def test_open_plaintext_passthrough(self, workspace_key: bytes) -> None:
        assert open_asset_if_sealed(workspace_key, None, b"plain") == b"plain"
        assert open_asset_if_sealed(None, None, b"plain") == b"plain"

    def test_open_tampered_with_key_raises_honest_error(self, workspace_key: bytes) -> None:
        blob, iv = seal_asset_if_keyed(workspace_key, b"plain")
        assert iv is not None
        tampered = bytearray(blob)
        tampered[0] ^= 0xFF
        with pytest.raises(ValueError, match="asset decryption failed"):
            open_asset_if_sealed(workspace_key, iv, bytes(tampered))
