"""E2EE crypto primitives — the GTK port of ``packages/protocol/src/crypto.ts``.

Both halves of the reference module now live here: the decrypt paths (the
``{"$e": {iv, ct}}`` envelope payload slot) and the custody half (the scrypt
KDF record, master/workspace key wrap, envelope/bytes encryption). Formats
match the server-side groundwork in ``apps/server/src/auth.ts``: wrapped keys
and verifiers are ``base64url(iv ‖ tag ‖ ct)`` / ``base64url(HMAC-SHA256(...))``.

CAS asset bytes (plan-05 contract, shared byte-for-byte with the TS and
Flutter ports — cross-client asset sharing depends on it):
- stored bytes = ``tag(16) ‖ ct`` — TAG FIRST, same order as the payload
  slot's ``ct`` field;
- the IV is EXTERNAL server metadata (the ``asset.iv`` column, b64u on the
  wire) — never part of the blob, never op-log state;
- keyless sessions are legal: no workspace key ⇒ plaintext upload (iv
  absent), and a sealed download without a key surfaces the caller's
  honest-fallback path (never a throw).

Wire formats (``tests/fixtures/wire/envelope-encrypted.json`` is the
byte-pinned contract for the slot, and its comment is authoritative):
- slot ``iv`` = base64url of the 12-byte GCM nonce;
- slot ``ct`` = base64url of ``tag(16) ‖ ct`` — TAG FIRST, unlike the
  wrapped-key record format (``iv ‖ tag ‖ ct`` in the server's auth.ts);
- plaintext payload = UTF-8 JSON of the payload object;
- GCM AAD = ``{envelope.id}.{envelope.workspaceId}``.

Keys are raw 32-byte strings in this port (no WebCrypto ``CryptoKey``
equivalent — ``cryptography`` takes bytes throughout). Every custody key
stays memory-only; nothing here touches disk.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from notees_gtk.core.protocol.models import RelayEnvelope

__all__ = [
    "GCM_NONCE_BYTES",
    "GCM_TAG_BYTES",
    "SCRYPT_DKLEN",
    "SCRYPT_N",
    "SCRYPT_P",
    "SCRYPT_R",
    "KdfRecord",
    "SealedAsset",
    "WorkspaceKeyCrypto",
    "b64u_decode",
    "b64u_encode",
    "create_kdf_record",
    "decrypt_asset_bytes",
    "decrypt_bytes",
    "decrypt_envelope",
    "decrypt_payload",
    "encrypt_asset_bytes",
    "encrypt_bytes",
    "encrypt_envelope",
    "encrypt_payload",
    "envelope_aad",
    "generate_master_key",
    "generate_workspace_key",
    "key_verifier_of",
    "open_asset_if_sealed",
    "scrypt_key_bytes",
    "seal_asset_if_keyed",
    "unwrap_master_key",
    "unwrap_workspace_key",
    "wrap_workspace_key",
]

GCM_NONCE_BYTES = 12
GCM_TAG_BYTES = 16

#: scrypt parameters — fixed by the server record (``apps/server/src/auth.ts``);
#: byte-compat with it is the contract (the plan-01 TS golden vector pins it).
SCRYPT_N = 131072
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32

#: The HMAC message binding a derived wrap key to its record — the verifier
#: is computed over the scrypt-DERIVED key, not the master key, so a wrong
#: password is detectable before the unwrap is attempted (auth.ts parity).
KEY_CHECK_MESSAGE = b"notees-key-check"


def b64u_encode(data: bytes) -> str:
    """Encode unpadded base64url (``b64uEncode`` in the reference crypto.ts)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(text: str) -> bytes:
    """Decode unpadded base64url (``b64uDecode`` in the reference crypto.ts)."""
    padded = text.replace("-", "+").replace("_", "/")
    return base64.b64decode(padded + "=" * (-len(padded) % 4))


# --- scrypt KDF record (password-wrapped master key, server-shape) -------------------


