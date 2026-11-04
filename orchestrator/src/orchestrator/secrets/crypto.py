"""Envelope encryption for `secret.value_ciphertext` (tech-stack decision 8).

A fresh 256-bit data key per value, wrapped by a master key that comes from the environment
today and a KMS later. Not Fernet: that pins the scheme into the token format, whereas here the
scheme is a byte in a versioned frame, so changing it is a version bump plus a lazy re-wrap.

Rotation is expected, which is what `secret.enc_key_id` is for: encryption always uses the
current master key, decryption looks the wrapping key up by the id carried in the blob, so a
retired key keeps working until the last row referencing it is rewritten.

Plaintext never leaves this module as a `str` — `decrypt` returns `SecretStr`, whose `repr` is
masked. Nothing here logs.
"""

from __future__ import annotations

import base64
import binascii
import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

from orchestrator.settings import get_settings

__all__ = [
    "DecryptionError",
    "EncryptedValue",
    "KeyRing",
    "SecretsConfigurationError",
    "decode_master_key",
    "decrypt",
    "encrypt",
    "load_keyring",
]

# --- framing ---------------------------------------------------------------------------------
#
# The blob stored in `secret.value_ciphertext` is self-describing, so a reader needs nothing but
# the master keyring to interpret it:
#
#   magic      3   b"SFS"
#   version    1   frame version; bump to change the layout below
#   scheme     1   SCHEME_AESGCM_V1 — AES-256-GCM data key, AES-256-GCM key wrap
#   kid_len    1   length of the master-key id
#   kid        n   master-key id, utf-8 (mirrored into the `enc_key_id` column for querying)
#   -------------- everything above is the header, and is AAD for BOTH GCM operations
#   wrap_nonce 12  nonce for the key-wrap
#   wrap_len   1   length of the wrapped data key (32-byte key + 16-byte tag = 48)
#   wrapped    n   AESGCM(master).encrypt(wrap_nonce, data_key, header)
#   nonce      12  nonce for the payload
#   payload    *   AESGCM(data_key).encrypt(nonce, plaintext, header + caller aad) — tag appended
#
# Binding the header as AAD is what makes the key id tamper-evident: swapping `kid` for another
# ring entry fails authentication rather than silently selecting a different key.

MAGIC: Final = b"SFS"
FRAME_V1: Final = 1
SCHEME_AESGCM_V1: Final = 1
_NONCE_BYTES: Final = 12
_KEY_BYTES: Final = 32


class SecretsConfigurationError(RuntimeError):
    """The master keyring is missing or malformed. Raised at first use, never at import —
    importing this module must not require a configured deployment (tests, migrations, CLI)."""


class DecryptionError(RuntimeError):
    """The blob did not authenticate: wrong key, unknown key id, or tampered bytes.

    Uninformative about which — that detail belongs in neither a log line nor an API response.
    """


@dataclass(frozen=True, slots=True)
class EncryptedValue:
    """The two DB columns together. `enc_key_id` duplicates the id inside `ciphertext` so a
    rotation sweep can find affected rows with a `WHERE`, not a full table decrypt."""

    ciphertext: bytes
    enc_key_id: str


@dataclass(frozen=True, slots=True)
class KeyRing:
    """Master keys by id, with one designated current.

    Rotation: add the new key, point `current_id` at it, re-`encrypt` rows at leisure.
    Decryption is a by-id lookup, so retired ids keep resolving until then.
    """

    keys: Mapping[str, bytes]
    current_id: str

    def __post_init__(self) -> None:
        if self.current_id not in self.keys:
            raise SecretsConfigurationError(
                f"current master key id {self.current_id!r} is not in the keyring"
            )

    def key(self, key_id: str) -> bytes:
        try:
            return self.keys[key_id]
        except KeyError:
            raise DecryptionError(f"no master key registered for id {key_id!r}") from None


_MAKE_A_KEY: Final = "generate one with `openssl rand -base64 32`"


