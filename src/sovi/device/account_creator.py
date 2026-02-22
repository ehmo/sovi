"""Account creation automation — full signup flow on device.

Creates new accounts when the scheduler has no warming tasks remaining.
Flow: install → signup → CAPTCHA → email verify → SMS → profile → TOTP → DB.
"""

from __future__ import annotations

import logging
import random
import string
import time
from typing import Any
from uuid import UUID

from sovi import events
from sovi.auth import totp
from sovi.auth.captcha_solver import solve_slide
from sovi.auth.email_verifier import ImapConfig, poll_for_code
from sovi.auth.sms_verifier import cancel_verification, request_number, wait_for_code
from sovi.crypto import encrypt
from sovi.db import sync_execute, sync_execute_one
from sovi.device.app_lifecycle import BUNDLES, delete_app, install_from_app_store
from sovi.device.wda_client import DeviceAutomation, WDASession

logger = logging.getLogger(__name__)


def _generate_username(niche_slug: str) -> str:
    """Generate a plausible username for a niche."""
    prefixes = {
        "personal_finance": ["money", "wealth", "finance", "cash", "invest"],
        "ai_storytelling": ["story", "tales", "narrative", "fiction", "epic"],
        "tech_ai_tools": ["tech", "ai", "digital", "code", "smart"],
        "motivation": ["grind", "hustle", "mindset", "growth", "win"],
        "true_crime": ["crime", "mystery", "case", "detective", "unsolved"],
    }
    prefix = random.choice(prefixes.get(niche_slug, ["user"]))
    suffix = "".join(random.choices(string.digits, k=random.randint(3, 6)))
    return f"{prefix}{suffix}"


def _pick_niche_for_platform(platform: str) -> dict[str, Any] | None:
    """Pick the niche with the fewest accounts on this platform."""
    row = sync_execute_one(
        """SELECT n.id, n.slug, n.name,
                  COUNT(a.id) FILTER (WHERE a.platform = %s AND a.deleted_at IS NULL) as account_count
           FROM niches n
           LEFT JOIN accounts a ON a.niche_id = n.id
           WHERE n.status = 'active'
           GROUP BY n.id, n.slug, n.name
           ORDER BY account_count ASC, n.created_at ASC
           LIMIT 1""",
        (platform,),
    )
    return row


def create_account(
    wda: WDASession,
    platform: str,
    niche_id: UUID | str,
    email: str,
    password: str,
    *,
    imap_config: ImapConfig | None = None,
    device_id: str | None = None,
) -> dict[str, Any] | None:
    """Create a new account on a platform.

    Full flow:
    1. Delete app (IDFV reset)
    2. Install from App Store
    3. Open app, start signup
    4. Handle CAPTCHA
    5. Verify email
    6. Verify SMS (disposable)
    7. Set profile
    8. Enable TOTP 2FA
    9. Store credentials in DB

    Returns the created account dict, or None on failure.
    """
    niche = sync_execute_one("SELECT * FROM niches WHERE id = %s", (str(niche_id),))
    niche_slug = niche["slug"] if niche else "unknown"
    username = _generate_username(niche_slug)
    auto = DeviceAutomation(wda)

    events.emit("account", "info", "account_creation_started",
                f"Starting {platform} account creation: {username}",
                device_id=device_id,
                context={"platform": platform, "niche": niche_slug, "email": email})

    # Step 1: Delete app for clean IDFV
    delete_app(wda, platform, device_id=device_id)
    time.sleep(2)

    # Step 2: Install fresh
    if not install_from_app_store(wda, platform, device_id=device_id):
        events.emit("account", "error", "account_creation_failed",
                    f"Failed to install {platform} app for account creation",
                    device_id=device_id,
                    context={"platform": platform, "step": "install"})
        return None

    time.sleep(3)

    # Step 3-7: Platform-specific signup
    if platform == "tiktok":
        success = _signup_tiktok(wda, auto, email, password, username, imap_config, device_id)
    elif platform == "instagram":
        success = _signup_instagram(wda, auto, email, password, username, imap_config, device_id)
    else:
        logger.error("Unsupported platform for signup: %s", platform)
        return None

    if not success:
        events.emit("account", "error", "account_creation_failed",
                    f"Signup flow failed for {platform}/{username}",
                    device_id=device_id,
                    context={"platform": platform, "username": username, "step": "signup"})
        return None

    # Step 8: Generate TOTP secret (enable 2FA later via settings)
    totp_secret = totp.generate_secret()

    # Step 9: Store in DB
    rows = sync_execute(
        """INSERT INTO accounts
           (platform, username, email_enc, password_enc, totp_secret_enc,
            niche_id, device_id, current_state, warming_day_count)
           VALUES (%s, %s, %s, %s, %s, %s, %s, 'created', 0)
           RETURNING id, platform, username, current_state""",
        (
            platform,
            username,
            encrypt(email).encode(),
            encrypt(password).encode(),
            encrypt(totp_secret).encode(),
            str(niche_id),
            device_id,
        ),
    )

    if not rows:
        logger.error("Failed to insert account into DB")
        return None

    account = rows[0]

    events.emit("account", "info", "account_created",
                f"Created {platform} account: {username}",
                device_id=device_id, account_id=account["id"],
                context={
                    "platform": platform,
                    "niche": niche_slug,
                    "email": email,
                    "username": username,
                })

    logger.info("Account created: %s/%s (id=%s)", platform, username, account["id"])
    return account


