"""TOTP (Time-based One-Time Password) management for 2FA.

Uses pyotp to generate secrets and codes for accounts that have
email+password auth with TOTP as ongoing 2FA.
"""

from __future__ import annotations

import pyotp


def generate_secret() -> str:
    """Generate a new TOTP secret (base32-encoded, 32 chars)."""
    return pyotp.random_base32()


def get_code(secret: str) -> str:
    """Get the current TOTP code for a secret."""
    return pyotp.TOTP(secret).now()


def verify_code(secret: str, code: str) -> bool:
    """Verify a TOTP code against a secret (allows +-1 window)."""
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def get_provisioning_uri(secret: str, username: str, issuer: str = "SOVI") -> str:
    """Get the otpauth:// URI for QR code enrollment."""
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)
