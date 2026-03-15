"""On-device email verification reader -- opens webmail in Safari via WDA.

Replaces server-side IMAP (email_verifier.py) and mail.tm REST API (email_api.py)
with fully on-device email reading through the phone's cellular connection.

Supported providers:
- ProtonMail: login to account.proton.me, read inbox
- Outlook: login to outlook.live.com, read inbox
- mail.tm: login to mail.tm web UI, read inbox

All traffic flows through the phone's cellular connection.

Usage from device/account_creator.py (plain credentials):
    code = poll_verification_code(wda, email, password, "tiktok", provider="protonmail")

Usage from persona/account_creator.py (email_account dict):
    code = read_verification_code(wda, email_account_row, "reddit")
"""

from __future__ import annotations

import logging
import re
import time

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

# Verification code patterns (4-8 digit codes), ordered by likelihood
CODE_PATTERNS = [
    re.compile(r"\b(\d{6})\b"),      # 6-digit (most common)
    re.compile(r"\b(\d{8})\b"),      # 8-digit
    re.compile(r"\b(\d{5})\b"),      # 5-digit
    re.compile(r"\b(\d{4})\b"),      # 4-digit
]

# Verification URL patterns (links to click to verify)
URL_PATTERNS = [
    re.compile(r"(https?://[^\s\"'<>]+verify[^\s\"'<>]*)"),
    re.compile(r"(https?://[^\s\"'<>]+confirm[^\s\"'<>]*)"),
    re.compile(r"(https?://[^\s\"'<>]+activate[^\s\"'<>]*)"),
    re.compile(r"(https?://[^\s\"'<>]+validate[^\s\"'<>]*)"),
]

# Numbers to skip when extracting codes (years, common false positives)
_SKIP_CODES = frozenset({
    "0000", "1111", "1234", "1970", "2000",
    "2020", "2021", "2022", "2023", "2024", "2025", "2026", "2027",
})

# Provider login URLs
PROVIDER_URLS = {
    "protonmail": "https://account.proton.me/login",
    "outlook": "https://outlook.live.com/mail/",
    "mailtm": "https://mail.tm/en/",
}

# Provider auto-detection from email domain
_DOMAIN_TO_PROVIDER = {
    "proton.me": "protonmail",
    "protonmail.com": "protonmail",
    "pm.me": "protonmail",
    "outlook.com": "outlook",
    "hotmail.com": "outlook",
    "live.com": "outlook",
}


def _detect_provider(email: str) -> str:
    """Detect email provider from the email domain."""
    domain = email.rsplit("@", 1)[-1].lower()
    return _DOMAIN_TO_PROVIDER.get(domain, "mailtm")


# ---------------------------------------------------------------------------
# Public API: poll_verification_code (for device/account_creator.py)
# ---------------------------------------------------------------------------


def poll_verification_code(
    wda: WDASession,
    email: str,
    email_password: str,
    platform: str,
    *,
    provider: str | None = None,
    device_id: str | None = None,
    timeout: int = 120,
    poll_interval: int = 10,
) -> str | None:
    """Poll for a verification code on-device via Safari webmail.

    Convenience wrapper for callers that have plain email/password strings
    instead of an email_account dict. Detects provider from domain if not given.

    Args:
        wda: WDA session for the device
        email: plaintext email address
        email_password: plaintext email password
        platform: which platform's verification to look for
        provider: email provider name; auto-detected from domain if None
        device_id: for event logging
        timeout: total seconds to poll
        poll_interval: seconds between inbox refreshes

    Returns:
        Verification code string, or None if not found.
    """
    if provider is None:
        provider = _detect_provider(email)

    logger.info(
        "On-device email poll: %s verification from %s (%s)",
        platform, email, provider,
    )

    try:
        if provider == "protonmail":
            code = _read_protonmail(wda, email, email_password, platform, timeout, poll_interval)
        elif provider == "outlook":
            code = _read_outlook(wda, email, email_password, platform, timeout, poll_interval)
        elif provider == "mailtm":
            code = _read_mailtm(wda, email, email_password, platform, timeout, poll_interval)
        else:
            logger.error("Unsupported email provider for poll: %s", provider)
            return None

        if code:
            logger.info("On-device poll found %s code: %s", platform, code)
            events.emit("persona", "info", "ondevice_email_code_found",
                        f"Found {platform} verification code on device: {code}",
                        device_id=device_id,
                        context={"provider": provider, "platform": platform, "code": code})
        else:
            logger.warning("On-device poll: no %s code found in %s", platform, provider)

        return code

    except Exception:
        logger.error("On-device email poll failed", exc_info=True)
        return None
    finally:
        wda.terminate_app(SAFARI)


