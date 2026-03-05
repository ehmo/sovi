"""Email account creation automation via Safari + WDA.

Automates Outlook and Mail.com account signup on-device. Each persona
needs at least one email before platform accounts can be created.
"""

from __future__ import annotations

import logging
import random
import string
import time
from typing import Any

from sovi import events
from sovi.auth.captcha_solver import solve_funcaptcha, solve_image
from sovi.auth.sms_verifier import cancel_verification, request_number, wait_for_code
from sovi.crypto import encrypt
from sovi.db import sync_execute, sync_execute_one
from sovi.device.wda_client import DeviceAutomation, WDASession

logger = logging.getLogger(__name__)

# Safari bundle ID
SAFARI_BUNDLE = "com.apple.mobilesafari"

# Provider configs
PROVIDERS = {
    "outlook": {
        "signup_url": "https://signup.live.com/signup",
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
        "domains": ["outlook.com", "hotmail.com"],
    },
    "mailcom": {
        "signup_url": "https://mail.com/int/",
        "imap_host": "imap.mail.com",
        "imap_port": 993,
        "domains": [
            "mail.com", "email.com", "usa.com", "post.com",
            "engineer.com", "consultant.com", "programmer.net",
            "techie.com", "myself.com", "writeme.com",
        ],
    },
}


def _generate_password() -> str:
    """Generate a strong random password."""
    chars = string.ascii_letters + string.digits + "!@#$%"
    # Ensure at least one of each type
    pw = [
        random.choice(string.ascii_uppercase),
        random.choice(string.ascii_lowercase),
        random.choice(string.digits),
        random.choice("!@#$%"),
    ]
    pw.extend(random.choices(chars, k=12))
    random.shuffle(pw)
    return "".join(pw)


def _derive_email_username(persona: dict, provider: str) -> str:
    """Derive an email username from persona data."""
    first = persona["first_name"].lower().replace(" ", "")
    last = persona["last_name"].lower().replace(" ", "")
    age = persona.get("age", 28)
    birth_year = str(2026 - age)[-2:]

    variants = [
        f"{first}.{last}",
        f"{first}{last}{birth_year}",
        f"{first}.{last}{birth_year}",
        f"{first}_{last}",
        f"{first}{last[0]}{birth_year}",
    ]
    return random.choice(variants)


def open_safari(wda: WDASession, url: str) -> None:
    """Launch Safari and navigate to URL."""
    wda.launch_app(SAFARI_BUNDLE)
    time.sleep(2)

    # Use WDA URL endpoint to navigate
    try:
        wda.client.post(f"{wda._s}/url", json={"url": url})
        time.sleep(3)
    except Exception:
        logger.warning("Direct URL navigation failed, trying address bar")
        # Fallback: tap address bar and type URL
        _type_in_address_bar(wda, url)


def close_safari(wda: WDASession) -> None:
    """Close Safari cleanly."""
    wda.terminate_app(SAFARI_BUNDLE)
    time.sleep(1)