@dataclass(frozen=True)
class KdfRecord:
    """The password-KDF record (``KdfRecord`` in the reference crypto.ts).

    Wire shape (camelCase, exactly the server's record): ``algorithm``
    (always ``"scrypt"``), ``N``, ``r``, ``p``, ``salt``, ``wrappedMasterKey``
    (b64u ``iv ‖ tag ‖ ct`` of the master key under the scrypt-derived wrap
    key), ``keyVerifier`` (b64u HMAC-SHA256 of the derived key over
    ``"notees-key-check"``).
    """

    algorithm: str
    n: int
    r: int
    p: int
    salt: str
    wrapped_master_key: str
    key_verifier: str

    @classmethod
    def from_dict(cls, raw: Any) -> KdfRecord:
        """Validate a server/AuthResult ``kdf`` body, failing loud on drift.

        Raises:
            ValueError: The body is not a scrypt KDF record with the full
                field grammar.
        """
        if not isinstance(raw, dict):
            raise ValueError(f"malformed kdf record: expected a JSON object, got {type(raw).__name__}")
        algorithm = raw.get("algorithm")
        if algorithm != "scrypt":
            raise ValueError(f"unsupported kdf algorithm: {algorithm!r}")
        try:
            n = int(raw["N"])
            r = int(raw["r"])
            p = int(raw["p"])
            salt = raw["salt"]
            wrapped_master_key = raw["wrappedMasterKey"]
            key_verifier = raw["keyVerifier"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"malformed kdf record: {exc}") from exc
        for name, value in (("salt", salt), ("wrappedMasterKey", wrapped_master_key), ("keyVerifier", key_verifier)):
            if not isinstance(value, str) or not value:
                raise ValueError(f"malformed kdf record: {name} must be a non-empty string")
        return cls(algorithm=algorithm, n=n, r=r, p=p, salt=salt, wrapped_master_key=wrapped_master_key,
                    key_verifier=key_verifier)

    def to_dict(self) -> dict[str, Any]:
        """The camelCase wire body (``to_dict`` for the server round-trip)."""
        return {
            "algorithm": self.algorithm,
            "N": self.n,
            "r": self.r,
            "p": self.p,
            "salt": self.salt,
            "wrappedMasterKey": self.wrapped_master_key,
            "keyVerifier": self.key_verifier,
        }


