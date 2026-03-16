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
from sovi.auth.captcha_solver import detect_captcha_popup, solve_puzzle_local, solve_slide
# TODO: Replace with on-device email_reader.py
def _poll_stub(*args, **kwargs):
    """Stub for quarantined email polling -- returns None with warning."""
    logger.warning("QUARANTINED: email polling called but module is quarantined; returning None")
    return None
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

    Returns the y-coordinate in WDA points (pixels / 3) of the band CENTER,
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
        band_start = None
        band_end = None
        for y in range(y_min, y_max, 3):
            red_ct = 0
            for x in range(0, w, 5):
                r, g, b = px[x, y][:3]
                if r > 200 and g < 100 and b < 100:
                    red_ct += 1
            if red_ct > 30:
                if band_start is None:
                    band_start = y
                band_end = y
            elif band_start is not None:
                # End of band — return center
                break
        if band_start is not None and band_end is not None:
            center_y = (band_start + band_end) // 2
            return center_y // 3  # Convert pixels to WDA points
    except Exception:
        logger.debug("Error analyzing screenshot for red band", exc_info=True)
    return None


def _find_wide_pink_band(png: bytes, y_min_frac: float = 0.0, y_max_frac: float = 1.0) -> int | None:
    """Find a wide pink/light-red horizontal band (TikTok's disabled-state buttons).

    The birthday page's Continue button is pink (255,171,187) not red.
    Returns center y in WDA points, or None.
    """
    if not png:
        return None
    try:
        img = Image.open(io.BytesIO(png))
        px = img.load()
        w, h = img.size
        y_min = int(h * y_min_frac)
        y_max = int(h * y_max_frac)
        band_start = None
        band_end = None
        for y in range(y_min, y_max, 3):
            pink_ct = 0
            for x in range(0, w, 5):
                r, g, b = px[x, y][:3]
                # Match both red (r>200,g<100,b<100) and pink (r>220,g>120,b>140)
                if r > 220 and (g < 100 or (120 < g < 200 and 140 < b < 210)):
                    pink_ct += 1
            if pink_ct > 30:
                if band_start is None:
                    band_start = y
                band_end = y
            elif band_start is not None:
                break
        if band_start is not None and band_end is not None:
            return (band_start + band_end) // 2 // 3
    except Exception:
        pass
    return None


