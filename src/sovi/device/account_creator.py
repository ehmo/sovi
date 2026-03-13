"""Account creation automation — full signup flow on device.

Creates new accounts when the scheduler has no warming tasks remaining.
Flow: install → signup → CAPTCHA → email verify → SMS → profile → TOTP → DB.

TikTok signup uses coordinate-based tapping with screenshot verification,
because TikTok's custom views don't expose accessibility elements reliably.
Coordinates are for iPhone 16 (393x852 points, 1179x2556 pixels, 3x scale).
"""

from __future__ import annotations

import io
import logging
import os
import random
import string
import time
from typing import Any
from uuid import UUID

from PIL import Image

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

# Debug screenshot directory (disabled in production — set env SOVI_SIGNUP_DEBUG=1)
_SIGNUP_DEBUG = os.environ.get("SOVI_SIGNUP_DEBUG", "0") == "1"
_SIGNUP_SS_DIR = "/tmp/sovi_signup"


## -- Screenshot analysis for TikTok coordinate-based signup -- ##


def _ss_save(png: bytes, step: int, name: str) -> str | None:
    """Save a debug screenshot if SOVI_SIGNUP_DEBUG is enabled. Returns path."""
    if not _SIGNUP_DEBUG or not png:
        return None
    os.makedirs(_SIGNUP_SS_DIR, exist_ok=True)
    path = os.path.join(_SIGNUP_SS_DIR, f"{step:02d}_{name}.png")
    with open(path, "wb") as f:
        f.write(png)
    logger.debug("Screenshot saved: %s (%d bytes)", path, len(png))
    return path


def _find_wide_red_band(png: bytes, y_min_frac: float = 0.0, y_max_frac: float = 1.0) -> int | None:
    """Find a wide red/pink horizontal band in a screenshot (TikTok buttons).

    Returns the y-coordinate in WDA points (pixels / 3) of the band center,
    or None if no red band found.
    """
    if not png:
        return None
    try:
        img = Image.open(io.BytesIO(png))
        px = img.load()
        w, h = img.size
        y_min = int(h * y_min_frac)
        y_max = int(h * y_max_frac)
        for y in range(y_min, y_max, 3):
            red_ct = 0
            for x in range(0, w, 5):
                r, g, b = px[x, y][:3]
                if r > 200 and g < 100 and b < 100:
                    red_ct += 1
            if red_ct > 30:
                return y // 3  # Convert pixels to WDA points
    except Exception:
        logger.debug("Error analyzing screenshot for red band", exc_info=True)
    return None


def _is_birthday_screen(png: bytes) -> bool:
    """Check if screenshot shows TikTok birthday picker.

    Looks for: pink Continue button at bottom + dark pixels at top-left (back arrow).
    """
    if not png:
        return False
    try:
        img = Image.open(io.BytesIO(png))
        px = img.load()
        w, h = img.size
        # Check for pink/red button in bottom 20%
        btn_y = _find_wide_red_band(png, 0.8, 1.0)
        if not btn_y:
            return False
        # Check for back arrow (dark pixels in top-left)
        for y in range(80, 150):
            for x in range(30, 120):
                r, g, b = px[x, y][:3]
                if r < 50 and g < 50 and b < 50:
                    return True
    except Exception:
        logger.debug("Error checking birthday screen", exc_info=True)
    return False


def _is_email_phone_screen(png: bytes) -> bool:
    """Check if screenshot shows the email/phone entry screen.

    Looks for a text input area (horizontal line) in the upper portion.
    """
    if not png:
        return False
    try:
        img = Image.open(io.BytesIO(png))
        px = img.load()
        w, h = img.size
        # The email/phone screen has tab selectors at top and an input field
        # Check for a horizontal gray line (input field underline) in y range 20-40%
        for y in range(int(h * 0.2), int(h * 0.4), 2):
            gray_ct = 0
            for x in range(int(w * 0.1), int(w * 0.9), 3):
                r, g, b = px[x, y][:3]
                if abs(r - g) < 15 and abs(g - b) < 15 and 150 < r < 220:
                    gray_ct += 1
            if gray_ct > 40:
                return True
    except Exception:
        logger.debug("Error checking email/phone screen", exc_info=True)
    return False


