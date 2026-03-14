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
PLATFORM_PRIORITY = ["tiktok", "instagram", "reddit", "youtube_shorts", "facebook", "linkedin"]

# Platforms that use app-based signup
APP_PLATFORMS = {"tiktok", "instagram"}
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
           WHERE persona_id = %s AND status = 'available'
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
    if email_row.get("provider") == "mailtm":
        # mail.tm uses REST API — pass None for imap_config, use API-based polling
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
    email_pw = password if email_row.get("provider") == "mailtm" else None

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

    # Rotate IP
    wda.toggle_airplane_mode()

    if platform == "reddit":
        return _signup_reddit(wda, auto, persona, email, password, device_id)
    elif platform == "youtube_shorts":
        return _signup_youtube(wda, auto, persona, email, password, device_id)
    elif platform == "facebook":
        return _signup_facebook(wda, auto, persona, email, password, device_id)
    elif platform == "linkedin":
        return _signup_linkedin(wda, auto, persona, email, password, device_id)
    return None


def _derive_username(persona: dict, platform: str) -> str:
    """Derive a platform-specific username from persona's username_base."""
    base = persona.get("username_base", "user123")
    suffixes = {
        "reddit": "",
        "youtube_shorts": "",
        "facebook": "",
        "linkedin": "",
    }
    return base.replace(".", "_") + suffixes.get(platform, "")


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


def _signup_reddit(
    wda: WDASession,
    auto: DeviceAutomation,
    persona: dict,
    email: str,
    password: str,
    device_id: str | None,
) -> dict | None:
    """Reddit signup flow via Safari."""
    username = _derive_username(persona, "reddit")

    try:
        open_safari(wda, "https://www.reddit.com/register")
        time.sleep(4)
        auto.dismiss_popups(max_attempts=2)

        # Email field
        email_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField" AND (name CONTAINS[c] "email")'
        )
        if email_field:
            wda.element_click(email_field["ELEMENT"])
            auto.human_delay()
            wda.element_value(email_field["ELEMENT"], email)
            time.sleep(1)

        _click_any(wda, ["Continue", "Next"])
        time.sleep(3)

        # Username
        user_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField" AND (name CONTAINS[c] "username")'
        )
        if user_field:
            wda.element_click(user_field["ELEMENT"])
            auto.human_delay()
            wda.element_value(user_field["ELEMENT"], username)
            time.sleep(1)

        # Password
        pw_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeSecureTextField"'
        )
        if pw_field:
            wda.element_click(pw_field["ELEMENT"])
            auto.human_delay()
            wda.element_value(pw_field["ELEMENT"], password)
            time.sleep(1)

        _click_any(wda, ["Sign Up", "Sign up", "Continue"])
        time.sleep(5)

        # Handle CAPTCHA
        screenshot = wda.screenshot()
        if screenshot:
            from sovi.auth.captcha_solver import solve_image
            solve_image(screenshot, platform="reddit", device_id=device_id)
            time.sleep(5)

        auto.dismiss_popups(max_attempts=3)
        close_safari(wda)

        # Store in DB
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