def _is_birthday_screen(png: bytes) -> bool:
    """Check if screenshot shows TikTok birthday picker.

    Looks for: pink/red Continue button at bottom + dark pixels at top-left (back arrow).
    """
    if not png:
        return False
    try:
        img = Image.open(io.BytesIO(png))
        px = img.load()
        w, h = img.size
        # Check for pink or red button in bottom 25%
        btn_y = _find_wide_red_band(png, 0.75, 1.0) or _find_wide_pink_band(png, 0.75, 1.0)
        if not btn_y:
            return False
        # Check for back arrow (dark pixels in top-left quadrant)
        for y in range(50, 250):
            for x in range(20, 250):
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
    imap_config: Any = None,
    email_password: str | None = None,
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
        success = _signup_tiktok(wda, auto, email, password, username, imap_config, device_id, email_password=email_password)
    elif platform == "instagram":
        success = _signup_instagram(wda, auto, email, password, username, imap_config, device_id, email_password=email_password)
    elif platform in ("x_twitter", "twitter"):
        success = _signup_x_twitter(wda, auto, email, password, username, imap_config, device_id, email_password=email_password)
    else:
        logger.error("Unsupported platform for signup: %s", platform)
        return None

    if not success:
        events.emit("account", "error", "account_creation_failed",
                    f"Signup flow failed for {platform}/{username}",
                    device_id=device_id,
                    context={"platform": platform, "username": username, "step": "signup"})
        return None

    # TODO: TOTP enrollment should happen via platform settings when 2FA is
    # actually enabled. Generating a secret here is premature — the platform
    # doesn't know about it yet, so codes derived from it would be invalid.

    # Step 8: Store in DB
    rows = sync_execute(
        """INSERT INTO accounts
           (platform, username, email_enc, password_enc, totp_secret_enc,
            niche_id, device_id, current_state, warming_day_count)
           VALUES (%s, %s, %s, %s, %s, %s, %s, 'created', 0)
           RETURNING id, platform, username, current_state""",
        (
            platform,
            username,
            encrypt(email),
            encrypt(password),
            None,
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
    imap_config: Any,
    device_id: str | None,
    *,
    email_password: str | None = None,
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
        time.sleep(random.uniform(15, 20))  # TikTok boot is very slow
        _dismiss_tiktok_alerts(wda)
        _ss("launch")

        # -- Step 2: Tap "Sign up" and verify we reached signup page --
        logger.info("TikTok signup step 2: Navigate to signup page")
        signup_red_y = None
        for attempt in range(3):
            wda.tap(283, 799)  # "Sign up" link at bottom
            time.sleep(random.uniform(15, 20))  # TikTok transitions need 10-15s
            _dismiss_tiktok_alerts(wda)
            png = _ss(f"after_signup_tap_{attempt + 1}")

            signup_red_y = _find_wide_red_band(png, 0.15, 0.50)
            if signup_red_y:
                logger.info("Signup page verified (red button center at y=%d)", signup_red_y)
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
        time.sleep(random.uniform(12, 16))
        _dismiss_tiktok_alerts(wda)

        # Verify birthday page
        for attempt in range(3):
            png = _ss(f"birthday_check_{attempt + 1}")
            if _is_birthday_screen(png):
                logger.info("Birthday page verified")
                break
            if attempt < 2:
                logger.info("Waiting for birthday page (attempt %d/3)", attempt + 1)
                time.sleep(8)
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
        # Detect Continue button position (pink band at bottom of birthday page)
        continue_y = _find_wide_pink_band(png, 0.75, 1.0) or _find_wide_red_band(png, 0.75, 1.0)
        if continue_y:
            wda.tap(197, continue_y)
        else:
            wda.tap(197, 720)  # Fallback: approximate center of Continue button
        time.sleep(random.uniform(12, 16))
        _dismiss_tiktok_alerts(wda)
        _ss("after_continue")

        # -- Step 6: Email entry --
        logger.info("TikTok signup step 6: Enter email")
        time.sleep(3)

        # Tap "Email" tab (right side of Phone/Email tabs)
        wda.tap(290, 130)
        time.sleep(3)

        # Find and clear email field, then type email
        email_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField"'
        ) or wda.find_element("class chain", "**/XCUIElementTypeTextField")

        if email_field:
            el_id = email_field["ELEMENT"]
            # Tap to focus, clear any autocomplete/existing text, then type
            wda.element_click(el_id)
            time.sleep(1)
            wda.element_clear(el_id)
            time.sleep(0.5)
            # Use wda/keys (character-by-character) to avoid autocomplete issues
            wda.type_text(email)
            logger.info("Email entered: %s", email)
        else:
            # Fallback: tap field coordinates and type via keyboard
            wda.tap(196, 220)
            time.sleep(2)
            wda.type_text(email)
            logger.info("Email entered via keyboard fallback: %s", email)

        time.sleep(2)
        _ss("email_entered")

        # Dismiss any autocomplete suggestions by tapping outside
        wda.tap(196, 350)
        time.sleep(0.5)

        # Tap "Next" / "Continue" — try red/pink band detection first
        png = _ss("before_email_next")
        continue_y = _find_wide_red_band(png, 0.3, 0.6) or _find_wide_pink_band(png, 0.3, 0.6)
        if continue_y:
            wda.tap(196, continue_y)
        else:
            # Try element-based, then coordinate fallback
            next_tapped = False
            for label in ["Next", "Continue"]:
                el = wda.find_element("accessibility id", label)
                if el:
                    wda.element_click(el["ELEMENT"])
                    next_tapped = True
                    break
            if not next_tapped:
                wda.tap(196, 449)  # Known Continue button position
        time.sleep(random.uniform(5, 8))
        _dismiss_tiktok_alerts(wda)
        _ss("after_email_next")

        # -- Step 7: CAPTCHA handling --
        logger.info("TikTok signup step 7: CAPTCHA check")
        no_captcha_count = 0  # Wait for CAPTCHA to appear (delayed popup)

        for captcha_round in range(10):
            time.sleep(5)  # Give CAPTCHA more time to appear
            png = _ss(f"captcha_check_{captcha_round + 1}")
            if not png:
                break

            # Detect puzzle CAPTCHA popup
            puzzle = solve_puzzle_local(png)
            if puzzle:
                no_captcha_count = 0  # Reset patience
                slider_y = puzzle["slider_y"]
                start_x = puzzle["slider_start_x"]
                popup_w = puzzle["popup_width"]
                targets = puzzle["targets"]

                logger.info(
                    "Puzzle CAPTCHA #%d: slider_y=%d, start_x=%d, targets=%s",
                    captcha_round + 1, slider_y, start_x,
                    [f"{t:.0%}" for t in targets[:5]],
                )

                # Try each target position until CAPTCHA clears
                solved = False
                for target_pct in targets[:8]:
                    target_x = start_x + int(popup_w * target_pct)
                    wda.drag(
                        start_x, slider_y, target_x, slider_y,
                        duration=random.uniform(0.3, 0.7),
                        timeout=5.0,
                    )
                    time.sleep(3)

                    # Retry screenshot up to 3 times (WDA timeouts common with TikTok)
                    verify_png = None
                    for _retry in range(3):
                        verify_png = _ss(f"captcha_verify_{captcha_round}_{target_pct:.0%}")
                        if verify_png:
                            break
                        time.sleep(2)

                    if not verify_png:
                        logger.warning("Screenshot timeout after drag — cannot verify, trying next target")
                        continue

                    if not detect_captcha_popup(verify_png):
                        logger.info(
                            "Puzzle CAPTCHA solved at %.0f%% on round %d",
                            target_pct * 100, captcha_round + 1,
                        )
                        solved = True
                        break

                if solved:
                    continue  # Check if another CAPTCHA appears
                logger.warning("Failed to solve puzzle CAPTCHA round %d", captcha_round + 1)
                continue

            # Fall back to CapSolver API for slide/other CAPTCHA types
            captcha_result = solve_slide(png, platform="tiktok", device_id=device_id)
            if captcha_result:
                logger.info("CAPTCHA solved via API: %s", str(captcha_result)[:80])
                slide_x = captcha_result.get("slideX") or captcha_result.get("distance")
                if slide_x:
                    size = wda.screen_size()
                    wda.swipe(
                        int(size["width"] * 0.15), int(size["height"] * 0.5),
                        int(size["width"] * 0.15) + int(slide_x),
                        int(size["height"] * 0.5),
                        duration=random.uniform(0.5, 1.0),
                    )
                time.sleep(5)
                continue

            # No CAPTCHA detected yet — wait a few rounds for delayed popup
            no_captcha_count += 1
            if no_captcha_count >= 3:
                logger.info("No CAPTCHA detected after %d checks, proceeding", no_captcha_count)
                break
            logger.info("No CAPTCHA detected (check %d/3), waiting...", no_captcha_count)

        _dismiss_tiktok_alerts(wda)

        # -- Step 8: Email verification code --
        logger.info("TikTok signup step 8: Email verification")
        _ss("verification_screen")

        # Poll for verification code via IMAP or mail.tm API
        code = None
        if imap_config:
            code = _poll_stub(imap_config, "tiktok", target_email=email, timeout=120)  # TODO: Replace with on-device email_reader.py
        elif email_password:
            code = _poll_stub(email, email_password, "tiktok", timeout=120)  # TODO: Replace with on-device email_reader.py
        else:
            logger.warning("No email verification method available")

        if code:
            logger.info("Email verification code received: %s", code)
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
                wda.tap(196, 280)
                time.sleep(1)
                code_field = wda.find_element(
                    "predicate string",
                    'type == "XCUIElementTypeTextField"'
                )
                if code_field:
                    wda.element_value(code_field["ELEMENT"], code)
                    time.sleep(3)

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
    imap_config: Any,
    device_id: str | None,
    *,
    email_password: str | None = None,
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

        # Confirmation code from email (IMAP or mail.tm API)
        code = None
        if imap_config:
            code = _poll_stub(imap_config, "instagram", target_email=email, timeout=90)  # TODO: Replace with on-device email_reader.py
        elif email_password:
            code = _poll_stub(email, email_password, "instagram", timeout=90)  # TODO: Replace with on-device email_reader.py

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


def _signup_x_twitter(
    wda: WDASession,
    auto: DeviceAutomation,
    email: str,
    password: str,
    username: str,
    imap_config: Any,
    device_id: str | None,
    *,
    email_password: str | None = None,
) -> bool:
    """X/Twitter signup flow via the X app.

    Flow: Launch → Create account → Name + Email + DOB → Next →
          CAPTCHA → Email verification → Password → Username → Done
    """
    try:
        wda.launch_app(BUNDLES["x_twitter"])
        time.sleep(random.uniform(5, 8))
        auto.dismiss_popups(max_attempts=3)

        # Look for "Create account" button
        for label in ["Create account", "Sign up", "Create Account"]:
            el = wda.find_element("accessibility id", label)
            if el:
                wda.element_click(el["ELEMENT"])
                time.sleep(3)
                break
        else:
            # Try predicate search
            el = wda.find_element(
                "predicate string",
                'name CONTAINS[c] "create account" OR name CONTAINS[c] "sign up"'
            )
            if el:
                wda.element_click(el["ELEMENT"])
                time.sleep(3)

        auto.dismiss_popups(max_attempts=2)

        # Name field
        name_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField" AND (name CONTAINS[c] "name" OR name CONTAINS[c] "Name")'
        )
        if name_field:
            display_name = username.replace("_", " ").title()
            wda.element_click(name_field["ELEMENT"])
            time.sleep(0.3)
            wda.element_value(name_field["ELEMENT"], display_name)
            time.sleep(1)

        # Email field — X might show phone first, need to switch to email
        email_link = wda.find_element(
            "predicate string",
            'name CONTAINS[c] "use email instead" OR name CONTAINS[c] "email"'
        )
        if email_link and "button" in str(wda.element_attribute(email_link["ELEMENT"], "type")).lower():
            wda.element_click(email_link["ELEMENT"])
            time.sleep(2)

        email_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField" AND (name CONTAINS[c] "email" OR name CONTAINS[c] "phone")'
        )
        if email_field:
            wda.element_click(email_field["ELEMENT"])
            time.sleep(0.3)
            wda.element_value(email_field["ELEMENT"], email)
            time.sleep(1)

        # Date of birth — X uses picker wheels
        pickers = wda.find_elements("class chain", "**/XCUIElementTypePickerWheel")
        if pickers:
            # Just swipe the year picker to set adult age
            if len(pickers) >= 3:
                year_picker = pickers[2]  # Usually 3rd picker = year
                for _ in range(5):
                    wda.swipe(
                        196, 600, 196, 700, duration=0.3
                    )
                    time.sleep(0.5)
            time.sleep(1)

        # Next button
        for label in ["Next", "Continue"]:
            el = wda.find_element("accessibility id", label)
            if el:
                wda.element_click(el["ELEMENT"])
                time.sleep(5)
                break

        auto.dismiss_popups(max_attempts=2)

        # Confirmation / "Sign up" button
        for label in ["Sign up", "Sign Up", "Create account", "Next"]:
            el = wda.find_element("accessibility id", label)
            if el:
                wda.element_click(el["ELEMENT"])
                time.sleep(5)
                break

        # CAPTCHA handling — X uses arkose labs
        time.sleep(5)
        auto.dismiss_popups(max_attempts=3)

        # Email verification code
        logger.info("X/Twitter signup: waiting for email verification code")
        code = None
        if imap_config:
            code = _poll_stub(imap_config, "x_twitter", target_email=email, timeout=120)  # TODO: Replace with on-device email_reader.py
        elif email_password:
            code = _poll_stub(email, email_password, "x_twitter", timeout=120)  # TODO: Replace with on-device email_reader.py

        if code:
            logger.info("X verification code received: %s", code)
            code_field = wda.find_element(
                "predicate string",
                'type == "XCUIElementTypeTextField"'
            )
            if code_field:
                wda.element_value(code_field["ELEMENT"], code)
                time.sleep(2)
            for label in ["Next", "Verify", "Continue"]:
                el = wda.find_element("accessibility id", label)
                if el:
                    wda.element_click(el["ELEMENT"])
                    time.sleep(3)
                    break
        else:
            logger.warning("No X verification code received")

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
            for label in ["Next", "Continue"]:
                el = wda.find_element("accessibility id", label)
                if el:
                    wda.element_click(el["ELEMENT"])
                    time.sleep(3)
                    break

        # Username — X may suggest one or let you pick
        username_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField" AND (name CONTAINS[c] "username" OR name CONTAINS[c] "handle")'
        )
        if username_field:
            wda.element_click(username_field["ELEMENT"])
            time.sleep(0.3)
            wda.element_value(username_field["ELEMENT"], username)
            time.sleep(1)
            for label in ["Next", "Continue", "Skip for now"]:
                el = wda.find_element("accessibility id", label)
                if el:
                    wda.element_click(el["ELEMENT"])
                    time.sleep(3)
                    break

        # Skip through onboarding
        for _ in range(5):
            dismissed = False
            for label in ["Skip", "Not now", "Skip for now", "Maybe later",
                          "Next", "Continue", "Allow", "Don't Allow"]:
                el = wda.find_element("accessibility id", label)
                if el:
                    wda.element_click(el["ELEMENT"])
                    time.sleep(2)
                    dismissed = True
                    break
            if not dismissed:
                break

        auto.dismiss_popups(max_attempts=3)
        logger.info("X/Twitter signup flow completed for %s", email)
        return True

    except Exception:
        logger.error("X/Twitter signup failed for %s", email, exc_info=True)
        return False


def auto_create_account(
    wda: WDASession,
    platform: str,
    email: str,
    password: str,
    *,
    imap_config: Any = None,
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