def _type_in_address_bar(wda: WDASession, url: str) -> None:
    """Tap Safari address bar and type a URL."""
    # Tap the address/URL bar area at the bottom of Safari
    size = wda.screen_size()
    wda.tap(size["width"] // 2, size["height"] - 50)
    time.sleep(1)

    # Clear existing text and type URL
    addr_field = wda.find_element(
        "predicate string",
        'type == "XCUIElementTypeTextField" OR type == "XCUIElementTypeSearchField"'
    )
    if addr_field:
        wda.element_click(addr_field["ELEMENT"])
        time.sleep(0.5)
        # Select all and type new URL
        wda.element_value(addr_field["ELEMENT"], url)
        time.sleep(0.5)
        # Press Go/Return
        wda.tap(size["width"] - 30, size["height"] - 50)
        time.sleep(3)


def create_email_for_persona(
    wda: WDASession,
    persona: dict,
    provider: str = "outlook",
    *,
    device_id: str | None = None,
) -> dict | None:
    """Create an email account on-device for a persona.

    1. Toggle airplane mode (fresh IP)
    2. Open Safari, navigate to signup page
    3. Fill form with persona data
    4. Handle CAPTCHA or phone verification
    5. Store credentials encrypted in email_accounts table

    Returns the email_account dict or None on failure.
    """
    if provider not in PROVIDERS:
        logger.error("Unknown email provider: %s", provider)
        return None

    config = PROVIDERS[provider]
    persona_id = str(persona["id"])
    auto = DeviceAutomation(wda)

    # Derive email address
    username = _derive_email_username(persona, provider)
    domain = random.choice(config["domains"])
    email_address = f"{username}@{domain}"
    password = _generate_password()

    events.emit("persona", "info", "email_creation_started",
                f"Creating {provider} email for {persona.get('display_name', '?')}: {email_address}",
                device_id=device_id,
                context={"provider": provider, "persona_id": persona_id})

    try:
        # Step 0: Rotate IP
        wda.toggle_airplane_mode()

        # Step 1: Open signup page
        open_safari(wda, config["signup_url"])
        time.sleep(3)
        auto.dismiss_popups(max_attempts=2)

        # Step 2: Provider-specific signup flow
        if provider == "outlook":
            success = _signup_outlook(wda, auto, persona, email_address, password, device_id)
        elif provider == "mailcom":
            success = _signup_mailcom(wda, auto, persona, email_address, password, device_id)
        else:
            success = False

        if not success:
            events.emit("persona", "error", "email_creation_failed",
                        f"Email signup failed for {email_address}",
                        device_id=device_id,
                        context={"provider": provider, "persona_id": persona_id})
            return None

        # Step 3: Store in DB
        row = sync_execute_one(
            """INSERT INTO email_accounts
               (persona_id, provider, email, password, imap_host, imap_port, domain, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'available')
               RETURNING id, provider, domain, status""",
            (
                persona_id, provider,
                encrypt(email_address), encrypt(password),
                config["imap_host"], config["imap_port"],
                domain,
            ),
        )

        if not row:
            logger.error("Failed to insert email account into DB")
            return None

        events.emit("persona", "info", "email_created",
                    f"Created {provider} email for {persona.get('display_name', '?')}",
                    device_id=device_id,
                    context={
                        "provider": provider,
                        "persona_id": persona_id,
                        "email_account_id": str(row["id"]),
                    })

        logger.info("Email created: %s (id=%s)", email_address, row["id"])
        return dict(row)

    except Exception:
        logger.error("Email creation failed for %s", persona.get("display_name", "?"), exc_info=True)
        events.emit("persona", "error", "email_creation_error",
                    f"Unhandled error creating email for {persona.get('display_name', '?')}",
                    device_id=device_id,
                    context={"provider": provider, "persona_id": persona_id})
        return None
    finally:
        close_safari(wda)


def _signup_outlook(
    wda: WDASession,
    auto: DeviceAutomation,
    persona: dict,
    email: str,
    password: str,
    device_id: str | None,
) -> bool:
    """Outlook/Hotmail signup flow via Safari."""
    try:
        # Get email address (the part before @)
        local_part = email.split("@")[0]

        # Enter email address
        email_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField"'
        )
        if email_field:
            wda.element_click(email_field["ELEMENT"])
            auto.human_delay()
            wda.element_value(email_field["ELEMENT"], local_part)
            time.sleep(1)

        # Click Next
        _click_button(wda, ["Next", "next"])
        time.sleep(3)

        # Enter password
        pw_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeSecureTextField"'
        )
        if pw_field:
            wda.element_click(pw_field["ELEMENT"])
            auto.human_delay()
            wda.element_value(pw_field["ELEMENT"], password)
            time.sleep(1)

        _click_button(wda, ["Next", "next"])
        time.sleep(3)

        # Enter name
        first_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField" AND (name CONTAINS "first" OR name CONTAINS "First")'
        )
        if first_field:
            wda.element_value(first_field["ELEMENT"], persona["first_name"])
            auto.human_delay()

        last_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField" AND (name CONTAINS "last" OR name CONTAINS "Last")'
        )
        if last_field:
            wda.element_value(last_field["ELEMENT"], persona["last_name"])
            auto.human_delay()

        _click_button(wda, ["Next", "next"])
        time.sleep(3)

        # Date of birth
        dob = persona.get("date_of_birth", "1995-06-15")
        if isinstance(dob, str):
            parts = dob.split("-")
            if len(parts) == 3:
                # Try to set month/day/year pickers or fields
                _set_dob_fields(wda, month=int(parts[1]), day=int(parts[2]), year=int(parts[0]))
                time.sleep(1)

        _click_button(wda, ["Next", "next"])
        time.sleep(3)

        # Handle CAPTCHA
        screenshot = wda.screenshot()
        if screenshot:
            # Try FunCaptcha first (common on Outlook)
            solve_funcaptcha(
                "B7D8911C-5CC8-A9A3-35B0-554ACEE604DA",
                "https://signup.live.com/signup",
                platform="outlook",
                device_id=device_id,
            )
            time.sleep(5)

        # Handle phone verification if required
        phone_el = wda.find_element(
            "predicate string",
            'name CONTAINS "phone" OR name CONTAINS "Phone" OR name CONTAINS "mobile"'
        )
        if phone_el:
            sms = request_number("outlook")
            if sms:
                wda.element_value(phone_el["ELEMENT"], sms.phone_number)
                _click_button(wda, ["Send code", "Send Code", "Next"])
                time.sleep(5)

                code = wait_for_code(sms, timeout=90)
                if code:
                    code_field = wda.find_element(
                        "predicate string",
                        'type == "XCUIElementTypeTextField"'
                    )
                    if code_field:
                        wda.element_value(code_field["ELEMENT"], code)
                        _click_button(wda, ["Next", "Verify"])
                        time.sleep(3)
                else:
                    cancel_verification(sms)
                    return False

        auto.dismiss_popups(max_attempts=3)
        logger.info("Outlook signup completed for %s", email)
        return True

    except Exception:
        logger.error("Outlook signup failed for %s", email, exc_info=True)
        return False


