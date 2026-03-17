"""On-device ProtonMail email creation via Safari + WDA.

Creates @proton.me accounts for personas by driving Safari on the iPhone.
Solves ProtonCAPTCHA jigsaw puzzles via edge density detection (Sobel gradient).
All network traffic goes through the phone's cellular connection.

Usage:
    Called by the seeder pipeline in scheduler.py, not directly.
"""

from __future__ import annotations

import logging
import math
import random
import time
import uuid
from io import BytesIO
from typing import Any

import numpy as np
from PIL import Image

from sovi import events
from sovi.auth import generate_password
from sovi.crypto import encrypt
from sovi.db import sync_execute_one
from sovi.device.wda_client import WDASession

logger = logging.getLogger(__name__)

# Safari bundle
SAFARI = "com.apple.mobilesafari"

# Photo area constants (WDA points → pixels at 3x retina)
PY1_W, PY2_W = 305, 642
PX1_W, PX2_W = 42, 352
NEXT_WDA = (197, 667)
PY1, PY2 = PY1_W * 3, PY2_W * 3
PX1, PX2 = PX1_W * 3, PX2_W * 3


# ---------------------------------------------------------------------------
# CAPTCHA solver (edge density detection)
# ---------------------------------------------------------------------------


def _find_cutout_edges(photo: np.ndarray, ph: int, pw: int) -> tuple[float, float]:
    """Find cutout center via Sobel gradient → top 3% edges → sliding window density.

    The cutout boundary is a pixel-perfect artificial overlay that produces
    the STRONGEST edges in any photo. This makes edge density the most
    reliable detection method.
    """
    ds = 2
    gray = np.mean(photo[::ds, ::ds, :3], axis=2)
    sh, sw = gray.shape

    # Sobel gradient (manual — avoids scipy dependency)
    gy = np.zeros((sh, sw))
    gx = np.zeros((sh, sw))
    for y in range(2, sh - 2):
        for x in range(2, sw - 2):
            gx[y, x] = (
                -gray[y - 1, x - 1] - 2 * gray[y, x - 1] - gray[y + 1, x - 1]
                + gray[y - 1, x + 1] + 2 * gray[y, x + 1] + gray[y + 1, x + 1]
            )
            gy[y, x] = (
                -gray[y - 1, x - 1] - 2 * gray[y - 1, x] - gray[y - 1, x + 1]
                + gray[y + 1, x - 1] + 2 * gray[y + 1, x] + gray[y + 1, x + 1]
            )

    magnitude = np.sqrt(gx ** 2 + gy ** 2)
    if np.max(magnitude) == 0:
        return pw * 0.55, ph * 0.45

    # Top 3% strongest edges
    thresh = np.percentile(magnitude[magnitude > 0], 97)
    strong = magnitude > thresh

    # Exclude borders (8%) and piece area (top-left 28%×32%)
    m = max(int(sh * 0.08), 5)
    strong[:m, :] = False
    strong[-m:, :] = False
    strong[:, :m] = False
    strong[:, -m:] = False
    strong[: int(sh * 0.32), : int(sw * 0.28)] = False

    ys, xs = np.where(strong)
    if len(ys) < 10:
        return pw * 0.55, ph * 0.45

    # Sliding window edge density (R=35)
    R = 35
    density = np.zeros((sh, sw))
    step = 4
    for y in range(m + R, sh - m - R, step):
        for x in range(m + R, sw - m - R, step):
            if y < int(sh * 0.32) and x < int(sw * 0.28):
                continue
            count = np.sum((ys - y) ** 2 + (xs - x) ** 2 < R * R)
            density[y, x] = count

    peak = np.unravel_index(np.argmax(density), density.shape)
    py, px = peak

    # Refine with centroid of nearby strong edges
    near = ((ys - py) ** 2 + (xs - px) ** 2) < (R * 1.2) ** 2
    if np.sum(near) > 5:
        return np.mean(xs[near]) * ds, np.mean(ys[near]) * ds
    return px * ds, py * ds


