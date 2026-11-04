"""Unit tests for envelope encryption. No DB, no settings file — the keyring is passed in."""

from __future__ import annotations

import base64
import os

import pytest
from pydantic import SecretStr

from orchestrator.secrets.crypto import (
    DecryptionError,
    EncryptedValue,
    KeyRing,
    SecretsConfigurationError,
    decrypt,
    encrypt,
    load_keyring,
)

PLAINTEXT = "ghp_notarealtoken_0123456789"


@pytest.fixture
def ring() -> KeyRing:
    return KeyRing(keys={"test-1": os.urandom(32)}, current_id="test-1")


def test_round_trip(ring: KeyRing) -> None:
    value = encrypt(PLAINTEXT, keyring=ring)
    assert value.enc_key_id == "test-1"
    assert PLAINTEXT.encode() not in value.ciphertext
    assert decrypt(value, keyring=ring).get_secret_value() == PLAINTEXT


def test_round_trip_with_aad(ring: KeyRing) -> None:
    value = encrypt(PLAINTEXT, aad=b"7:GH_TOKEN", keyring=ring)
    assert decrypt(value, aad=b"7:GH_TOKEN", keyring=ring).get_secret_value() == PLAINTEXT
    # A blob moved to a different project/name no longer authenticates.
    with pytest.raises(DecryptionError):
        decrypt(value, aad=b"8:GH_TOKEN", keyring=ring)


def test_ciphertext_differs_per_call(ring: KeyRing) -> None:
    """Fresh data key + fresh nonce every time — identical plaintexts must not be linkable."""
    blobs = {encrypt(PLAINTEXT, keyring=ring).ciphertext for _ in range(5)}
    assert len(blobs) == 5


def test_tampering_any_byte_fails_authentication(ring: KeyRing) -> None:
    original = encrypt(PLAINTEXT, keyring=ring)
    for index in range(len(original.ciphertext)):
        mutated = bytearray(original.ciphertext)
        mutated[index] ^= 0x01
        candidate = EncryptedValue(ciphertext=bytes(mutated), enc_key_id=original.enc_key_id)
        with pytest.raises(DecryptionError):
            decrypt(candidate, keyring=ring)


def test_truncation_fails(ring: KeyRing) -> None:
    original = encrypt(PLAINTEXT, keyring=ring)
    with pytest.raises(DecryptionError):
        decrypt(
            EncryptedValue(ciphertext=original.ciphertext[:-4], enc_key_id=original.enc_key_id),
            keyring=ring,
        )


def test_decrypts_with_retired_key_and_encrypts_with_current() -> None:
    """`secret.enc_key_id` exists for rotation: old rows keep resolving after the current key
    moves on (tech-stack decision 8)."""
    retired = os.urandom(32)
    old_ring = KeyRing(keys={"v1": retired}, current_id="v1")
    old_value = encrypt(PLAINTEXT, keyring=old_ring)

    rotated = KeyRing(keys={"v1": retired, "v2": os.urandom(32)}, current_id="v2")
    assert decrypt(old_value, keyring=rotated).get_secret_value() == PLAINTEXT
    assert encrypt(PLAINTEXT, keyring=rotated).enc_key_id == "v2"


def test_unknown_key_id_is_a_clear_error(ring: KeyRing) -> None:
    value = encrypt(PLAINTEXT, keyring=ring)
    other = KeyRing(keys={"other": os.urandom(32)}, current_id="other")
    with pytest.raises(DecryptionError, match="test-1"):
        decrypt(value, keyring=other)


def test_current_id_must_be_in_the_ring() -> None:
    with pytest.raises(SecretsConfigurationError, match="not in the keyring"):
        KeyRing(keys={"a": os.urandom(32)}, current_id="b")


@pytest.mark.parametrize(
    ("master_key", "match"),
    [
        ("", "is empty"),
        (base64.b64encode(bytes(16)).decode(), "32 bytes"),
        ("not base64!!", "base64"),
    ],
)
def test_missing_or_short_master_key_raises_at_first_use(
    monkeypatch: pytest.MonkeyPatch, master_key: str, match: str
) -> None:
    """Configuration errors surface on use, never at import (importing must not need a
    configured deployment)."""
    from orchestrator import settings as settings_module

    monkeypatch.setenv("FACTORIAL_MASTER_KEY", master_key)
    monkeypatch.setenv("FACTORIAL_MASTER_KEY_ID", "bad-1")
    settings_module.get_settings.cache_clear()
    load_keyring.cache_clear()
    try:
        with pytest.raises(SecretsConfigurationError, match=match):
            load_keyring()
    finally:
        settings_module.get_settings.cache_clear()
        load_keyring.cache_clear()


def test_decrypted_value_repr_does_not_leak(ring: KeyRing) -> None:
    secret = decrypt(encrypt(PLAINTEXT, keyring=ring), keyring=ring)
    assert isinstance(secret, SecretStr)
    assert PLAINTEXT not in repr(secret)
    assert PLAINTEXT not in str(secret)


def test_non_secret_blob_is_rejected(ring: KeyRing) -> None:
    with pytest.raises(DecryptionError, match="not a factorial secret blob"):
        decrypt(EncryptedValue(ciphertext=b"plain bytes", enc_key_id="test-1"), keyring=ring)
