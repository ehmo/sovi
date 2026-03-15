"""On-device email verification reader — opens webmail in Safari via WDA.

Replaces server-side IMAP (email_verifier.py) and mail.tm REST API (email_api.py)
with fully on-device email reading through the phone's cellular connection.

Supported providers:
- ProtonMail: login to account.proton.me, read inbox
- Outlook: login to outlook.live.com, read inbox
- mail.tm: login to mail.tm web UI, read inbox

All traffic flows through the phone's cellular connection.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from sovi import events
from sovi.crypto import decrypt
from sovi.db import sync_execute
from sovi.device.wda_client import WDASession

logger = logging.getLogger(__name__)

SAFARI = "com.apple.mobilesafari"

# Platform-specific email senders to look for
PLATFORM_SENDERS = {
    "tiktok": ["tiktok", "musically", "bytedance"],
    "instagram": ["instagram", "facebook", "facebookmail"],
    "reddit": ["reddit", "redditmail"],
    "youtube": ["google", "youtube", "accounts.google"],
    "youtube_shorts": ["google", "youtube", "accounts.google"],
    "facebook": ["facebook", "facebookmail"],
    "linkedin": ["linkedin"],
    "x_twitter": ["twitter", "x.com"],
}

# Verification code patterns (4-8 digit codes)
CODE_PATTERNS = [
    re.compile(r"\b(\d{6})\b"),      # 6-digit (most common)
    re.compile(r"\b(\d{4})\b"),      # 4-digit
    re.compile(r"\b(\d{8})\b"),      # 8-digit
    re.compile(r"\b(\d{5})\b"),      # 5-digit
]

# Provider login URLs
PROVIDER_URLS = {
    "protonmail": "https://account.proton.me/login",
    "outlook": "https://outlook.live.com/mail/",
    "mailtm": "https://mail.tm/en/",
}


def read_verification_code(
    wda: WDASession,
    email_account: dict,
    platform: str,
    *,
    device_id: str | None = None,
    timeout: int = 120,
    poll_interval: int = 10,
) -> str | None:
    """Read a verification code from email on-device via Safari.

    Opens the email provider's web interface in Safari, logs in,
    searches for a message from the platform, and extracts the code.

    Args:
        wda: WDA session for the device
        email_account: dict with provider, email (encrypted), password (encrypted)
        platform: which platform's verification to look for
        device_id: for event logging
        timeout: total seconds to poll
        poll_interval: seconds between inbox refreshes

    Returns:
        Verification code string, or None if not found.
    """
    provider = email_account.get("provider", "")
    email = decrypt(email_account["email"])
    password = decrypt(email_account["password"])

    logger.info("Reading verification for %s from %s (%s)", platform, email, provider)

    events.emit("persona", "info", "email_verification_attempt",
                f"Reading {platform} verification code from {provider}",
                device_id=device_id,
                context={
                    "provider": provider,
                    "platform": platform,
                    "email_account_id": str(email_account.get("id", "")),
                })

    try:
        if provider == "protonmail":
            code = _read_protonmail(wda, email, password, platform, timeout, poll_interval)
        elif provider == "outlook":
            code = _read_outlook(wda, email, password, platform, timeout, poll_interval)
        elif provider == "mailtm":
            code = _read_mailtm(wda, email, password, platform, timeout, poll_interval)
        else:
            logger.error("Unsupported email provider: %s", provider)
            return None

        if code:
            events.emit("persona", "info", "email_verification_success",
                        f"Found {platform} verification code: {code}",
                        device_id=device_id,
                        context={"provider": provider, "platform": platform, "code": code})
            # Update verification status
            sync_execute(
                """UPDATE email_accounts SET verification_status = 'verified',
                   last_checked_at = now() WHERE id = %s""",
                (str(email_account["id"]),),
            )
        else:
            events.emit("persona", "warning", "email_verification_failed",
                        f"No {platform} verification code found in {provider}",
                        device_id=device_id,
                        context={"provider": provider, "platform": platform})

        return code

    except Exception:
        logger.error("Email verification failed", exc_info=True)
        events.emit("persona", "error", "email_verification_error",
                    "Exception during email verification",
                    device_id=device_id,
                    context={"provider": provider, "platform": platform})
        return None
    finally:
        wda.terminate_app(SAFARI)


def _read_protonmail(
    wda: WDASession,
    email: str,
    password: str,
    platform: str,
    timeout: int,
    poll_interval: int,
) -> str | None:
    """Read verification code from ProtonMail web interface."""
    # Open ProtonMail login
    wda.terminate_app(SAFARI)
    time.sleep(1)
    wda.launch_app(SAFARI)
    time.sleep(2)
    wda.open_url("https://account.proton.me/login")
    time.sleep(5)

    # Enter email
    email_field = wda.find_element(
        "predicate string",
        'type == "XCUIElementTypeTextField" AND (name CONTAINS "Email" OR name CONTAINS "email")',
    )
    if not email_field:
        # Try generic text field
        email_field = wda.find_element(
            "predicate string", 'type == "XCUIElementTypeTextField"',
        )
    if email_field:
        wda.element_click(email_field["ELEMENT"])
        time.sleep(0.5)
        wda.type_text(email)
        time.sleep(1)

    # Enter password
    pw_field = wda.find_element(
        "predicate string", 'type == "XCUIElementTypeSecureTextField"',
    )
    if pw_field:
        wda.element_click(pw_field["ELEMENT"])
        time.sleep(0.5)
        wda.type_text(password)
        time.sleep(1)

    # Click sign in
    for label in ["Sign in", "Log in"]:
        btn = wda.find_element("predicate string", f'name == "{label}"')
        if btn:
            wda.element_click(btn["ELEMENT"])
            break
    time.sleep(8)  # ProtonMail login is slow

    # Wait for inbox and search for platform email
    senders = PLATFORM_SENDERS.get(platform, [platform])
    deadline = time.time() + timeout

    while time.time() < deadline:
        # Look for message matching platform sender in visible elements
        code = _scan_inbox_elements(wda, senders)
        if code:
            return code

        # Try scrolling inbox
        wda.swipe_up(duration=0.5)
        time.sleep(2)

        code = _scan_inbox_elements(wda, senders)
        if code:
            return code

        # Refresh — pull down
        size = wda.screen_size()
        wda.swipe(
            size["width"] // 2, 200,
            size["width"] // 2, 500,
            duration=0.3,
        )
        time.sleep(poll_interval)

    return None


def _read_outlook(
    wda: WDASession,
    email: str,
    password: str,
    platform: str,
    timeout: int,
    poll_interval: int,
) -> str | None:
    """Read verification code from Outlook web interface."""
    wda.terminate_app(SAFARI)
    time.sleep(1)
    wda.launch_app(SAFARI)
    time.sleep(2)
    wda.open_url("https://outlook.live.com/mail/")
    time.sleep(5)

    # Login flow — Outlook has multi-step login
    email_field = wda.find_element(
        "predicate string", 'type == "XCUIElementTypeTextField"',
    )
    if email_field:
        wda.element_click(email_field["ELEMENT"])
        time.sleep(0.5)
        wda.type_text(email)
        time.sleep(1)

    # Click Next
    for label in ["Next", "next", "Sign in"]:
        btn = wda.find_element("predicate string", f'name == "{label}"')
        if btn:
            wda.element_click(btn["ELEMENT"])
            break
    time.sleep(3)

    # Password
    pw_field = wda.find_element(
        "predicate string", 'type == "XCUIElementTypeSecureTextField"',
    )
    if pw_field:
        wda.element_click(pw_field["ELEMENT"])
        time.sleep(0.5)
        wda.type_text(password)
        time.sleep(1)

    for label in ["Sign in", "Next"]:
        btn = wda.find_element("predicate string", f'name == "{label}"')
        if btn:
            wda.element_click(btn["ELEMENT"])
            break
    time.sleep(5)

    # Dismiss "Stay signed in?" dialog
    no_btn = wda.find_element("predicate string", 'name == "No"')
    if no_btn:
        wda.element_click(no_btn["ELEMENT"])
        time.sleep(3)

    # Search inbox
    senders = PLATFORM_SENDERS.get(platform, [platform])
    deadline = time.time() + timeout

    while time.time() < deadline:
        code = _scan_inbox_elements(wda, senders)
        if code:
            return code

        wda.swipe_up(duration=0.5)
        time.sleep(poll_interval)

    return None


def _read_mailtm(
    wda: WDASession,
    email: str,
    password: str,
    platform: str,
    timeout: int,
    poll_interval: int,
) -> str | None:
    """Read verification code from mail.tm web interface."""
    wda.terminate_app(SAFARI)
    time.sleep(1)
    wda.launch_app(SAFARI)
    time.sleep(2)
    wda.open_url("https://mail.tm/en/")
    time.sleep(5)

    # Login
    login_btn = wda.find_element("predicate string", 'name CONTAINS "Log in"')
    if login_btn:
        wda.element_click(login_btn["ELEMENT"])
        time.sleep(2)

    email_field = wda.find_element(
        "predicate string", 'type == "XCUIElementTypeTextField"',
    )
    if email_field:
        wda.element_click(email_field["ELEMENT"])
        time.sleep(0.5)
        wda.type_text(email)
        time.sleep(1)

    pw_field = wda.find_element(
        "predicate string", 'type == "XCUIElementTypeSecureTextField"',
    )
    if pw_field:
        wda.element_click(pw_field["ELEMENT"])
        time.sleep(0.5)
        wda.type_text(password)
        time.sleep(1)

    submit = wda.find_element("predicate string", 'name CONTAINS "Log in"')
    if submit:
        wda.element_click(submit["ELEMENT"])
    time.sleep(5)

    # Search inbox
    senders = PLATFORM_SENDERS.get(platform, [platform])
    deadline = time.time() + timeout

    while time.time() < deadline:
        code = _scan_inbox_elements(wda, senders)
        if code:
            return code

        time.sleep(poll_interval)

    return None


def _scan_inbox_elements(
    wda: WDASession,
    senders: list[str],
) -> str | None:
    """Scan visible WDA elements for a message from given senders containing a code.

    Strategy:
    1. Find any text element mentioning the sender name
    2. If found, tap it to open the message
    3. Scan message body elements for verification code pattern
    """
    # Look for sender in visible elements
    for sender in senders:
        el = wda.find_element(
            "predicate string",
            f'name CONTAINS[c] "{sender}" OR label CONTAINS[c] "{sender}"',
        )
        if el:
            # Found a message from this sender — tap to open
            wda.element_click(el["ELEMENT"])
            time.sleep(3)

            # Now scan for verification code in the opened message
            code = _extract_code_from_page(wda)
            if code:
                return code

            # Go back to inbox
            back = wda.find_element("predicate string", 'name CONTAINS "Back"')
            if back:
                wda.element_click(back["ELEMENT"])
                time.sleep(2)

    return None


def _extract_code_from_page(wda: WDASession) -> str | None:
    """Extract a verification code from the current page's visible elements.

    Searches all static text elements for numeric patterns.
    """
    # Get all static text elements
    elements = wda.find_elements(
        "class chain", "**/XCUIElementTypeStaticText",
    )
    if not elements:
        return None

    all_text = []
    for el in elements[:50]:  # limit to avoid timeout
        el_id = el.get("ELEMENT", "")
        if not el_id:
            continue
        try:
            resp = wda.client.get(
                f"{wda._s}/element/{el_id}/text",
                timeout=5,
            )
            text = resp.json().get("value", "")
            if text:
                all_text.append(text)
        except Exception:
            continue

    # Join all text and search for codes
    full_text = " ".join(all_text)

    for pattern in CODE_PATTERNS:
        match = pattern.search(full_text)
        if match:
            code = match.group(1)
            # Filter out unlikely codes (years, common numbers)
            if code in ("2024", "2025", "2026", "0000", "1234"):
                continue
            logger.info("Found verification code: %s", code)
            return code

    return None