def decode_master_key(raw: str, key_id: str) -> bytes:
    if not raw:
        raise SecretsConfigurationError(f"FACTORIAL_MASTER_KEY is empty: {_MAKE_A_KEY}")
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SecretsConfigurationError(
            f"FACTORIAL_MASTER_KEY is not valid base64: {_MAKE_A_KEY}"
        ) from exc
    if len(key) != _KEY_BYTES:
        raise SecretsConfigurationError(
            f"FACTORIAL_MASTER_KEY must decode to {_KEY_BYTES} bytes, got {len(key)} "
            f"(key id {key_id!r}): {_MAKE_A_KEY}"
        )
    return key


@lru_cache(maxsize=1)
def load_keyring() -> KeyRing:
    """Build the keyring from settings on first use.

    Settings carry exactly one key today, so the ring has one entry; nothing above this function
    knows how many there are.
    """
    settings = get_settings()
    key_id = settings.master_key_id
    return KeyRing(
        keys={key_id: decode_master_key(settings.master_key.get_secret_value(), key_id)},
        current_id=key_id,
    )


def _header(key_id: str) -> bytes:
    kid = key_id.encode("utf-8")
    if not 0 < len(kid) < 256:
        raise SecretsConfigurationError(f"master key id {key_id!r} must be 1..255 utf-8 bytes")
    return MAGIC + bytes([FRAME_V1, SCHEME_AESGCM_V1, len(kid)]) + kid


def encrypt(
    plaintext: str,
    *,
    aad: bytes | None = None,
    keyring: KeyRing | None = None,
) -> EncryptedValue:
    """Wrap `plaintext` under a fresh data key, itself wrapped by the current master key.

    `aad` binds the ciphertext to its context — the store passes `project_id:name`, so a row
    copied to another project or renamed fails to decrypt rather than resolving to a value the
    caller was never granted. Pass the same `aad` to `decrypt`.
    """
    ring = keyring or load_keyring()
    header = _header(ring.current_id)
    master = ring.key(ring.current_id)

    data_key = os.urandom(_KEY_BYTES)
    wrap_nonce = os.urandom(_NONCE_BYTES)
    wrapped = AESGCM(master).encrypt(wrap_nonce, data_key, header)

    nonce = os.urandom(_NONCE_BYTES)
    payload = AESGCM(data_key).encrypt(nonce, plaintext.encode("utf-8"), header + (aad or b""))

    blob = header + wrap_nonce + bytes([len(wrapped)]) + wrapped + nonce + payload
    return EncryptedValue(ciphertext=blob, enc_key_id=ring.current_id)


def decrypt(
    value: EncryptedValue,
    *,
    aad: bytes | None = None,
    keyring: KeyRing | None = None,
) -> SecretStr:
    """Inverse of `encrypt`. Returns `SecretStr` so an accidental log line prints a mask."""
    ring = keyring or load_keyring()
    blob = value.ciphertext
    try:
        if blob[:3] != MAGIC:
            raise DecryptionError("not a factorial secret blob")
        version, scheme, kid_len = blob[3], blob[4], blob[5]
        if version != FRAME_V1 or scheme != SCHEME_AESGCM_V1:
            raise DecryptionError(f"unsupported frame v{version} scheme {scheme}")

        pos = 6
        header = blob[: pos + kid_len]
        key_id = blob[pos : pos + kid_len].decode("utf-8")
        pos += kid_len

        wrap_nonce = blob[pos : pos + _NONCE_BYTES]
        pos += _NONCE_BYTES
        wrap_len = blob[pos]
        pos += 1
        wrapped = blob[pos : pos + wrap_len]
        pos += wrap_len

        nonce = blob[pos : pos + _NONCE_BYTES]
        pos += _NONCE_BYTES
        payload = blob[pos:]
        if (
            len(wrap_nonce) != _NONCE_BYTES
            or len(wrapped) != wrap_len
            or len(nonce) != _NONCE_BYTES
        ):
            raise DecryptionError("truncated secret blob")
    except IndexError as exc:
        raise DecryptionError("truncated secret blob") from exc
    except UnicodeDecodeError as exc:
        raise DecryptionError("malformed key id in secret blob") from exc

    master = ring.key(key_id)
    try:
        data_key = AESGCM(master).decrypt(wrap_nonce, wrapped, header)
        plaintext = AESGCM(data_key).decrypt(nonce, payload, header + (aad or b""))
    except InvalidTag as exc:
        raise DecryptionError("secret failed authentication") from exc
    return SecretStr(plaintext.decode("utf-8"))
