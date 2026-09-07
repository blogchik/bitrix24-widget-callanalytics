"""AES-256-GCM envelope for the token columns (`*_enc`, §3, decision 19).

Envelope layout, exactly as the DDL comments promise:

    key_id(1 byte) || nonce(12) || ciphertext || tag(16)

WHY the AAD is `member_id:column`: authenticated data binds each ciphertext to the tenant
row AND the column it came from. A `refresh_token_enc` blob copied into another portal's
row, or into that same row's `access_token_enc`, fails the GCM tag check and decrypts to
nothing - so an attacker (or a bad backup restore) with write access to the table cannot
move a working credential around, and cannot make one portal's worker act as another.

WHY the key id travels in the envelope: decryption looks the key up by the id the
ciphertext carries, not by the active setting. Rotation is therefore a background
re-encrypt job that can run while both keys are live, never a migration and never a
flag day. v1 ships one key; the byte costs nothing and keeps that door open.
"""

from __future__ import annotations

import os
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings

_KEY_ID_LEN = 1
_NONCE_LEN = 12  # GCM standard nonce size; anything else costs an extra GHASH step
_TAG_LEN = 16
_MIN_BLOB_LEN = _KEY_ID_LEN + _NONCE_LEN + _TAG_LEN


class DecryptionError(Exception):
    """The blob is not a valid envelope for this key ring, tenant and column.

    Deliberately carries no detail about WHY: an unknown key id, a truncated blob and a
    failed tag are the same event to a caller, and the difference is not something a log
    line should invite an attacker to probe.
    """


@lru_cache(maxsize=8)
def _cipher(key_id: int) -> AESGCM:
    """Cached AESGCM per key id (key schedule setup is not free per call)."""
    key = settings.token_enc_keys.get(key_id)
    if key is None:
        raise KeyError(key_id)
    return AESGCM(key)


def _aad(member_id: str, column: str) -> bytes:
    """The tenant/column binding described above."""
    return f"{member_id}:{column}".encode()


def encrypt(plaintext: str, *, member_id: str, column: str) -> bytes:
    """Encrypt with the ACTIVE key; the resulting blob records which key that was."""
    key_id = settings.token_enc_active_key_id
    cipher = _cipher(key_id)  # config guarantees the active key exists (fail-fast at import)
    nonce = os.urandom(_NONCE_LEN)
    sealed = cipher.encrypt(nonce, plaintext.encode("utf-8"), _aad(member_id, column))
    return bytes((key_id,)) + nonce + sealed


def decrypt(blob: bytes, *, member_id: str, column: str) -> str:
    """Decrypt a blob written for THIS tenant and THIS column, or raise DecryptionError."""
    data = bytes(blob) if not isinstance(blob, bytes) else blob
    if len(data) < _MIN_BLOB_LEN:
        raise DecryptionError("ciphertext is too short to be an envelope")

    key_id = data[0]
    try:
        cipher = _cipher(key_id)
    except KeyError:
        # A key that is no longer (or not yet) on the ring: retiring a key must fail
        # loudly on read rather than silently return an empty credential.
        raise DecryptionError("unknown key id") from None

    nonce = data[_KEY_ID_LEN : _KEY_ID_LEN + _NONCE_LEN]
    sealed = data[_KEY_ID_LEN + _NONCE_LEN :]
    try:
        opened = cipher.decrypt(nonce, sealed, _aad(member_id, column))
    except InvalidTag:
        # Wrong tenant, wrong column, wrong key or tampered bytes - all indistinguishable.
        raise DecryptionError("authentication tag mismatch") from None
    try:
        return opened.decode("utf-8")
    except UnicodeDecodeError:
        raise DecryptionError("plaintext is not valid UTF-8") from None
