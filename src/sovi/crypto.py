"""AES-256-GCM encryption for sensitive fields (passwords, TOTP secrets, proxy creds)."""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from sovi.config import settings

_NONCE_SIZE = 12  # 96-bit nonce for AES-GCM


def _get_key() -> bytes:
    raw = settings.sovi_master_key
    if not raw:
        raise RuntimeError("SOVI_MASTER_KEY not set")
    key = base64.b64decode(raw)
    if len(key) != 32:
        raise ValueError("SOVI_MASTER_KEY must be 32 bytes (base64-encoded)")
    return key


def encrypt(plaintext: str) -> str:
    """Encrypt a string. Returns base64(nonce + ciphertext)."""
    key = _get_key()
    nonce = os.urandom(_NONCE_SIZE)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode(), None)
    return base64.b64encode(nonce + ct).decode()


def decrypt(token: str) -> str:
    """Decrypt a base64(nonce + ciphertext) token back to plaintext."""
    key = _get_key()
    raw = base64.b64decode(token)
    nonce, ct = raw[:_NONCE_SIZE], raw[_NONCE_SIZE:]
    return AESGCM(key).decrypt(nonce, ct, None).decode()
