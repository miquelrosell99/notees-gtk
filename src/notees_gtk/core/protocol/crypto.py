"""E2EE payload decryption — the GTK port of the reference decrypt paths.

Ports the decrypt half of ``packages/protocol/src/crypto.ts`` (the Notees
monorepo): unpadded base64url helpers and AES-256-GCM payload-slot decryption
for the ``{"$e": {iv, ct}}`` envelope payload slot. Only the DECRYPT half
ports in this slice: the GTK client's runtime sync still rejects ``$e`` at
apply time (``data/store.py`` — custody is a later phase). This module serves
the fixture lockstep gate (decrypt-and-validate) and the replay harness.

Wire formats (``tests/fixtures/wire/envelope-encrypted.json`` is the
byte-pinned contract, and its comment is authoritative):
- slot ``iv`` = base64url of the 12-byte GCM nonce;
- slot ``ct`` = base64url of ``tag(16) ‖ ct`` — TAG FIRST, unlike the
  wrapped-key record format (``iv ‖ tag ‖ ct`` in the server's auth.ts);
- plaintext payload = UTF-8 JSON of the payload object;
- GCM AAD = ``{envelope.id}.{envelope.workspaceId}``.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from notees_gtk.core.protocol.models import RelayEnvelope

__all__ = [
    "GCM_NONCE_BYTES",
    "GCM_TAG_BYTES",
    "b64u_decode",
    "decrypt_envelope",
    "decrypt_payload",
    "envelope_aad",
]

GCM_NONCE_BYTES = 12
GCM_TAG_BYTES = 16


def b64u_decode(text: str) -> bytes:
    """Decode unpadded base64url (``b64uDecode`` in the reference crypto.ts)."""
    padded = text.replace("-", "+").replace("_", "/")
    return base64.b64decode(padded + "=" * (-len(padded) % 4))


def envelope_aad(envelope: RelayEnvelope) -> str:
    """The GCM AAD binding a payload to its envelope: ``{id}.{workspaceId}``."""
    return f"{envelope.id}.{envelope.workspace_id}"


def decrypt_payload(workspace_key: bytes, aad: str, slot: dict[str, str]) -> dict[str, Any]:
    """Open an encrypted payload slot and return the plaintext payload object.

    ``slot`` is ``{"iv": ..., "ct": ...}`` with the tag-first ``ct`` layout.
    ``cryptography`` wants the seal back as ``ct ‖ tag`` (the WebCrypto
    layout), so the tag is moved back behind the ciphertext before decrypt.

    Raises:
        ValueError: wrong key length, malformed slot, or any GCM failure
            (wrong key, wrong AAD, tampered ciphertext, or a port that
            misread the tag-first layout as ``ct ‖ tag``).
    """
    if len(workspace_key) != 32:
        raise ValueError("workspace key must be 32 raw bytes")
    iv = b64u_decode(slot["iv"])
    raw = b64u_decode(slot["ct"])
    if len(iv) != GCM_NONCE_BYTES:
        raise ValueError(f"nonce must be {GCM_NONCE_BYTES} bytes, got {len(iv)}")
    if len(raw) <= GCM_TAG_BYTES:
        raise ValueError("sealed payload shorter than the 16-byte tag")
    tag, ct = raw[:GCM_TAG_BYTES], raw[GCM_TAG_BYTES:]
    try:
        plain = AESGCM(workspace_key).decrypt(iv, ct + tag, aad.encode("utf-8"))
    except InvalidTag as exc:
        raise ValueError(
            "payload decryption failed: wrong key, wrong AAD, or tampered ciphertext",
        ) from exc
    payload: dict[str, Any] = json.loads(plain.decode("utf-8"))
    return payload


def decrypt_envelope(workspace_key: bytes, envelope: RelayEnvelope) -> RelayEnvelope:
    """Open an envelope's payload; plaintext envelopes pass through unchanged.

    Mirrors ``decryptEnvelope`` in the reference crypto.ts (the
    ``isEncryptedPayload`` passthrough gate).
    """
    if "$e" not in envelope.payload:
        return envelope
    opened = decrypt_payload(workspace_key, envelope_aad(envelope), envelope.payload["$e"])
    return envelope.model_copy(update={"payload": opened})