# ---------------------------------------------------------------------------
# Public API: read_verification_code (for persona/account_creator.py)
# ---------------------------------------------------------------------------


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
            email_id = email_account.get("id")
            if email_id:
                sync_execute(
                    """UPDATE email_accounts SET verification_status = 'verified',
                       last_checked_at = now() WHERE id = %s""",
                    (str(email_id),),
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


# ---------------------------------------------------------------------------
# Public API: read_verification_url (extract link instead of code)
# ---------------------------------------------------------------------------


def read_verification_url(
    wda: WDASession,
    email_account: dict,
    platform: str,
    *,
    device_id: str | None = None,
    timeout: int = 120,
    poll_interval: int = 10,
) -> str | None:
    """Read a verification URL from email on-device via Safari.

    Same flow as read_verification_code but extracts a URL instead of a numeric code.
    Useful for platforms that send "click to verify" emails.

    Returns:
        Verification URL string, or None if not found.
    """
    provider = email_account.get("provider", "")
    email = decrypt(email_account["email"])
    password = decrypt(email_account["password"])

    logger.info("Reading verification URL for %s from %s (%s)", platform, email, provider)

    try:
        # Login to the appropriate provider
        if provider == "protonmail":
            _login_protonmail(wda, email, password)
        elif provider == "outlook":
            _login_outlook(wda, email, password)
        elif provider == "mailtm":
            _login_mailtm(wda, email, password)
        else:
            logger.error("Unsupported email provider: %s", provider)
            return None

        # Search for verification email and extract URL
        senders = PLATFORM_SENDERS.get(platform, [platform])
        deadline = time.time() + timeout

        while time.time() < deadline:
            url = _scan_inbox_for_url(wda, senders)
            if url:
                logger.info("Found verification URL for %s: %s", platform, url[:80])
                return url

            # Refresh inbox
            _refresh_inbox(wda)
            time.sleep(poll_interval)

        return None

    except Exception:
        logger.error("Verification URL reading failed", exc_info=True)
        return None
    finally:
        wda.terminate_app(SAFARI)


# ---------------------------------------------------------------------------
# Provider login helpers (separated from read logic for reuse)
# ---------------------------------------------------------------------------


def _login_protonmail(wda: WDASession, email: str, password: str) -> None:
    """Login to ProtonMail webmail in Safari."""
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


def _login_outlook(wda: WDASession, email: str, password: str) -> None:
    """Login to Outlook webmail in Safari."""
    wda.terminate_app(SAFARI)
    time.sleep(1)
    wda.launch_app(SAFARI)
    time.sleep(2)
    wda.open_url("https://outlook.live.com/mail/")
    time.sleep(5)

    # Email
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


def _login_mailtm(wda: WDASession, email: str, password: str) -> None:
    """Login to mail.tm web UI in Safari."""
    wda.terminate_app(SAFARI)
    time.sleep(1)
    wda.launch_app(SAFARI)
    time.sleep(2)
    wda.open_url("https://mail.tm/en/")
    time.sleep(5)

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


# ---------------------------------------------------------------------------
# Provider read flows (login + poll loop)
# ---------------------------------------------------------------------------


def _read_protonmail(
    wda: WDASession,
    email: str,
    password: str,
    platform: str,
    timeout: int,
    poll_interval: int,
) -> str | None:
    """Read verification code from ProtonMail web interface."""
    _login_protonmail(wda, email, password)

    senders = PLATFORM_SENDERS.get(platform, [platform])
    deadline = time.time() + timeout

    while time.time() < deadline:
        code = _scan_inbox_elements(wda, senders)
        if code:
            return code

        # Try scrolling inbox
        wda.swipe_up(duration=0.5)
        time.sleep(2)

        code = _scan_inbox_elements(wda, senders)
        if code:
            return code

        # Refresh -- pull down
        _refresh_inbox(wda)
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
    _login_outlook(wda, email, password)

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
    _login_mailtm(wda, email, password)

    senders = PLATFORM_SENDERS.get(platform, [platform])
    deadline = time.time() + timeout

    while time.time() < deadline:
        code = _scan_inbox_elements(wda, senders)
        if code:
            return code

        time.sleep(poll_interval)

    return None


# ---------------------------------------------------------------------------
# Inbox scanning
# ---------------------------------------------------------------------------


def _refresh_inbox(wda: WDASession) -> None:
    """Pull-to-refresh gesture on inbox."""
    size = wda.screen_size()
    wda.swipe(
        size["width"] // 2, 200,
        size["width"] // 2, 500,
        duration=0.3,
    )


def _scan_inbox_elements(
    wda: WDASession,
    senders: list[str],
) -> str | None:
    """Scan visible WDA elements for a message from given senders containing a code.

    Strategy:
    1. Find any text element mentioning the sender name
    2. If found, tap it to open the message
    3. Scan message body elements for verification code pattern
    4. Fall back to page source if element scanning doesn't find a code
    """
    for sender in senders:
        el = wda.find_element(
            "predicate string",
            f'name CONTAINS[c] "{sender}" OR label CONTAINS[c] "{sender}"',
        )
        if el:
            # Found a message from this sender -- tap to open
            wda.element_click(el["ELEMENT"])
            time.sleep(3)

            # Try element-based extraction first
            code = _extract_code_from_page(wda)
            if code:
                return code

            # Fall back to page source (captures text in web views)
            code = _extract_code_from_source(wda)
            if code:
                return code

            # Go back to inbox
            back = wda.find_element("predicate string", 'name CONTAINS "Back"')
            if back:
                wda.element_click(back["ELEMENT"])
                time.sleep(2)
            else:
                # Try swipe-from-left as back gesture
                size = wda.screen_size()
                wda.swipe(0, size["height"] // 2, size["width"] // 2, size["height"] // 2, duration=0.3)
                time.sleep(2)

    return None


def _scan_inbox_for_url(
    wda: WDASession,
    senders: list[str],
) -> str | None:
    """Scan inbox for a verification URL from given senders."""
    for sender in senders:
        el = wda.find_element(
            "predicate string",
            f'name CONTAINS[c] "{sender}" OR label CONTAINS[c] "{sender}"',
        )
        if el:
            wda.element_click(el["ELEMENT"])
            time.sleep(3)

            url = _extract_url_from_page(wda)
            if url:
                return url

            # Fall back to page source
            url = _extract_url_from_source(wda)
            if url:
                return url

            # Go back
            back = wda.find_element("predicate string", 'name CONTAINS "Back"')
            if back:
                wda.element_click(back["ELEMENT"])
                time.sleep(2)

    return None


# ---------------------------------------------------------------------------
# Code / URL extraction
# ---------------------------------------------------------------------------


def _extract_code_from_page(wda: WDASession) -> str | None:
    """Extract a verification code from the current page's visible elements.

    Searches all static text elements for numeric patterns.
    """
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

    full_text = " ".join(all_text)
    return _find_code_in_text(full_text)


def _extract_code_from_source(wda: WDASession) -> str | None:
    """Extract verification code from WDA page source XML.

    Falls back to page source when element text scanning misses content
    in web views (e.g., ProtonMail renders email body in a web view
    that may not expose individual text elements).
    """
    try:
        source = wda.source()
        if not source:
            return None
        return _find_code_in_text(source)
    except Exception:
        logger.debug("Failed to get page source for code extraction", exc_info=True)
        return None


def _extract_url_from_page(wda: WDASession) -> str | None:
    """Extract a verification URL from visible link elements."""
    # Look for links with verify/confirm keywords
    for keyword in ["verify", "confirm", "activate", "validate"]:
        el = wda.find_element(
            "predicate string",
            f'type == "XCUIElementTypeLink" AND (name CONTAINS[c] "{keyword}" OR label CONTAINS[c] "{keyword}")',
        )
        if el:
            # Get the href/URL from the element name or label
            el_id = el["ELEMENT"]
            try:
                resp = wda.client.get(f"{wda._s}/element/{el_id}/attribute/name", timeout=5)
                name = resp.json().get("value", "")
                if name and name.startswith("http"):
                    return name
            except Exception:
                pass

    # Fall back to scanning static text for URLs
    elements = wda.find_elements(
        "class chain", "**/XCUIElementTypeStaticText",
    )
    all_text = []
    for el in elements[:50]:
        el_id = el.get("ELEMENT", "")
        if not el_id:
            continue
        try:
            resp = wda.client.get(f"{wda._s}/element/{el_id}/text", timeout=5)
            text = resp.json().get("value", "")
            if text:
                all_text.append(text)
        except Exception:
            continue

    full_text = " ".join(all_text)
    return _find_url_in_text(full_text)


def _extract_url_from_source(wda: WDASession) -> str | None:
    """Extract verification URL from WDA page source."""
    try:
        source = wda.source()
        if not source:
            return None
        return _find_url_in_text(source)
    except Exception:
        return None


def _find_code_in_text(text: str) -> str | None:
    """Find a verification code in arbitrary text."""
    for pattern in CODE_PATTERNS:
        for match in pattern.finditer(text):
            code = match.group(1)
            if code not in _SKIP_CODES:
                logger.info("Found verification code: %s", code)
                return code
    return None


def _find_url_in_text(text: str) -> str | None:
    """Find a verification URL in arbitrary text."""
    for pattern in URL_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None