def _dismiss_tiktok_alerts(wda: WDASession) -> None:
    """Dismiss TikTok-specific system alerts (Google SSO, tracking, etc)."""
    for _ in range(3):
        text = wda.get_alert_text()
        if not text or not isinstance(text, str):
            break
        lower = text.lower()
        logger.info("TikTok alert: %s", text[:60])
        if any(kw in lower for kw in ["google", "sign in", "track", "would like"]):
            wda.dismiss_alert()
        else:
            wda.accept_alert()
        time.sleep(1)


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

    # Create device-account binding for identity isolation
    if device_id:
        try:
            sync_execute(
                "SELECT bind_account_to_device(%s, %s, 'initial')",
                (str(account["id"]), device_id),
            )
        except Exception:
            logger.warning("Failed to create device binding for account %s", account["id"],
                          exc_info=True)

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
    """TikTok signup flow — coordinate-based with screenshot verification.

    TikTok's custom views don't expose accessibility elements reliably,
    so we use hardcoded coordinates (iPhone 16, 393x852 points) and
    verify each screen transition via screenshot pixel analysis.

    Coordinate reference (all in WDA points = screenshot pixels / 3):
        Login screen:
            "Sign up" link: (280, 799)
        Signup method screen:
            "Use phone or email" button: ~(196, <detected_y>) — red band
        Birthday screen:
            Month picker: (137, 654), Day: (280, 654), Year: (357, 654)
            "Continue" button: (197, 770)
        Email/Phone screen:
            "Email" tab: (290, 130)
            Email input field: (196, 220)
            "Next" button: ~(196, 475) or bottom of screen
    """
    step_n = 0

    def _ss(name: str) -> bytes:
        """Take screenshot, optionally save debug copy, return PNG bytes."""
        nonlocal step_n
        step_n += 1
        png = wda.screenshot()
        _ss_save(png, step_n, name)
        return png

    try:
        # -- Step 1: Fresh launch --
        logger.info("TikTok signup step 1: Launch app")
        wda.terminate_app(BUNDLES["tiktok"])
        time.sleep(3)
        wda.launch_app(BUNDLES["tiktok"])
        time.sleep(random.uniform(7, 10))  # TikTok boot is slow
        _dismiss_tiktok_alerts(wda)
        _ss("launch")

        # -- Step 2: Tap "Sign up" and verify we reached signup page --
        logger.info("TikTok signup step 2: Navigate to signup page")
        signup_red_y = None
        for attempt in range(3):
            wda.tap(280, 799)  # "Sign up" link at bottom
            time.sleep(random.uniform(12, 16))  # TikTok transitions take 10-15s
            _dismiss_tiktok_alerts(wda)
            png = _ss(f"after_signup_tap_{attempt + 1}")

            signup_red_y = _find_wide_red_band(png, 0.15, 0.45)
            if signup_red_y:
                logger.info("Signup page verified (red button at y=%d)", signup_red_y)
                break
            logger.info("Not on signup page yet (attempt %d/3)", attempt + 1)
        else:
            logger.error("Failed to reach signup page after 3 attempts")
            events.emit("account", "error", "signup_nav_failed",
                        "Could not navigate to TikTok signup page",
                        device_id=device_id, context={"platform": "tiktok", "step": "signup_page"})
            return False

        # -- Step 3: Tap "Use phone or email" (red button) --
        logger.info("TikTok signup step 3: Tap 'Use phone or email'")
        tap_y = signup_red_y or 243
        wda.tap(196, tap_y)
        time.sleep(random.uniform(8, 12))
        _dismiss_tiktok_alerts(wda)

        # Verify birthday page
        for attempt in range(3):
            png = _ss(f"birthday_check_{attempt + 1}")
            if _is_birthday_screen(png):
                logger.info("Birthday page verified")
                break
            if attempt < 2:
                logger.info("Waiting for birthday page (attempt %d/3)", attempt + 1)
                time.sleep(5)
        else:
            logger.warning("Could not verify birthday page, continuing anyway")

        # -- Step 4: Set birthday (year → ~1995-2002) --
        logger.info("TikTok signup step 4: Set birthday year")
        target_year = random.randint(1995, 2002)
        # Year picker: x=357 points, center at y=654 points
        # Default year is current (2026). Each swipe moves ~3-4 years.
        years_back = 2026 - target_year  # ~24-31 years back
        swipes_needed = max(6, years_back // 4)

        year_x = 357
        picker_y = 654
        for i in range(swipes_needed):
            # Swipe down on year picker (from above center to below = earlier years)
            wda.swipe(year_x, picker_y - 50, year_x, picker_y + 100, duration=0.5)
            time.sleep(random.uniform(2.5, 3.5))

        # Also randomize month and day by swiping their pickers slightly
        month_swipes = random.randint(0, 5)
        for _ in range(month_swipes):
            direction = random.choice([-1, 1])
            wda.swipe(137, picker_y - 30 * direction, 137, picker_y + 30 * direction, duration=0.3)
            time.sleep(1)

        day_swipes = random.randint(0, 3)
        for _ in range(day_swipes):
            direction = random.choice([-1, 1])
            wda.swipe(280, picker_y - 30 * direction, 280, picker_y + 30 * direction, duration=0.3)
            time.sleep(1)

        time.sleep(3)
        _ss("after_birthday_set")

        # -- Step 5: Tap Continue --
        logger.info("TikTok signup step 5: Tap Continue")
        wda.tap(197, 770)
        time.sleep(random.uniform(8, 12))
        _dismiss_tiktok_alerts(wda)
        _ss("after_continue")

        # -- Step 6: Email entry --
        logger.info("TikTok signup step 6: Enter email")
        time.sleep(3)

        # Tap "Email" tab (right side of Phone/Email tabs)
        wda.tap(290, 130)
        time.sleep(3)

        # Tap email input field
        wda.tap(196, 220)
        time.sleep(2)

        # Type email — try element-based first, fall back to coordinate tap
        email_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField"'
        )
        if email_field:
            wda.element_value(email_field["ELEMENT"], email)
            logger.info("Email entered via element: %s", email)
        else:
            # Fallback: try class chain
            email_field = wda.find_element("class chain", "**/XCUIElementTypeTextField")
            if email_field:
                wda.element_value(email_field["ELEMENT"], email)
                logger.info("Email entered via class chain: %s", email)
            else:
                logger.warning("No text field found for email entry")
                events.emit("account", "warning", "signup_no_email_field",
                            "Could not find email text field",
                            device_id=device_id, context={"platform": "tiktok", "step": "email"})

        time.sleep(2)
        _ss("email_entered")

        # Tap "Next" — try element first, then coordinates
        next_tapped = False
        for label in ["Next", "Continue"]:
            el = wda.find_element("accessibility id", label)
            if el:
                wda.element_click(el["ELEMENT"])
                next_tapped = True
                break
        if not next_tapped:
            # Coordinate fallback: Next button near bottom of form area
            wda.tap(196, 475)
        time.sleep(random.uniform(5, 8))
        _dismiss_tiktok_alerts(wda)
        _ss("after_email_next")

        # -- Step 7: CAPTCHA handling --
        logger.info("TikTok signup step 7: CAPTCHA check")
        png = _ss("captcha_check")
        if png:
            captcha_result = solve_slide(png, platform="tiktok", device_id=device_id)
            if captcha_result:
                logger.info("CAPTCHA solved: %s", str(captcha_result)[:80])
                # Apply the slide solution — this depends on the CAPTCHA type
                # CapSolver returns coordinates for where to slide
                slide_x = captcha_result.get("slideX") or captcha_result.get("distance")
                if slide_x:
                    size = wda.screen_size()
                    # Slide from left side of CAPTCHA track to target position
                    wda.swipe(
                        int(size["width"] * 0.15), int(size["height"] * 0.5),
                        int(size["width"] * 0.15) + int(slide_x),
                        int(size["height"] * 0.5),
                        duration=random.uniform(0.5, 1.0),
                    )
                time.sleep(5)
                _dismiss_tiktok_alerts(wda)
        time.sleep(3)

        # -- Step 8: Email verification code --
        logger.info("TikTok signup step 8: Email verification")
        _ss("verification_screen")

        if imap_config:
            code = poll_for_code(imap_config, "tiktok", target_email=email, timeout=120)
            if code:
                logger.info("Email verification code received: %s", code)
                # Find the code input field
                code_field = wda.find_element(
                    "predicate string",
                    'type == "XCUIElementTypeTextField"'
                )
                if not code_field:
                    code_field = wda.find_element("class chain", "**/XCUIElementTypeTextField")
                if code_field:
                    wda.element_value(code_field["ELEMENT"], code)
                    time.sleep(3)
                else:
                    # Tap the code input area and try again
                    wda.tap(196, 280)
                    time.sleep(1)
                    code_field = wda.find_element(
                        "predicate string",
                        'type == "XCUIElementTypeTextField"'
                    )
                    if code_field:
                        wda.element_value(code_field["ELEMENT"], code)
                        time.sleep(3)

                # Tap Next/Verify
                for label in ["Next", "Verify", "Continue"]:
                    el = wda.find_element("accessibility id", label)
                    if el:
                        wda.element_click(el["ELEMENT"])
                        break
                time.sleep(random.uniform(5, 8))
            else:
                logger.warning("No email verification code received")
                events.emit("account", "warning", "signup_no_email_code",
                            "Email verification code not received",
                            device_id=device_id,
                            context={"platform": "tiktok", "email": email, "step": "email_verify"})
        else:
            logger.warning("No IMAP config — cannot verify email")

        _dismiss_tiktok_alerts(wda)
        _ss("after_email_verify")

        # -- Step 9: Password entry --
        logger.info("TikTok signup step 9: Create password")
        pw_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeSecureTextField"'
        )
        if pw_field:
            wda.element_click(pw_field["ELEMENT"])
            time.sleep(0.5)
            wda.element_value(pw_field["ELEMENT"], password)
            time.sleep(2)
        else:
            # Tap password field area and try again
            wda.tap(196, 280)
            time.sleep(1)
            pw_field = wda.find_element(
                "predicate string",
                'type == "XCUIElementTypeSecureTextField"'
            )
            if pw_field:
                wda.element_click(pw_field["ELEMENT"])
                time.sleep(0.5)
                wda.element_value(pw_field["ELEMENT"], password)
                time.sleep(2)

        _ss("password_entered")

        # Tap Next/Sign up
        for label in ["Next", "Sign up", "Sign Up"]:
            el = wda.find_element("accessibility id", label)
            if el:
                wda.element_click(el["ELEMENT"])
                break
        else:
            wda.tap(196, 475)
        time.sleep(random.uniform(5, 8))
        _dismiss_tiktok_alerts(wda)
        _ss("after_password_next")

        # -- Step 10: SMS verification (if required) --
        logger.info("TikTok signup step 10: SMS check")
        sms_el = wda.find_element(
            "predicate string",
            'name CONTAINS "phone" OR name CONTAINS "Phone"'
        )
        if sms_el:
            logger.info("SMS verification required")
            sms_verification = request_number("tiktok")
            if sms_verification:
                wda.element_value(sms_el["ELEMENT"], sms_verification.phone_number)
                time.sleep(2)
                for label in ["Send code", "Send Code", "Next"]:
                    el = wda.find_element("accessibility id", label)
                    if el:
                        wda.element_click(el["ELEMENT"])
                        break
                time.sleep(3)

                sms_code = wait_for_code(sms_verification, timeout=120)
                if sms_code:
                    code_field = wda.find_element(
                        "predicate string",
                        'type == "XCUIElementTypeTextField"'
                    )
                    if code_field:
                        wda.element_value(code_field["ELEMENT"], sms_code)
                        time.sleep(3)
                    # Submit
                    for label in ["Next", "Verify", "Submit"]:
                        el = wda.find_element("accessibility id", label)
                        if el:
                            wda.element_click(el["ELEMENT"])
                            break
                    time.sleep(5)
                else:
                    logger.warning("SMS code not received, cancelling")
                    cancel_verification(sms_verification)
            else:
                logger.warning("Could not get SMS number")

        _dismiss_tiktok_alerts(wda)
        _ss("after_sms")

        # -- Step 11: Username / interests / onboarding --
        logger.info("TikTok signup step 11: Post-signup screens")
        time.sleep(3)

        # Try to set username if prompted
        username_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField" AND (name CONTAINS "username" OR name CONTAINS "Username")'
        )
        if username_field:
            wda.element_click(username_field["ELEMENT"])
            time.sleep(0.5)
            wda.element_value(username_field["ELEMENT"], username)
            time.sleep(1)

        # Skip through onboarding screens
        for _ in range(5):
            dismissed = False
            for label in ["Skip", "Not now", "Not Now", "Maybe later", "Maybe Later",
                          "Got it", "Dismiss", "Close", "No thanks"]:
                el = wda.find_element("accessibility id", label)
                if el:
                    wda.element_click(el["ELEMENT"])
                    logger.info("Dismissed onboarding: %s", label)
                    time.sleep(2)
                    dismissed = True
                    break
            if not dismissed:
                break

        auto.dismiss_popups(max_attempts=3)
        _ss("signup_complete")

        logger.info("TikTok signup flow completed for %s", email)
        events.emit("account", "info", "signup_completed",
                    f"TikTok signup completed for {email}",
                    device_id=device_id,
                    context={"platform": "tiktok", "email": email, "username": username})
        return True

    except Exception:
        logger.error("TikTok signup failed for %s", email, exc_info=True)
        events.emit("account", "error", "signup_exception",
                    f"TikTok signup exception for {email}",
                    device_id=device_id,
                    context={"platform": "tiktok", "email": email})
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
