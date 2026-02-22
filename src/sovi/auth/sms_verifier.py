"""Disposable SMS verification via TextVerified API.

Used during account signup for one-time phone verification,
then discarded (ongoing 2FA uses TOTP instead).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from sovi.config import settings

logger = logging.getLogger(__name__)

TEXTVERIFIED_BASE = "https://www.textverified.com/api"

# TextVerified service names for each platform
PLATFORM_SERVICES: dict[str, str] = {
    "tiktok": "TikTok",
    "instagram": "Instagram",
}


@dataclass
class SmsVerification:
    """An in-progress SMS verification."""
    verification_id: str
    phone_number: str
    service: str


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.textverified_api_key}",
        "Content-Type": "application/json",
    }


def request_number(platform: str) -> SmsVerification | None:
    """Request a disposable phone number for a platform's SMS verification.

    Returns a SmsVerification with the phone number, or None on failure.
    """
    service = PLATFORM_SERVICES.get(platform)
    if not service:
        logger.error("No SMS service configured for platform: %s", platform)
        return None

    if not settings.textverified_api_key:
        logger.error("TEXTVERIFIED_API_KEY not configured")
        return None

    try:
        resp = httpx.post(
            f"{TEXTVERIFIED_BASE}/Verifications",
            headers=_headers(),
            json={"id": service},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()

        verification = SmsVerification(
            verification_id=data["id"],
            phone_number=data["number"],
            service=service,
        )
        logger.info("Got SMS number %s for %s (id=%s)",
                     verification.phone_number, platform, verification.verification_id)
        return verification

    except Exception:
        logger.error("Failed to request SMS number for %s", platform, exc_info=True)
        return None


def wait_for_code(
    verification: SmsVerification,
    timeout: int = 120,
    poll_interval: int = 5,
) -> str | None:
    """Poll TextVerified for the SMS verification code.

    Returns the code string, or None if not received within timeout.
    """
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            resp = httpx.get(
                f"{TEXTVERIFIED_BASE}/Verifications/{verification.verification_id}",
                headers=_headers(),
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()

            code = data.get("code")
            if code:
                logger.info("Received SMS code for %s: %s", verification.service, code)
                return code

            sms_text = data.get("sms")
            if sms_text:
                # Extract code from SMS text (usually 4-6 digits)
                import re
                match = re.search(r"\b(\d{4,6})\b", sms_text)
                if match:
                    code = match.group(1)
                    logger.info("Extracted SMS code from text for %s: %s", verification.service, code)
                    return code

        except Exception:
            logger.warning("Error polling SMS verification", exc_info=True)

        time.sleep(poll_interval)

    logger.warning("Timed out waiting for SMS code (verification_id=%s)", verification.verification_id)
    return None


def cancel_verification(verification: SmsVerification) -> bool:
    """Cancel an in-progress verification (release the number)."""
    try:
        resp = httpx.put(
            f"{TEXTVERIFIED_BASE}/Verifications/{verification.verification_id}/Cancel",
            headers=_headers(),
            timeout=15.0,
        )
        return resp.status_code < 400
    except Exception:
        return False
