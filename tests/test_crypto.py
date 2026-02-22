"""Tests for AES-256-GCM encryption."""

from __future__ import annotations

import base64
import os

import pytest


def test_encrypt_decrypt(monkeypatch):
    # Generate a test key
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("SOVI_MASTER_KEY", key)

    # Re-import to pick up new env
    from sovi.config import Settings
    monkeypatch.setattr("sovi.crypto.settings", Settings(_env_file=None, sovi_master_key=key))

    from sovi.crypto import decrypt, encrypt

    plaintext = "super_secret_password_123!"
    token = encrypt(plaintext)
    assert token != plaintext
    assert decrypt(token) == plaintext


def test_encrypt_produces_different_ciphertexts(monkeypatch):
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("SOVI_MASTER_KEY", key)

    from sovi.config import Settings
    monkeypatch.setattr("sovi.crypto.settings", Settings(_env_file=None, sovi_master_key=key))

    from sovi.crypto import encrypt

    # Same plaintext should produce different ciphertexts (random nonce)
    t1 = encrypt("test")
    t2 = encrypt("test")
    assert t1 != t2


def test_missing_key_raises(monkeypatch):
    monkeypatch.setenv("SOVI_MASTER_KEY", "")
    from sovi.config import Settings
    monkeypatch.setattr("sovi.crypto.settings", Settings(_env_file=None, sovi_master_key=""))

    from sovi.crypto import encrypt

    with pytest.raises(RuntimeError, match="SOVI_MASTER_KEY not set"):
        encrypt("test")