def scrypt_key_bytes(password: str, salt: bytes) -> bytes:
    """Derive the 32-byte wrap key (fixed params — the server record's contract)."""
    kdf = Scrypt(salt=salt, length=SCRYPT_DKLEN, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return kdf.derive(password.encode("utf-8"))


def key_verifier_of(derived_key_bytes: bytes) -> bytes:
    """The record's verifier: HMAC-SHA256(derived wrap key, ``notees-key-check``)."""
    return hmac.new(derived_key_bytes, KEY_CHECK_MESSAGE, hashlib.sha256).digest()


def generate_master_key() -> bytes:
    """A fresh random 32-byte master key (the ``generateMasterKey`` port)."""
    return secrets.token_bytes(SCRYPT_DKLEN)


def create_kdf_record(master_key: bytes, password: str) -> KdfRecord:
    """Wrap ``master_key`` under ``password`` into a fresh server-shape record.

    Mirrors ``createKdfRecord``: random 16-byte salt, scrypt-derived wrap key,
    random 12-byte GCM iv; the record's ``wrappedMasterKey`` is b64u
    ``iv ‖ tag ‖ ct`` (the server's layout, NOT the payload slot's tag-first).
    """
    if len(master_key) != SCRYPT_DKLEN:
        raise ValueError(f"master key must be {SCRYPT_DKLEN} raw bytes, got {len(master_key)}")
    salt = secrets.token_bytes(16)
    derived = scrypt_key_bytes(password, salt)
    iv = secrets.token_bytes(GCM_NONCE_BYTES)
    sealed = AESGCM(derived).encrypt(iv, master_key, None)  # ct ‖ tag
    ct, tag = sealed[:-GCM_TAG_BYTES], sealed[-GCM_TAG_BYTES:]
    return KdfRecord(
        algorithm="scrypt",
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        salt=b64u_encode(salt),
        wrapped_master_key=b64u_encode(iv + tag + ct),
        key_verifier=b64u_encode(key_verifier_of(derived)),
    )


def unwrap_master_key(password: str, record: KdfRecord) -> bytes:
    """Open a :class:`KdfRecord`, returning the raw 32-byte master key.

    Fail fast on a wrong password: the verifier is over the derived key, so
    it is checkable (constant-time) before touching the wrapped master key
    (auth.ts parity). A verifier match with an unopenable wrap (a tampered
    record) fails loud at the GCM tag.
    """
    salt = b64u_decode(record.salt)
    derived = scrypt_key_bytes(password, salt)
    if not hmac.compare_digest(key_verifier_of(derived), b64u_decode(record.key_verifier)):
        raise ValueError("master-key verifier mismatch (wrong password)")
    raw = b64u_decode(record.wrapped_master_key)
    iv, tag, ct = raw[:GCM_NONCE_BYTES], raw[GCM_NONCE_BYTES:28], raw[28:]
    if not iv or len(tag) != GCM_TAG_BYTES or not ct:
        raise ValueError("malformed wrapped master key: not iv ‖ tag ‖ ct")
    try:
        return AESGCM(derived).decrypt(iv, ct + tag, None)
    except InvalidTag as exc:
        raise ValueError("master-key unwrap failed: tampered wrapped key") from exc


# --- workspace keys (master-key-wrapped) ----------------------------------------------


def _check_key_bytes(name: str, key: bytes) -> None:
    if len(key) != SCRYPT_DKLEN:
        raise ValueError(f"{name} must be {SCRYPT_DKLEN} raw bytes, got {len(key)}")


def generate_workspace_key() -> bytes:
    """A fresh random 32-byte workspace key (the ``generateWorkspaceKey`` port)."""
    return secrets.token_bytes(SCRYPT_DKLEN)


def wrap_workspace_key(master_key: bytes, workspace_key: bytes) -> str:
    """Wrap a workspace key under the master key: b64u ``iv ‖ tag ‖ ct``.

    NOTE the byte order — the OPPOSITE of the payload slot's ``ct`` field
    (``tag ‖ ct``): wrapped-key records carry the iv first, per the server's
    auth.ts, while payload slots omit the iv from ``ct`` and lead with the
    tag. The fixture comment is authoritative for the slot; auth.ts for this.
    """
    _check_key_bytes("master key", master_key)
    _check_key_bytes("workspace key", workspace_key)
    iv = secrets.token_bytes(GCM_NONCE_BYTES)
    sealed = AESGCM(master_key).encrypt(iv, workspace_key, None)  # ct ‖ tag
    ct, tag = sealed[:-GCM_TAG_BYTES], sealed[-GCM_TAG_BYTES:]
    return b64u_encode(iv + tag + ct)


def unwrap_workspace_key(master_key: bytes, wrapped: str) -> bytes:
    """Open :func:`wrap_workspace_key`'s record, returning the raw workspace key."""
    _check_key_bytes("master key", master_key)
    raw = b64u_decode(wrapped)
    iv, tag, ct = raw[:GCM_NONCE_BYTES], raw[GCM_NONCE_BYTES:28], raw[28:]
    if not iv or len(tag) != GCM_TAG_BYTES or not ct:
        raise ValueError("malformed wrapped workspace key: not iv ‖ tag ‖ ct")
    try:
        key = AESGCM(master_key).decrypt(iv, ct + tag, None)
    except InvalidTag as exc:
        raise ValueError("workspace-key unwrap failed: wrong master key or tampered record") from exc
    _check_key_bytes("unwrapped workspace key", key)
    return key


# --- envelope payload encryption (the {"$e": {iv, ct}} slot) -------------------------


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
    _check_key_bytes("workspace key", workspace_key)
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


def encrypt_payload(workspace_key: bytes, aad: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Seal a payload object into the ``{"$e": {iv, ct}}`` slot (tag-first ``ct``).

    The exact inverse of :func:`decrypt_payload`: UTF-8 JSON of the payload
    object, GCM AAD bound to the envelope, and the slot's ``ct`` field is
    b64u ``tag(16) ‖ ct`` — the payload slot leads with the tag (no iv inside
    ``ct``), unlike the wrapped-key record's ``iv ‖ tag ‖ ct``.
    """
    _check_key_bytes("workspace key", workspace_key)
    iv = secrets.token_bytes(GCM_NONCE_BYTES)
    # Compact separators — byte parity with the reference's JSON.stringify.
    sealed = AESGCM(workspace_key).encrypt(
        iv, json.dumps(payload, separators=(",", ":")).encode("utf-8"), aad.encode("utf-8")
    )
    ct, tag = sealed[:-GCM_TAG_BYTES], sealed[-GCM_TAG_BYTES:]
    return {"$e": {"iv": b64u_encode(iv), "ct": b64u_encode(tag + ct)}}


def encrypt_envelope(workspace_key: bytes, envelope: RelayEnvelope) -> RelayEnvelope:
    """Seal an envelope's payload; every other field (id, HLC, …) is preserved.

    Mirrors ``encryptEnvelope`` in the reference crypto.ts.
    """
    slot = encrypt_payload(workspace_key, envelope_aad(envelope), envelope.payload)
    return envelope.model_copy(update={"payload": slot})


def decrypt_envelope(workspace_key: bytes, envelope: RelayEnvelope) -> RelayEnvelope:
    """Open an envelope's payload; plaintext envelopes pass through unchanged.

    Mirrors ``decryptEnvelope`` in the reference crypto.ts (the
    ``isEncryptedPayload`` passthrough gate).
    """
    if "$e" not in envelope.payload:
        return envelope
    opened = decrypt_payload(workspace_key, envelope_aad(envelope), envelope.payload["$e"])
    return envelope.model_copy(update={"payload": opened})


# --- byte encryption (snapshot blobs) --------------------------------------------------


def encrypt_bytes(workspace_key: bytes, data: bytes) -> bytes:
    """Seal a snapshot blob: ``iv ‖ tag ‖ ct`` (the wrapped-key record layout)."""
    _check_key_bytes("workspace key", workspace_key)
    iv = secrets.token_bytes(GCM_NONCE_BYTES)
    sealed = AESGCM(workspace_key).encrypt(iv, data, None)  # ct ‖ tag
    ct, tag = sealed[:-GCM_TAG_BYTES], sealed[-GCM_TAG_BYTES:]
    return iv + tag + ct


def decrypt_bytes(workspace_key: bytes, data: bytes) -> bytes:
    """Open :func:`encrypt_bytes`'s blob (``iv ‖ tag ‖ ct`` back to plaintext)."""
    _check_key_bytes("workspace key", workspace_key)
    iv, tag, ct = data[:GCM_NONCE_BYTES], data[GCM_NONCE_BYTES:28], data[28:]
    if not iv or len(tag) != GCM_TAG_BYTES or not ct:
        raise ValueError("malformed sealed bytes: not iv ‖ tag ‖ ct")
    try:
        return AESGCM(workspace_key).decrypt(iv, ct + tag, None)
    except InvalidTag as exc:
        raise ValueError("byte decryption failed: wrong key or tampered blob") from exc


# --- CAS asset bytes (plan-05: external IV, tag-first blob) ----------------------------


@dataclass(frozen=True)
class SealedAsset:
    """The seal of one asset upload (the ``encryptAssetBytes`` return shape).

    ``iv`` is the random 12-byte GCM nonce — EXTERNAL to the blob, carried as
    server-side asset metadata (b64u on the wire, the ``asset.iv`` column);
    ``blob`` is what CAS stores and dedupes on, ``tag(16) ‖ ct``.
    """

    iv: bytes
    blob: bytes


def encrypt_asset_bytes(workspace_key: bytes, data: bytes) -> SealedAsset:
    """Seal CAS asset bytes for upload (the ``encryptAssetBytes`` port).

    The blob layout is ``tag(16) ‖ ct`` — tag first, exactly like the payload
    slot's ``ct`` field and unlike :func:`encrypt_bytes`' snapshot layout
    (``iv ‖ tag ‖ ct``). The IV never enters the blob: it rides the upload
    multipart + the server's ``asset.iv`` metadata column instead.
    """
    _check_key_bytes("workspace key", workspace_key)
    iv = secrets.token_bytes(GCM_NONCE_BYTES)
    sealed = AESGCM(workspace_key).encrypt(iv, data, None)  # ct ‖ tag
    ct, tag = sealed[:-GCM_TAG_BYTES], sealed[-GCM_TAG_BYTES:]
    return SealedAsset(iv=iv, blob=tag + ct)


def decrypt_asset_bytes(workspace_key: bytes, iv: bytes, blob: bytes) -> bytes:
    """Open :func:`encrypt_asset_bytes`'s seal (tag-first blob, external IV).

    Raises:
        ValueError: wrong key length, malformed IV/blob, or any GCM failure
            (wrong key or tampered ciphertext — an honest error the caller
            surfaces, never a corrupted render).
    """
    _check_key_bytes("workspace key", workspace_key)
    if len(iv) != GCM_NONCE_BYTES:
        raise ValueError(f"asset iv must be {GCM_NONCE_BYTES} bytes, got {len(iv)}")
    if len(blob) < GCM_TAG_BYTES:
        raise ValueError("sealed asset shorter than the 16-byte tag")
    tag, ct = blob[:GCM_TAG_BYTES], blob[GCM_TAG_BYTES:]
    try:
        return AESGCM(workspace_key).decrypt(iv, ct + tag, None)
    except InvalidTag as exc:
        raise ValueError("asset decryption failed: wrong key or tampered blob") from exc


def seal_asset_if_keyed(workspace_key: bytes | None, data: bytes) -> tuple[bytes, str | None]:
    """Encrypt-at-upload boundary (the ``sealAssetIfKeyed`` port).

    Keyed ⇒ the sealed blob + b64u iv for the multipart ``iv`` field. Keyless
    (a locked/API-key session with no workspace key) is legal: the bytes pass
    through plaintext and the iv stays ``None`` — the server sniffs and stores
    ``asset.iv NULL`` exactly as an unkeyed client always has.
    """
    if workspace_key is None:
        return data, None
    sealed = encrypt_asset_bytes(workspace_key, data)
    return sealed.blob, b64u_encode(sealed.iv)


def open_asset_if_sealed(workspace_key: bytes | None, iv: str | None, blob: bytes) -> bytes | None:
    """Decrypt-at-download boundary (the ``openAssetIfSealed`` port).

    Plaintext asset (``iv is None``) ⇒ the blob passes through. Sealed asset
    with a key ⇒ the opened bytes (a GCM failure raises :class:`ValueError` —
    the caller's honest-error surface, never a corrupted render). Sealed asset
    WITHOUT a key ⇒ ``None`` — the caller shows its existing asset
    error/fallback UI; this function never throws in the keyless case.
    """
    if iv is None:
        return blob
    if workspace_key is None:
        return None
    return decrypt_asset_bytes(workspace_key, b64u_decode(iv), blob)


# --- engine provider (the sync ``EnvelopeCrypto`` surface) ----------------------------


@dataclass(frozen=True)
class WorkspaceKeyCrypto:
    """The engine's E2EE provider closed over a raw workspace key.

    The GTK shape of the TS ``EnvelopeCrypto`` (``packages/sync``): the sync
    engine is key-agnostic and drives this surface — encrypt/decrypt envelope
    at the push/apply boundaries, seal/open snapshot bytes. Immutable; the
    client swaps providers via ``SyncEngine.set_crypto``.
    """

    workspace_key: bytes

    def __post_init__(self) -> None:
        _check_key_bytes("workspace key", self.workspace_key)

    def encrypt_envelope(self, envelope: RelayEnvelope) -> RelayEnvelope:
        return encrypt_envelope(self.workspace_key, envelope)

    def decrypt_envelope(self, envelope: RelayEnvelope) -> RelayEnvelope:
        return decrypt_envelope(self.workspace_key, envelope)

    def encrypt_bytes(self, data: bytes) -> bytes:
        return encrypt_bytes(self.workspace_key, data)

    def decrypt_bytes(self, data: bytes) -> bytes:
        return decrypt_bytes(self.workspace_key, data)