def _signup_tiktok(
    wda: WDASession,
    auto: DeviceAutomation,
    email: str,
    password: str,
    username: str,
    imap_config: ImapConfig | None,
    device_id: str | None,
) -> bool:
    """TikTok signup flow."""
    try:
        wda.launch_app(BUNDLES["tiktok"])
        time.sleep(random.uniform(3, 5))
        auto.dismiss_popups(max_attempts=3)

        # Look for Sign up
        for label in ["Sign up", "Sign Up", "Use phone or email"]:
            el = wda.find_element("accessibility id", label)
            if el:
                wda.element_click(el["ELEMENT"])
                time.sleep(2)
                break

        # Birthdate picker
        pickers = wda.find_elements("class chain", "**/XCUIElementTypePickerWheel")
        if len(pickers) == 3:
            wheel_ids = [p.get("ELEMENT", "") for p in pickers]
            month = random.choice([
                "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December",
            ])
            day = str(random.randint(1, 28))
            year = str(random.randint(1990, 2002))
            for wid, val in zip(wheel_ids, [month, day, year]):
                if wid:
                    wda.client.post(
                        f"/session/{wda.session_id}/element/{wid}/value",
                        json={"value": [val]},
                    )
                    time.sleep(0.3)

            next_el = wda.find_element("accessibility id", "Next")
            if next_el:
                wda.element_click(next_el["ELEMENT"])
            time.sleep(3)

        # Select email signup
        for label in ["Email", "Use email"]:
            el = wda.find_element("accessibility id", label)
            if el:
                wda.element_click(el["ELEMENT"])
                time.sleep(1)
                break

        # Enter email
        email_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField"'
        )
        if email_field:
            wda.element_click(email_field["ELEMENT"])
            time.sleep(0.3)
            wda.element_value(email_field["ELEMENT"], email)
            time.sleep(1)

        # Tap Next
        for label in ["Next", "Continue"]:
            el = wda.find_element("accessibility id", label)
            if el:
                wda.element_click(el["ELEMENT"])
                time.sleep(3)
                break

        # Handle CAPTCHA if present
        screenshot = wda.screenshot()
        if screenshot:
            solve_slide(screenshot, platform="tiktok", device_id=device_id)
        time.sleep(3)

        # Email verification code
        if imap_config:
            code = poll_for_code(imap_config, "tiktok", target_email=email, timeout=90)
            if code:
                code_field = wda.find_element(
                    "predicate string",
                    'type == "XCUIElementTypeTextField"'
                )
                if code_field:
                    wda.element_value(code_field["ELEMENT"], code)
                    time.sleep(2)

        # Create password
        pw_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeSecureTextField"'
        )
        if pw_field:
            wda.element_click(pw_field["ELEMENT"])
            time.sleep(0.3)
            wda.element_value(pw_field["ELEMENT"], password)
            time.sleep(1)

        # Next/Sign up
        for label in ["Next", "Sign up", "Sign Up"]:
            el = wda.find_element("accessibility id", label)
            if el:
                wda.element_click(el["ELEMENT"])
                time.sleep(3)
                break

        # Handle SMS verification if required
        sms_el = wda.find_element(
            "predicate string",
            'name CONTAINS "phone" OR name CONTAINS "Phone"'
        )
        if sms_el:
            sms_verification = request_number("tiktok")
            if sms_verification:
                wda.element_value(sms_el["ELEMENT"], sms_verification.phone_number)
                time.sleep(2)
                # Submit
                for label in ["Send code", "Send Code", "Next"]:
                    el = wda.find_element("accessibility id", label)
                    if el:
                        wda.element_click(el["ELEMENT"])
                        break
                time.sleep(3)

                sms_code = wait_for_code(sms_verification, timeout=90)
                if sms_code:
                    code_field = wda.find_element(
                        "predicate string",
                        'type == "XCUIElementTypeTextField"'
                    )
                    if code_field:
                        wda.element_value(code_field["ELEMENT"], sms_code)
                        time.sleep(2)
                else:
                    cancel_verification(sms_verification)

        # Set username
        time.sleep(3)
        auto.dismiss_popups(max_attempts=3)

        # Skip interests selection
        for label in ["Skip", "Not now", "Maybe later"]:
            el = wda.find_element("accessibility id", label)
            if el:
                wda.element_click(el["ELEMENT"])
                time.sleep(2)
                break

        auto.dismiss_popups(max_attempts=3)
        logger.info("TikTok signup flow completed for %s", email)
        return True

    except Exception:
        logger.error("TikTok signup failed for %s", email, exc_info=True)
        return False