def _signup_mailcom(
    wda: WDASession,
    auto: DeviceAutomation,
    persona: dict,
    email: str,
    password: str,
    device_id: str | None,
) -> bool:
    """Mail.com signup flow via Safari."""
    try:
        # Click "Sign up" / free email
        _click_button(wda, ["Sign up", "Free email", "Create account", "Get started"])
        time.sleep(3)

        # Enter desired email address
        email_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField"'
        )
        if email_field:
            local_part = email.split("@")[0]
            wda.element_click(email_field["ELEMENT"])
            auto.human_delay()
            wda.element_value(email_field["ELEMENT"], local_part)
            time.sleep(1)

        # Domain selection — mail.com offers 20+ domains
        # Try to select the domain dropdown if visible
        domain = email.split("@")[1] if "@" in email else "mail.com"
        domain_el = wda.find_element(
            "predicate string",
            f'name CONTAINS "{domain}" OR name CONTAINS "domain"'
        )
        if domain_el:
            wda.element_click(domain_el["ELEMENT"])
            time.sleep(1)

        _click_button(wda, ["Next", "Continue", "Check availability"])
        time.sleep(3)

        # Enter name
        first_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField" AND (name CONTAINS "first" OR name CONTAINS "First")'
        )
        if first_field:
            wda.element_value(first_field["ELEMENT"], persona["first_name"])
            auto.human_delay()

        last_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField" AND (name CONTAINS "last" OR name CONTAINS "Last")'
        )
        if last_field:
            wda.element_value(last_field["ELEMENT"], persona["last_name"])
            auto.human_delay()

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

        # DOB
        dob = persona.get("date_of_birth", "1995-06-15")
        if isinstance(dob, str):
            parts = dob.split("-")
            if len(parts) == 3:
                _set_dob_fields(wda, month=int(parts[1]), day=int(parts[2]), year=int(parts[0]))

        # Gender
        gender = persona.get("gender", "female")
        gender_el = wda.find_element(
            "predicate string",
            f'name CONTAINS "{gender}" OR name CONTAINS "{gender.capitalize()}"'
        )
        if gender_el:
            wda.element_click(gender_el["ELEMENT"])
            time.sleep(0.5)

        _click_button(wda, ["Create account", "Register", "Sign up", "Continue"])
        time.sleep(5)

        # Handle CAPTCHA
        screenshot = wda.screenshot()
        if screenshot:
            solve_image(screenshot, platform="mailcom", device_id=device_id)
            time.sleep(5)

        auto.dismiss_popups(max_attempts=3)
        logger.info("Mail.com signup completed for %s", email)
        return True

    except Exception:
        logger.error("Mail.com signup failed for %s", email, exc_info=True)
        return False


def _click_button(wda: WDASession, labels: list[str]) -> bool:
    """Try to click a button with one of the given labels."""
    for label in labels:
        el = wda.find_element("accessibility id", label)
        if el:
            wda.element_click(el["ELEMENT"])
            return True
        # Try link text too
        el = wda.find_element(
            "predicate string",
            f'name == "{label}" OR label == "{label}"'
        )
        if el:
            wda.element_click(el["ELEMENT"])
            return True
    return False


def _set_dob_fields(wda: WDASession, month: int, day: int, year: int) -> None:
    """Try to set DOB via picker wheels or text fields."""
    # Try picker wheels first
    pickers = wda.find_elements("class chain", "**/XCUIElementTypePickerWheel")
    if len(pickers) >= 3:
        month_names = [
            "", "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ]
        wheel_ids = [p.get("ELEMENT", "") for p in pickers]
        values = [month_names[month], str(day), str(year)]
        for wid, val in zip(wheel_ids, values):
            if wid:
                try:
                    wda.client.post(
                        f"/session/{wda.session_id}/element/{wid}/value",
                        json={"value": [val]},
                    )
                    time.sleep(0.3)
                except Exception:
                    pass
        return

    # Try select dropdowns or text fields for month/day/year
    for label_part, value in [("month", str(month)), ("day", str(day)), ("year", str(year))]:
        field = wda.find_element(
            "predicate string",
            f'name CONTAINS[c] "{label_part}"'
        )
        if field:
            wda.element_click(field["ELEMENT"])
            time.sleep(0.3)
            wda.element_value(field["ELEMENT"], value)
            time.sleep(0.3)