def _find_piece(photo_gray: np.ndarray, ph: int, pw: int) -> tuple[float, float]:
    """Find puzzle piece center in top-left region."""
    ds = 3
    region = photo_gray[: ph // 3, : pw // 3]
    small = region[::ds, ::ds]
    sh, sw = small.shape
    edges = np.zeros((sh, sw))
    for y in range(1, sh - 1):
        for x in range(1, sw - 1):
            gx = float(small[y, x + 1]) - float(small[y, x - 1])
            gy = float(small[y + 1, x]) - float(small[y - 1, x])
            edges[y, x] = math.sqrt(gx * gx + gy * gy)
    thresh = np.percentile(edges[edges > 0], 75) if np.any(edges > 0) else 1e9
    ey, ex = np.where(edges > thresh)
    if len(ey) > 20:
        return float(np.median(ex)) * ds, float(np.median(ey)) * ds
    return pw * 0.08, ph * 0.08


def _solve_captcha(wda: WDASession, *, max_attempts: int = 10) -> bool:
    """Solve ProtonCAPTCHA jigsaw puzzle. Returns True on success."""
    import base64

    for attempt in range(1, max_attempts + 1):
        # Check for retry button (previous failed attempt)
        retry = wda.find_element(
            "predicate string", 'name == "Retry"',
        )
        if retry:
            wda.element_click(retry["ELEMENT"])
            time.sleep(4)
            # Wait for new puzzle to load
            for _ in range(16):
                reset = wda.find_element(
                    "predicate string", 'name == "Reset puzzle piece"',
                )
                if reset:
                    break
                time.sleep(0.5)
            time.sleep(1)

        # Take screenshot and extract photo area
        png = wda.screenshot()
        if not png:
            logger.warning("Screenshot failed on attempt %d", attempt)
            time.sleep(2)
            continue

        arr = np.array(Image.open(BytesIO(png)))
        photo = arr[PY1:PY2, PX1:PX2]
        ph, pw = photo.shape[:2]

        # Check for error/blank screen
        center_bright = np.mean(photo[ph // 3 : 2 * ph // 3, pw // 3 : 2 * pw // 3, :3])
        if center_bright > 245:
            logger.debug("Error screen detected, waiting...")
            time.sleep(2)
            continue

        photo_gray = np.mean(photo[:, :, :3], axis=2)

        # Find cutout and piece positions
        cutout_cx, cutout_cy = _find_cutout_edges(photo, ph, pw)
        piece_cx, piece_cy = _find_piece(photo_gray, ph, pw)

        piece_wda = ((piece_cx + PX1) / 3, (piece_cy + PY1) / 3)
        cutout_wda = ((cutout_cx + PX1) / 3, (cutout_cy + PY1) / 3)

        logger.info(
            "CAPTCHA attempt %d: piece(%.0f,%.0f) -> cutout(%.0f,%.0f)",
            attempt, piece_wda[0], piece_wda[1], cutout_wda[0], cutout_wda[1],
        )

        # Pre-tap photo center to dismiss any text selection
        wda.tap(
            int((PX1_W + PX2_W) / 2),
            int((PY1_W + PY2_W) / 2),
            duration=0.05,
        )
        time.sleep(0.2)

        # Human-like drag from piece to cutout
        _drag_human(
            wda,
            piece_wda[0], piece_wda[1],
            cutout_wda[0], cutout_wda[1],
        )
        time.sleep(1.5)

        # Click Next
        wda.tap(NEXT_WDA[0], NEXT_WDA[1], duration=0.05)
        time.sleep(3)

        # Check result
        retry = wda.find_element("predicate string", 'name == "Retry"')
        if retry:
            logger.info("CAPTCHA attempt %d failed", attempt)
            continue

        pcaptcha = wda.find_element("predicate string", 'name == "pcaptcha"')
        if not pcaptcha:
            logger.info("CAPTCHA solved on attempt %d!", attempt)
            return True

        # pcaptcha still showing — might be processing
        time.sleep(2)
        pcaptcha2 = wda.find_element("predicate string", 'name == "pcaptcha"')
        if not pcaptcha2:
            logger.info("CAPTCHA solved (delayed) on attempt %d!", attempt)
            return True

    logger.warning("CAPTCHA not solved after %d attempts", max_attempts)
    return False


def _drag_human(
    wda: WDASession,
    fx: float, fy: float,
    tx: float, ty: float,
) -> None:
    """Human-like drag using W3C Actions (touch pointer, ease-out, gaussian jitter)."""
    steps = 40
    actions = [
        {"type": "pointerMove", "duration": 0, "x": int(fx), "y": int(fy)},
        {"type": "pointerDown", "button": 0},
        {"type": "pause", "duration": random.randint(80, 200)},
    ]
    for i in range(1, steps + 1):
        f = i / steps
        ease = 1 - (1 - f) ** 2  # ease-out
        jx = random.gauss(0, 1.0) if i < steps else 0
        jy = random.gauss(0, 1.0) if i < steps else 0
        actions.append({
            "type": "pointerMove",
            "duration": random.randint(10, 30),
            "x": int(fx + (tx - fx) * ease + jx),
            "y": int(fy + (ty - fy) * ease + jy),
        })
    actions.extend([
        {"type": "pause", "duration": random.randint(80, 200)},
        {"type": "pointerUp", "button": 0},
    ])
    try:
        wda.client.post(
            f"{wda._s}/actions",
            json={"actions": [{
                "type": "pointer",
                "id": "f1",
                "parameters": {"pointerType": "touch"},
                "actions": actions,
            }]},
            timeout=15,
        )
    except Exception:
        logger.warning("Drag action timed out (gesture may still execute)")


# ---------------------------------------------------------------------------
# ProtonMail signup flow
# ---------------------------------------------------------------------------


def _generate_username(persona: dict) -> str:
    """Generate a ProtonMail-friendly username from persona data."""
    base = persona.get("username_base", "user")
    base = base.replace("_", "").replace("-", "")
    suffix = random.randint(100, 9999)
    return f"{base}{suffix}"


def create_protonmail_email(
    wda: WDASession,
    persona: dict,
    *,
    device_id: str | None = None,
) -> dict[str, Any] | None:
    """Create a ProtonMail account for a persona on-device via Safari.

    Flow:
    1. Toggle airplane mode (fresh cellular IP)
    2. Open Safari → ProtonMail signup
    3. Fill username + password
    4. Solve CAPTCHA if presented
    5. Dismiss upsell + recovery kit
    6. Store in DB

    Returns email_account dict or None on failure.
    """
    username = _generate_username(persona)
    password = generate_password()
    email = f"{username}@proton.me"
    persona_id = str(persona.get("id") or persona.get("persona_id", ""))

    logger.info("Creating ProtonMail: %s for %s", email, persona.get("display_name", "?"))

    events.emit("persona", "info", "protonmail_creation_started",
                f"Creating ProtonMail for {persona.get('display_name', '?')}: {email}",
                device_id=device_id,
                context={"persona_id": persona_id, "email": email})

    try:
        # Step 0: Fresh cellular IP
        if not wda.toggle_airplane_mode():
            events.emit("persona", "error", "cellular_enforcement_failed",
                        f"Could not rotate to a cellular-only state for {email}",
                        device_id=device_id,
                        context={"persona_id": persona_id, "email": email})
            return None

        # Step 1: Open ProtonMail signup in Safari
        wda.terminate_app(SAFARI)
        time.sleep(1)
        wda.launch_app(SAFARI)
        time.sleep(2)
        wda.open_url("https://account.proton.me/signup")
        time.sleep(6)

        # Step 2: Enter username
        username_field = wda.find_element(
            "predicate string", 'type == "XCUIElementTypeTextField"',
        )
        if not username_field:
            logger.error("Username field not found")
            return None
        wda.element_click(username_field["ELEMENT"])
        time.sleep(0.5)
        # Clear existing text
        wda.type_text("\u0008" * 30)
        time.sleep(0.3)
        wda.type_text(username)
        time.sleep(2)  # wait for availability check

        # Step 3: Enter password
        pwd_field = wda.find_element(
            "predicate string", 'type == "XCUIElementTypeSecureTextField"',
        )
        if not pwd_field:
            logger.error("Password field not found")
            return None
        wda.element_click(pwd_field["ELEMENT"])
        time.sleep(0.5)
        wda.type_text(password)
        time.sleep(1)

        # Step 4: Scroll to reveal confirm password + enter it
        wda.tap(197, 500, duration=0.05)
        time.sleep(0.5)
        wda.swipe(197, 500, 197, 300, duration=0.3)
        time.sleep(1)

        confirm_field = wda.find_element(
            "predicate string", 'type == "XCUIElementTypeSecureTextField"',
        )
        if confirm_field:
            wda.element_click(confirm_field["ELEMENT"])
            time.sleep(0.5)
            wda.type_text(password)
            time.sleep(1)

        # Step 5: Click submit
        submit = wda.find_element(
            "predicate string", 'name CONTAINS "Start using Proton"',
        )
        if not submit:
            submit = wda.find_element(
                "predicate string", 'name CONTAINS "Create"',
            )
        if submit:
            wda.element_click(submit["ELEMENT"])
            logger.info("Clicked submit")
        else:
            # Try scrolling more
            wda.swipe(197, 600, 197, 300, duration=0.3)
            time.sleep(1)
            submit = wda.find_element(
                "predicate string", 'name CONTAINS "Start using"',
            )
            if submit:
                wda.element_click(submit["ELEMENT"])
            else:
                logger.error("Submit button not found")
                return None

        time.sleep(5)

        # Step 6: Handle CAPTCHA
        pcaptcha = wda.find_element(
            "predicate string", 'name == "pcaptcha"',
        )
        if pcaptcha:
            logger.info("CAPTCHA appeared, solving...")
            events.emit("persona", "info", "protonmail_captcha",
                        "CAPTCHA encountered during ProtonMail signup",
                        device_id=device_id,
                        context={"persona_id": persona_id})
            if not _solve_captcha(wda, max_attempts=10):
                logger.error("CAPTCHA solve failed")
                return None
            logger.info("CAPTCHA solved!")
            time.sleep(3)

        # Step 7: Dismiss upsell ("No, thanks")
        upsell = wda.find_element(
            "predicate string", 'name == "No, thanks"',
        )
        if upsell:
            wda.element_click(upsell["ELEMENT"])
            time.sleep(3)

        # Step 8: Handle recovery kit screens
        for _ in range(5):
            checkbox = wda.find_element(
                "predicate string", 'type == "XCUIElementTypeSwitch"',
            )
            if checkbox:
                wda.element_click(checkbox["ELEMENT"])
                time.sleep(1)

            cont = wda.find_element("predicate string", 'name == "Continue"')
            if cont:
                wda.element_click(cont["ELEMENT"])
                time.sleep(3)
                continue

            skip = wda.find_element("predicate string", 'name == "Skip"')
            if skip:
                wda.element_click(skip["ELEMENT"])
                time.sleep(3)
                continue

            welcome = wda.find_element("predicate string", 'name == "Welcome"')
            if welcome:
                logger.info("Account setup complete!")
                break

            time.sleep(2)

        # Step 9: Store in DB
        email_id = str(uuid.uuid4())
        row = sync_execute_one(
            """INSERT INTO email_accounts
               (id, persona_id, provider, email, password,
                imap_host, imap_port, domain, status, login_url, verification_status)
               VALUES (%s, %s, 'protonmail', %s, %s,
                       'account.proton.me', 0, 'proton.me', 'available',
                       'https://account.proton.me/login', 'unverified')
               RETURNING id, provider, domain, status""",
            (email_id, persona_id, encrypt(email), encrypt(password)),
        )

        if not row:
            logger.error("DB insert failed for %s", email)
            return None

        events.emit("persona", "info", "protonmail_created",
                    f"Created ProtonMail for {persona.get('display_name', '?')}: {email}",
                    device_id=device_id,
                    context={
                        "persona_id": persona_id,
                        "email_account_id": email_id,
                    })

        logger.info("ProtonMail created: %s (id=%s)", email, email_id[:8])
        return {
            "id": email_id,
            "email": email,
            "password": password,
            "provider": "protonmail",
        }

    except Exception:
        logger.error(
            "ProtonMail creation failed for %s",
            persona.get("display_name", "?"),
            exc_info=True,
        )
        events.emit("persona", "error", "protonmail_creation_error",
                    f"ProtonMail creation exception for {persona.get('display_name', '?')}",
                    device_id=device_id,
                    context={"persona_id": persona_id})
        return None
    finally:
        wda.terminate_app(SAFARI)