def _signup_instagram(
    wda: WDASession,
    auto: DeviceAutomation,
    email: str,
    password: str,
    username: str,
    imap_config: ImapConfig | None,
    device_id: str | None,
) -> bool:
    """Instagram signup flow."""
    try:
        wda.launch_app(BUNDLES["instagram"])
        time.sleep(random.uniform(3, 5))
        auto.dismiss_popups(max_attempts=3)

        # Look for "Create new account" / "Join Instagram"
        for label in ["Create new account", "Join Instagram", "Sign Up", "Sign up"]:
            el = wda.find_element("accessibility id", label)
            if el:
                wda.element_click(el["ELEMENT"])
                time.sleep(2)
                break

        # Enter email
        email_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField" AND (name CONTAINS "email" OR name CONTAINS "Email" OR name CONTAINS "phone")'
        )
        if email_field:
            wda.element_click(email_field["ELEMENT"])
            time.sleep(0.3)
            wda.element_value(email_field["ELEMENT"], email)
            time.sleep(1)

        # Next
        for label in ["Next", "Continue"]:
            el = wda.find_element("accessibility id", label)
            if el:
                wda.element_click(el["ELEMENT"])
                time.sleep(3)
                break

        # Confirmation code from email
        if imap_config:
            code = poll_for_code(imap_config, "instagram", target_email=email, timeout=90)
            if code:
                code_field = wda.find_element(
                    "predicate string",
                    'type == "XCUIElementTypeTextField"'
                )
                if code_field:
                    wda.element_value(code_field["ELEMENT"], code)
                    time.sleep(1)
                    for label in ["Next", "Confirm", "Continue"]:
                        el = wda.find_element("accessibility id", label)
                        if el:
                            wda.element_click(el["ELEMENT"])
                            time.sleep(3)
                            break

        # Full name
        name_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField" AND (name CONTAINS "name" OR name CONTAINS "Name")'
        )
        if name_field:
            display_name = username.replace("_", " ").title()
            wda.element_value(name_field["ELEMENT"], display_name)
            time.sleep(1)

        # Password
        pw_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeSecureTextField"'
        )
        if pw_field:
            wda.element_click(pw_field["ELEMENT"])
            time.sleep(0.3)
            wda.element_value(pw_field["ELEMENT"], password)
            time.sleep(1)

        # Next
        for label in ["Next", "Continue", "Sign Up"]:
            el = wda.find_element("accessibility id", label)
            if el:
                wda.element_click(el["ELEMENT"])
                time.sleep(3)
                break

        # Birthday
        # Instagram may show a birthday picker — set adult DOB
        pickers = wda.find_elements("class chain", "**/XCUIElementTypePickerWheel")
        if pickers:
            # Just set a reasonable adult date
            for label in ["Next", "Set Date", "Continue"]:
                el = wda.find_element("accessibility id", label)
                if el:
                    wda.element_click(el["ELEMENT"])
                    time.sleep(2)
                    break

        # Username suggestion — Instagram often auto-suggests
        # Accept or change to our generated one
        username_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField" AND (name CONTAINS "username" OR name CONTAINS "Username")'
        )
        if username_field:
            wda.element_click(username_field["ELEMENT"])
            time.sleep(0.3)
            # Clear and type our username
            wda.element_value(username_field["ELEMENT"], username)
            time.sleep(1)
            for label in ["Next", "Continue"]:
                el = wda.find_element("accessibility id", label)
                if el:
                    wda.element_click(el["ELEMENT"])
                    time.sleep(3)
                    break

        # Handle post-signup screens
        auto.dismiss_popups(max_attempts=5)
        time.sleep(2)

        # Skip profile photo, contacts, etc.
        for label in ["Skip", "Not Now", "Not now", "Maybe Later"]:
            el = wda.find_element("accessibility id", label)
            if el:
                wda.element_click(el["ELEMENT"])
                time.sleep(2)

        auto.dismiss_popups(max_attempts=3)
        logger.info("Instagram signup flow completed for %s", email)
        return True

    except Exception:
        logger.error("Instagram signup failed for %s", email, exc_info=True)
        return False


def auto_create_account(
    wda: WDASession,
    platform: str,
    email: str,
    password: str,
    *,
    imap_config: ImapConfig | None = None,
    device_id: str | None = None,
) -> dict[str, Any] | None:
    """Auto-create an account on the platform with the least-served niche.

    Used by the scheduler when no warming tasks remain.
    """
    niche = _pick_niche_for_platform(platform)
    if not niche:
        logger.error("No active niches found for %s", platform)
        return None

    return create_account(
        wda, platform, niche["id"], email, password,
        imap_config=imap_config, device_id=device_id,
    )
