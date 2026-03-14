"""Persona-aware platform account creation.

Orchestrates account creation across all platforms for a persona,
using the persona's identity (name, bio, DOB, photos) instead of
random generation.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sovi import events
from sovi.auth.email_verifier import ImapConfig
from sovi.crypto import decrypt
from sovi.db import sync_execute, sync_execute_one
from sovi.device.account_creator import create_account
from sovi.device.wda_client import DeviceAutomation, WDASession
from sovi.persona.email_creator import SAFARI_BUNDLE, close_safari, open_safari

logger = logging.getLogger(__name__)

# Platform signup priorities
PLATFORM_PRIORITY = ["tiktok", "instagram", "x_twitter", "reddit", "youtube_shorts", "facebook", "linkedin"]

# Platforms that use app-based signup
APP_PLATFORMS = {"tiktok", "instagram", "x_twitter"}
# Platforms that use Safari web signup
WEB_PLATFORMS = {"reddit", "youtube_shorts", "facebook", "linkedin"}


def create_account_for_persona(
    wda: WDASession,
    persona: dict,
    platform: str,
    *,
    device_id: str | None = None,
) -> dict | None:
    """Create a platform account for a persona using their identity.

    Looks up persona's email account, then creates the platform account
    using persona data instead of random generation.

    Returns the created account dict or None on failure.
    """
    persona_id = str(persona["id"])

    # Get persona's email account
    email_row = sync_execute_one(
        """SELECT id, email, password, provider, imap_host, imap_port
           FROM email_accounts
           WHERE persona_id = %s AND status IN ('available', 'assigned')
           ORDER BY created_at DESC LIMIT 1""",
        (persona_id,),
    )
    if not email_row:
        logger.error("No email account found for persona %s", persona_id)
        events.emit("persona", "error", "no_email_for_persona",
                    f"Cannot create {platform} account: no email for persona {persona.get('display_name', '?')}",
                    device_id=device_id,
                    context={"persona_id": persona_id, "platform": platform})
        return None

    email = decrypt(email_row["email"])
    password = decrypt(email_row["password"])
    email_account_id = str(email_row["id"])

    # Build email verification config based on provider
    provider = email_row.get("provider", "")
    if provider in ("mailtm", "protonmail"):
        # mail.tm uses REST API, ProtonMail has no IMAP without Bridge
        imap_config = None
    else:
        imap_config = ImapConfig(
            host=email_row["imap_host"],
            username=email,
            password=password,
            port=email_row["imap_port"],
        )

    events.emit("persona", "info", "platform_account_creation_started",
                f"Creating {platform} account for {persona.get('display_name', '?')}",
                device_id=device_id,
                context={"persona_id": persona_id, "platform": platform})

    # For mail.tm accounts, pass the email password for API-based code polling
    email_pw = password if provider == "mailtm" else None

    if platform in APP_PLATFORMS:
        result = _create_app_account(wda, persona, platform, email, password, imap_config, device_id, email_password=email_pw)
    elif platform in WEB_PLATFORMS:
        result = _create_web_account(wda, persona, platform, email, password, imap_config, device_id, email_password=email_pw)
    else:
        logger.error("Unknown platform: %s", platform)
        return None

    if result:
        # Link account to persona and email
        sync_execute(
            "UPDATE accounts SET persona_id = %s, email_account_id = %s WHERE id = %s",
            (persona_id, email_account_id, str(result["id"])),
        )

        # Mark email as assigned
        sync_execute(
            "UPDATE email_accounts SET status = 'assigned', updated_at = now() WHERE id = %s",
            (email_account_id,),
        )

        events.emit("persona", "info", "platform_account_created",
                    f"Created {platform} account for {persona.get('display_name', '?')}: {result.get('username', '?')}",
                    device_id=device_id,
                    context={
                        "persona_id": persona_id,
                        "platform": platform,
                        "username": result.get("username"),
                        "account_id": str(result["id"]),
                    })

    return result


def _create_app_account(
    wda: WDASession,
    persona: dict,
    platform: str,
    email: str,
    password: str,
    imap_config: ImapConfig | None,
    device_id: str | None,
    *,
    email_password: str | None = None,
) -> dict | None:
    """Create account via app (TikTok, Instagram) using persona data.

    Delegates to existing account_creator.create_account but passes
    persona's niche_id.
    """
    niche_id = persona["niche_id"]
    return create_account(
        wda, platform, niche_id, email, password,
        imap_config=imap_config, email_password=email_password, device_id=device_id,
    )


def _create_web_account(
    wda: WDASession,
    persona: dict,
    platform: str,
    email: str,
    password: str,
    imap_config: ImapConfig | None,
    device_id: str | None,
    *,
    email_password: str | None = None,
) -> dict | None:
    """Create account via Safari web signup."""
    auto = DeviceAutomation(wda)

    # Skip airplane mode toggle for web signup — it disconnects WDA
    # IP rotation is handled externally if needed

    if platform == "reddit":
        return _signup_reddit(wda, auto, persona, email, password, device_id, email_password=email_password)
    elif platform == "youtube_shorts":
        return _signup_youtube(wda, auto, persona, email, password, device_id)
    elif platform == "facebook":
        return _signup_facebook(wda, auto, persona, email, password, device_id)
    elif platform == "linkedin":
        return _signup_linkedin(wda, auto, persona, email, password, device_id)
    return None


def _derive_username(persona: dict, platform: str) -> str:
    """Derive a platform-specific username from persona's username_base.

    Adds random digits to avoid collisions on platforms with taken usernames.
    """
    import random
    base = persona.get("username_base", "user123").replace(".", "_")
    suffix = str(random.randint(100, 9999))
    return base + suffix


def _store_account(
    persona: dict,
    platform: str,
    username: str,
    email: str,
    password: str,
    device_id: str | None,
) -> dict | None:
    """Store a newly created account in the DB."""
    from sovi.auth.totp import generate_secret
    from sovi.crypto import encrypt

    totp_secret = generate_secret()
    niche_id = str(persona["niche_id"])

    rows = sync_execute(
        """INSERT INTO accounts
           (platform, username, email_enc, password_enc, totp_secret_enc,
            niche_id, device_id, current_state, warming_day_count)
           VALUES (%s, %s, %s, %s, %s, %s, %s, 'created', 0)
           RETURNING id, platform, username, current_state""",
        (
            platform, username,
            encrypt(email).encode(), encrypt(password).encode(),
            encrypt(totp_secret).encode(),
            niche_id, device_id,
        ),
    )
    return rows[0] if rows else None


def _get_element_rect(wda: WDASession, element_id: str) -> dict | None:
    """Get element bounding rect {x, y, width, height} via WDA."""
    try:
        resp = wda.client.get(f"{wda._s}/element/{element_id}/rect")
        return resp.json().get("value")
    except Exception:
        return None


def _logout_reddit(wda: WDASession) -> bool:
    """Log out of Reddit in Safari by tapping user avatar → Log Out.

    Returns True if logout was performed (or user wasn't logged in).
    """
    try:
        open_safari(wda, "https://www.reddit.com/")
        time.sleep(6)  # Extra time for page to fully render

        # Dismiss any "Sign in with Google" or cookie popups first
        alert = wda.get_alert_text()
        if alert:
            wda.accept_alert()
            time.sleep(1)

        # Check if logged in by looking for "Expand user menu" avatar
        avatar = wda.find_element(
            "predicate string",
            'name CONTAINS[c] "Expand user menu"',
        )
        if not avatar:
            logger.info("Not logged into Reddit — no logout needed")
            return True

        # Try tapping the avatar up to 2 times (use short 100ms press for web)
        for attempt in range(2):
            wda.tap(361, 86, duration=100)
            time.sleep(3)

            logout = wda.find_element("predicate string", 'name == "Log Out"')
            if not logout:
                logout = wda.find_element(
                    "predicate string",
                    'name CONTAINS[c] "Log Out"',
                )
            if logout:
                rect = _get_element_rect(wda, logout["ELEMENT"])
                if rect and rect["y"] >= 0:
                    wda.tap(
                        rect["x"] + rect["width"] // 2,
                        rect["y"] + rect["height"] // 2,
                        duration=100,
                    )
                else:
                    wda.element_click(logout["ELEMENT"])
                time.sleep(3)
                logger.info("Logged out of Reddit (attempt %d)", attempt + 1)
                return True

            if attempt == 0:
                logger.debug("Log Out not found, retrying avatar tap...")

        logger.warning("Log Out button not found after 2 attempts")
        return False
    except Exception:
        logger.warning("Reddit logout failed", exc_info=True)
        return False


def _signup_reddit(
    wda: WDASession,
    auto: DeviceAutomation,
    persona: dict,
    email: str,
    password: str,
    device_id: str | None,
    *,
    email_password: str | None = None,
) -> dict | None:
    """Reddit signup flow via Safari.

    Multi-step: email → verify code → username/password → done.
    Uses mail.tm API to fetch verification codes.
    """
    username = _derive_username(persona, "reddit")

    try:
        # Kill any existing Safari session to start fresh
        close_safari(wda)
        time.sleep(1)

        # Log out of any existing Reddit session
        _logout_reddit(wda)

        # If logout failed, force-kill Safari to avoid stale session
        close_safari(wda)
        time.sleep(2)

        # Try loading register page with retry on failure
        email_field = None
        for load_attempt in range(2):
            if load_attempt > 0:
                logger.info("Retrying register page load (attempt %d)...", load_attempt + 1)
                close_safari(wda)
                time.sleep(5)

            open_safari(wda, "https://www.reddit.com/register")

            # Wait for page to load — don't call dismiss_popups here because
            # it would click Reddit's "Close" (X) button and close the signup modal
            time.sleep(5)

            # Only dismiss system alerts (not in-app buttons)
            alert = wda.get_alert_text()
            if alert:
                wda.accept_alert()
                time.sleep(1)

            email_field = _wait_for_element(
                wda, "predicate string",
                'type == "XCUIElementTypeTextField" AND (name CONTAINS[c] "email" OR label CONTAINS[c] "email")',
                timeout=15, label="Reddit email field",
            )
            if email_field:
                break

        if not email_field:
            logger.error("Reddit signup page didn't load — email field not found after retries")
            close_safari(wda)
            return None

        # Step 1: Enter email
        wda.element_click(email_field["ELEMENT"])
        auto.human_delay()
        wda.type_text(email)
        time.sleep(1)

        if not _click_any(wda, ["Continue", "Next"]):
            wda.tap(196, 706)
        time.sleep(4)

        # Step 2: Email verification code
        verify_field = _wait_for_element(
            wda, "predicate string",
            'type == "XCUIElementTypeTextField" AND (name CONTAINS[c] "erification" OR name CONTAINS[c] "code")',
            timeout=10, label="Reddit verification code field",
        )
        if verify_field and email_password:
            from sovi.persona.email_api import poll_for_code_mailtm
            logger.info("Polling mail.tm for Reddit verification code...")
            code = poll_for_code_mailtm(email, email_password, "reddit", timeout=90, poll_interval=5)
            if code:
                logger.info("Got Reddit verification code: %s", code)
                wda.element_click(verify_field["ELEMENT"])
                time.sleep(0.5)
                wda.type_text(code)
                time.sleep(1)
                _click_any(wda, ["Continue", "Next"])
                time.sleep(4)
            else:
                logger.warning("No verification code received — trying Skip")
                _click_any(wda, ["Skip"])
                time.sleep(3)
        elif verify_field:
            logger.warning("Verification needed but no email_password — trying Skip")
            _click_any(wda, ["Skip"])
            time.sleep(3)

        # Step 3: Username + Password page
        user_field = _wait_for_element(
            wda, "predicate string",
            'type == "XCUIElementTypeTextField" AND name CONTAINS[c] "username"',
            timeout=10, label="Reddit username field",
        )
        if not user_field:
            logger.warning("Username field not found — signup may have failed")
            close_safari(wda)
            return None

        # Clear pre-filled username and enter ours
        wda.element_click(user_field["ELEMENT"])
        time.sleep(0.5)
        wda.element_clear(user_field["ELEMENT"])
        time.sleep(0.5)
        wda.type_text(username)
        time.sleep(2)  # Wait for availability check

        # Enter password
        pw_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeSecureTextField"'
        )
        if pw_field:
            wda.element_click(pw_field["ELEMENT"])
            auto.human_delay()
            wda.type_text(password)
            time.sleep(1)

        # Submit
        if not _click_any(wda, ["Continue", "Sign Up", "Sign up"]):
            wda.tap(196, 706)
        time.sleep(5)

        # Handle CAPTCHA if present
        screenshot = wda.screenshot()
        if screenshot:
            from sovi.auth.captcha_solver import solve_image
            solve_image(screenshot, platform="reddit", device_id=device_id)
            time.sleep(5)

        auto.dismiss_popups(max_attempts=3)
        close_safari(wda)

        return _store_account(persona, "reddit", username, email, password, device_id)

    except Exception:
        logger.error("Reddit signup failed for %s", email, exc_info=True)
        close_safari(wda)
        return None


def _signup_youtube(
    wda: WDASession,
    auto: DeviceAutomation,
    persona: dict,
    email: str,
    password: str,
    device_id: str | None,
) -> dict | None:
    """YouTube/Google account signup via Safari.

    Note: Google signup almost always requires phone verification.
    """
    username = _derive_username(persona, "youtube_shorts")

    try:
        open_safari(wda, "https://accounts.google.com/signup")
        time.sleep(4)
        auto.dismiss_popups(max_attempts=2)

        # First name
        first_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField" AND (name CONTAINS[c] "first")'
        )
        if first_field:
            wda.element_value(first_field["ELEMENT"], persona["first_name"])
            auto.human_delay()

        # Last name
        last_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField" AND (name CONTAINS[c] "last")'
        )
        if last_field:
            wda.element_value(last_field["ELEMENT"], persona["last_name"])
            auto.human_delay()

        _click_any(wda, ["Next", "next"])
        time.sleep(3)

        # DOB and gender
        dob = persona.get("date_of_birth", "1995-06-15")
        if isinstance(dob, str):
            parts = dob.split("-")
            if len(parts) == 3:
                month_field = wda.find_element("predicate string", 'name CONTAINS[c] "month"')
                if month_field:
                    wda.element_click(month_field["ELEMENT"])
                    time.sleep(0.5)
                day_field = wda.find_element("predicate string", 'name CONTAINS[c] "day"')
                if day_field:
                    wda.element_value(day_field["ELEMENT"], parts[2])
                year_field = wda.find_element("predicate string", 'name CONTAINS[c] "year"')
                if year_field:
                    wda.element_value(year_field["ELEMENT"], parts[0])

        _click_any(wda, ["Next", "next"])
        time.sleep(3)

        # Choose "Create your own Gmail address"
        _click_any(wda, ["Create your own Gmail address", "create your own"])
        time.sleep(2)

        # Gmail username
        gmail_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField"'
        )
        if gmail_field:
            gmail_username = persona.get("username_base", username).replace(".", "")
            wda.element_value(gmail_field["ELEMENT"], gmail_username)
            auto.human_delay()

        _click_any(wda, ["Next", "next"])
        time.sleep(3)

        # Password
        pw_fields = wda.find_elements(
            "predicate string",
            'type == "XCUIElementTypeSecureTextField"'
        )
        if pw_fields:
            wda.element_value(pw_fields[0]["ELEMENT"], password)
            if len(pw_fields) > 1:
                wda.element_value(pw_fields[1]["ELEMENT"], password)  # Confirm
            auto.human_delay()

        _click_any(wda, ["Next", "next"])
        time.sleep(3)

        # Phone verification (almost always required)
        phone_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField" AND (name CONTAINS[c] "phone")'
        )
        if phone_field:
            from sovi.auth.sms_verifier import cancel_verification, request_number, wait_for_code
            sms = request_number("google")
            if sms:
                wda.element_value(phone_field["ELEMENT"], sms.phone_number)
                _click_any(wda, ["Next", "next"])
                time.sleep(5)

                code = wait_for_code(sms, timeout=120)
                if code:
                    code_field = wda.find_element(
                        "predicate string",
                        'type == "XCUIElementTypeTextField"'
                    )
                    if code_field:
                        wda.element_value(code_field["ELEMENT"], code)
                        _click_any(wda, ["Verify", "Next"])
                        time.sleep(3)
                else:
                    cancel_verification(sms)
                    close_safari(wda)
                    return None

        # Recovery email
        _click_any(wda, ["Skip", "Not now", "Next"])
        time.sleep(3)

        # Agree to terms
        _click_any(wda, ["I agree", "Agree", "Accept"])
        time.sleep(5)

        auto.dismiss_popups(max_attempts=3)
        close_safari(wda)

        return _store_account(persona, "youtube_shorts", username, email, password, device_id)

    except Exception:
        logger.error("YouTube signup failed for %s", email, exc_info=True)
        close_safari(wda)
        return None


def _signup_facebook(
    wda: WDASession,
    auto: DeviceAutomation,
    persona: dict,
    email: str,
    password: str,
    device_id: str | None,
) -> dict | None:
    """Facebook signup via Safari."""
    username = _derive_username(persona, "facebook")

    try:
        open_safari(wda, "https://www.facebook.com/reg/")
        time.sleep(4)
        auto.dismiss_popups(max_attempts=2)

        # First name
        first_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField" AND (name CONTAINS[c] "first")'
        )
        if first_field:
            wda.element_value(first_field["ELEMENT"], persona["first_name"])
            auto.human_delay()

        # Last name
        last_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField" AND (name CONTAINS[c] "last" OR name CONTAINS[c] "surname")'
        )
        if last_field:
            wda.element_value(last_field["ELEMENT"], persona["last_name"])
            auto.human_delay()

        # Email / phone
        email_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField" AND (name CONTAINS[c] "email" OR name CONTAINS[c] "mobile")'
        )
        if email_field:
            wda.element_value(email_field["ELEMENT"], email)
            auto.human_delay()

        # Re-enter email
        reenter = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField" AND name CONTAINS[c] "re-enter"'
        )
        if reenter:
            wda.element_value(reenter["ELEMENT"], email)

        # Password
        pw_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeSecureTextField"'
        )
        if pw_field:
            wda.element_value(pw_field["ELEMENT"], password)
            auto.human_delay()

        # DOB
        dob = persona.get("date_of_birth", "1995-06-15")
        if isinstance(dob, str):
            parts = dob.split("-")
            if len(parts) == 3:
                _set_dob_selects(wda, month=int(parts[1]), day=int(parts[2]), year=int(parts[0]))

        # Gender
        gender = persona.get("gender", "female")
        gender_el = wda.find_element(
            "predicate string",
            f'name CONTAINS[c] "{gender}"'
        )
        if gender_el:
            wda.element_click(gender_el["ELEMENT"])
            time.sleep(0.5)

        _click_any(wda, ["Sign Up", "Sign up", "Create Account"])
        time.sleep(5)

        auto.dismiss_popups(max_attempts=3)
        close_safari(wda)

        return _store_account(persona, "facebook", username, email, password, device_id)

    except Exception:
        logger.error("Facebook signup failed for %s", email, exc_info=True)
        close_safari(wda)
        return None


def _signup_linkedin(
    wda: WDASession,
    auto: DeviceAutomation,
    persona: dict,
    email: str,
    password: str,
    device_id: str | None,
) -> dict | None:
    """LinkedIn signup via Safari."""
    username = _derive_username(persona, "linkedin")

    try:
        open_safari(wda, "https://www.linkedin.com/signup")
        time.sleep(4)
        auto.dismiss_popups(max_attempts=2)

        # Email
        email_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField" AND (name CONTAINS[c] "email")'
        )
        if email_field:
            wda.element_value(email_field["ELEMENT"], email)
            auto.human_delay()

        # Password
        pw_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeSecureTextField"'
        )
        if pw_field:
            wda.element_value(pw_field["ELEMENT"], password)
            auto.human_delay()

        _click_any(wda, ["Agree & Join", "Join now", "Continue"])
        time.sleep(3)

        # First name
        first_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField" AND (name CONTAINS[c] "first")'
        )
        if first_field:
            wda.element_value(first_field["ELEMENT"], persona["first_name"])
            auto.human_delay()

        # Last name
        last_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField" AND (name CONTAINS[c] "last")'
        )
        if last_field:
            wda.element_value(last_field["ELEMENT"], persona["last_name"])
            auto.human_delay()

        _click_any(wda, ["Continue", "Next"])
        time.sleep(3)

        # Location / country — usually auto-detected
        _click_any(wda, ["Continue", "Next"])
        time.sleep(3)

        # Email verification
        # LinkedIn sends a verification code
        time.sleep(5)

        auto.dismiss_popups(max_attempts=3)
        close_safari(wda)

        return _store_account(persona, "linkedin", username, email, password, device_id)

    except Exception:
        logger.error("LinkedIn signup failed for %s", email, exc_info=True)
        close_safari(wda)
        return None


def _wait_for_element(
    wda: WDASession,
    strategy: str,
    selector: str,
    *,
    timeout: int = 10,
    label: str = "element",
) -> dict | None:
    """Poll for an element to appear, returning it or None on timeout."""
    for _ in range(timeout // 2):
        el = wda.find_element(strategy, selector)
        if el:
            return el
        time.sleep(2)
    logger.warning("Timed out waiting for %s", label)
    return None


def _click_any(wda: WDASession, labels: list[str]) -> bool:
    """Try to click an element with any of the given labels."""
    for label in labels:
        el = wda.find_element("accessibility id", label)
        if el:
            wda.element_click(el["ELEMENT"])
            return True
        el = wda.find_element(
            "predicate string",
            f'name == "{label}" OR label == "{label}"'
        )
        if el:
            wda.element_click(el["ELEMENT"])
            return True
    return False


def _set_dob_selects(wda: WDASession, month: int, day: int, year: int) -> None:
    """Set DOB via select elements (used by Facebook)."""
    # Facebook uses select dropdowns
    for name, val in [("month", str(month)), ("day", str(day)), ("year", str(year))]:
        el = wda.find_element("predicate string", f'name CONTAINS[c] "{name}"')
        if el:
            wda.element_click(el["ELEMENT"])
            time.sleep(0.5)
            # Try to find and click the value in the picker
            val_el = wda.find_element("predicate string", f'value == "{val}" OR name == "{val}"')
            if val_el:
                wda.element_click(val_el["ELEMENT"])
                time.sleep(0.3)
