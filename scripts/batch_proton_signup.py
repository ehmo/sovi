#!/usr/bin/env python3
"""Batch ProtonMail account creation via Safari WDA automation.

Creates @proton.me email accounts for personas, solving the
ProtonCAPTCHA jigsaw puzzle via edge density detection.

Usage (on studio):
    .venv/bin/python scripts/batch_proton_signup.py --limit 1 --dry-run
    .venv/bin/python scripts/batch_proton_signup.py --limit 5
    .venv/bin/python scripts/batch_proton_signup.py --delay 45
"""
import argparse
import base64
import logging
import math
import os
import random
import string
import sys
import time
import uuid
from io import BytesIO

import httpx
import numpy as np
from PIL import Image

sys.path.insert(0, "src")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("proton_batch")

from sovi.crypto import encrypt
from sovi.db import sync_execute


# ─── WDA helpers ───────────────────────────────────────────────────

class WDA:
    """Minimal WDA client for ProtonMail signup."""

    def __init__(self, port=8100):
        self.base = f"http://localhost:{port}"
        self.client = httpx.Client(timeout=45)
        self.sid = None
        self.s = None

    def connect(self):
        resp = self.client.post(f"{self.base}/session",
                                json={"capabilities": {"alwaysMatch": {"platformName": "iOS"}}})
        self.sid = resp.json()["value"]["sessionId"]
        self.s = f"{self.base}/session/{self.sid}"
        log.info("WDA session: %s", self.sid[:8])

    def find(self, pred, timeout=10):
        try:
            r = self.client.post(f"{self.s}/element",
                                 json={"using": "predicate string", "value": pred},
                                 timeout=timeout)
            d = r.json()
            el = d.get("value", {})
            return el.get("ELEMENT") if "ELEMENT" in el else None
        except Exception:
            return None

    def click(self, eid):
        try:
            self.client.post(f"{self.s}/element/{eid}/click", json={}, timeout=15)
        except Exception:
            pass

    def rect(self, eid):
        try:
            r = self.client.get(f"{self.s}/element/{eid}/rect", timeout=10)
            return r.json().get("value", {})
        except Exception:
            return {}

    def tap(self, x, y, dur=100):
        self.client.post(f"{self.s}/actions", json={"actions": [
            {"type": "pointer", "id": "f1", "parameters": {"pointerType": "touch"},
             "actions": [
                 {"type": "pointerMove", "duration": 0, "x": int(x), "y": int(y)},
                 {"type": "pointerDown", "button": 0},
                 {"type": "pause", "duration": dur},
                 {"type": "pointerUp", "button": 0},
             ]}]})

    def type_keys(self, text):
        self.client.post(f"{self.s}/wda/keys", json={"value": list(text)}, timeout=15)

    def screenshot(self):
        r = self.client.get(f"{self.base}/screenshot", timeout=45)
        raw = base64.b64decode(r.json()["value"])
        return np.array(Image.open(BytesIO(raw)))

    def open_url(self, url):
        self.client.post(f"{self.s}/url", json={"url": url}, timeout=15)

    def terminate(self, bundle):
        self.client.post(f"{self.s}/wda/apps/terminate",
                         json={"bundleId": bundle}, timeout=10)

    def activate(self, bundle):
        self.client.post(f"{self.s}/wda/apps/activate",
                         json={"bundleId": bundle}, timeout=10)

    def drag_human(self, fx, fy, tx, ty):
        steps = 40
        acts = [
            {"type": "pointerMove", "duration": 0, "x": int(fx), "y": int(fy)},
            {"type": "pointerDown", "button": 0},
            {"type": "pause", "duration": random.randint(80, 200)},
        ]
        for i in range(1, steps + 1):
            f = i / steps
            ease = 1 - (1 - f) ** 2
            jx = random.gauss(0, 1.0) if i < steps else 0
            jy = random.gauss(0, 1.0) if i < steps else 0
            acts.append({"type": "pointerMove", "duration": random.randint(10, 30),
                          "x": int(fx + (tx-fx)*ease + jx), "y": int(fy + (ty-fy)*ease + jy)})
        acts.extend([{"type": "pause", "duration": random.randint(80, 200)},
                      {"type": "pointerUp", "button": 0}])
        self.client.post(f"{self.s}/actions", json={"actions": [
            {"type": "pointer", "id": "f1", "parameters": {"pointerType": "touch"}, "actions": acts}
        ]}, timeout=15)


# ─── CAPTCHA solver ────────────────────────────────────────────────

# Photo area constants (WDA coords)
PY1_W, PY2_W = 305, 642
PX1_W, PX2_W = 42, 352
NEXT_WDA = (197, 667)
PY1, PY2 = PY1_W * 3, PY2_W * 3
PX1, PX2 = PX1_W * 3, PX2_W * 3


def find_cutout_edges(photo, ph, pw):
    """Find cutout center via edge density detection."""
    ds = 2
    gray = np.mean(photo[::ds, ::ds, :3], axis=2)
    sh, sw = gray.shape

    # Sobel gradient
    gy = np.zeros((sh, sw))
    gx = np.zeros((sh, sw))
    for y in range(2, sh-2):
        for x in range(2, sw-2):
            gx[y,x] = (-gray[y-1,x-1] - 2*gray[y,x-1] - gray[y+1,x-1]
                        + gray[y-1,x+1] + 2*gray[y,x+1] + gray[y+1,x+1])
            gy[y,x] = (-gray[y-1,x-1] - 2*gray[y-1,x] - gray[y-1,x+1]
                        + gray[y+1,x-1] + 2*gray[y+1,x] + gray[y+1,x+1])

    magnitude = np.sqrt(gx**2 + gy**2)
    if np.max(magnitude) == 0:
        return pw * 0.55, ph * 0.45

    # Top 3% strongest edges
    thresh = np.percentile(magnitude[magnitude > 0], 97)
    strong = magnitude > thresh

    # Exclude borders (8%) and piece area (top-left 28%x32%)
    m = max(int(sh * 0.08), 5)
    strong[:m, :] = False
    strong[-m:, :] = False
    strong[:, :m] = False
    strong[:, -m:] = False
    strong[:int(sh * 0.32), :int(sw * 0.28)] = False

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
            count = np.sum((ys - y)**2 + (xs - x)**2 < R*R)
            density[y, x] = count

    peak = np.unravel_index(np.argmax(density), density.shape)
    py, px = peak

    # Refine with centroid
    near = ((ys - py)**2 + (xs - px)**2) < (R * 1.2)**2
    if np.sum(near) > 5:
        return np.mean(xs[near]) * ds, np.mean(ys[near]) * ds
    return px * ds, py * ds


def find_piece(photo_gray, ph, pw):
    """Find puzzle piece center in top-left."""
    ds = 3
    region = photo_gray[:ph//3, :pw//3]
    small = region[::ds, ::ds]
    sh, sw = small.shape
    edges = np.zeros((sh, sw))
    for y in range(1, sh-1):
        for x in range(1, sw-1):
            gx = small[y,x+1] - small[y,x-1]
            gy = small[y+1,x] - small[y-1,x]
            edges[y,x] = math.sqrt(gx*gx + gy*gy)
    thresh = np.percentile(edges[edges>0], 75) if np.any(edges>0) else 1e9
    ey, ex = np.where(edges > thresh)
    if len(ey) > 20:
        return np.median(ex)*ds, np.median(ey)*ds
    return pw * 0.08, ph * 0.08


def solve_captcha(wda, max_attempts=10):
    """Solve ProtonCAPTCHA puzzle. Returns True on success."""
    for attempt in range(1, max_attempts + 1):
        # Check state
        retry = wda.find('name == "Retry"', timeout=3)
        if retry:
            wda.click(retry)
            time.sleep(4)
            # Wait for puzzle
            for _ in range(16):
                if wda.find('name == "Reset puzzle piece"', timeout=2):
                    break
                time.sleep(0.5)
            time.sleep(1)

        arr = wda.screenshot()
        photo = arr[PY1:PY2, PX1:PX2]
        ph, pw = photo.shape[:2]

        # Check not error screen
        center_bright = np.mean(photo[ph//3:2*ph//3, pw//3:2*pw//3, :3])
        if center_bright > 245:
            log.debug("Error screen, waiting...")
            time.sleep(2)
            continue

        photo_gray = np.mean(photo[:,:,:3], axis=2)

        # Find positions
        cutout_cx, cutout_cy = find_cutout_edges(photo, ph, pw)
        piece_cx, piece_cy = find_piece(photo_gray, ph, pw)

        piece_wda = ((piece_cx + PX1) / 3, (piece_cy + PY1) / 3)
        cutout_wda = ((cutout_cx + PX1) / 3, (cutout_cy + PY1) / 3)

        log.info("  Attempt %d: piece(%.0f,%.0f) -> cutout(%.0f,%.0f)",
                 attempt, piece_wda[0], piece_wda[1], cutout_wda[0], cutout_wda[1])

        # Pre-tap + drag
        wda.tap((PX1_W + PX2_W) / 2, (PY1_W + PY2_W) / 2, dur=50)
        time.sleep(0.2)
        wda.drag_human(piece_wda[0], piece_wda[1], cutout_wda[0], cutout_wda[1])
        time.sleep(1.5)

        # Click Next
        wda.tap(*NEXT_WDA, dur=50)
        time.sleep(3)

        # Check result
        retry = wda.find('name == "Retry"', timeout=3)
        if retry:
            log.info("  Attempt %d: failed", attempt)
            continue

        pcaptcha = wda.find('name == "pcaptcha"', timeout=3)
        if not pcaptcha:
            log.info("  CAPTCHA solved on attempt %d!", attempt)
            return True

        # pcaptcha still showing but no retry — might be processing
        time.sleep(2)
        pcaptcha2 = wda.find('name == "pcaptcha"', timeout=2)
        if not pcaptcha2:
            log.info("  CAPTCHA solved (delayed) on attempt %d!", attempt)
            return True

    log.warning("  CAPTCHA not solved after %d attempts", max_attempts)
    return False


# ─── ProtonMail signup flow ────────────────────────────────────────

def generate_proton_username(persona):
    """Generate a ProtonMail-friendly username from persona data."""
    base = persona["username_base"].replace("_", "").replace("-", "")
    suffix = random.randint(100, 9999)
    return f"{base}{suffix}"


def generate_password():
    """Generate a strong random password."""
    chars = string.ascii_letters + string.digits + "!@#$%"
    pwd = "".join(random.choices(chars, k=14))
    # Ensure at least one uppercase, one lowercase, one digit, one special
    pwd = (random.choice(string.ascii_uppercase) +
           random.choice(string.ascii_lowercase) +
           random.choice(string.digits) +
           random.choice("!@#$%") +
           pwd[:10])
    return pwd


def signup_protonmail(wda, persona):
    """Create a ProtonMail account for a persona. Returns dict or None."""
    username = generate_proton_username(persona)
    password = generate_password()
    addr = f"{username}@proton.me"

    log.info("Creating %s for %s", addr, persona["display_name"])

    # Step 1: Navigate to signup page
    wda.terminate("com.apple.mobilesafari")
    time.sleep(1)
    wda.activate("com.apple.mobilesafari")
    time.sleep(2)
    wda.open_url("https://account.proton.me/signup")
    time.sleep(6)

    # Step 2: Enter username
    username_field = wda.find('type == "XCUIElementTypeTextField"')
    if not username_field:
        log.error("Username field not found")
        return None
    wda.click(username_field)
    time.sleep(0.5)
    # Clear existing text
    wda.type_keys(["\u0008"] * 30)
    time.sleep(0.3)
    wda.type_keys(username)
    time.sleep(1)

    # Step 3: Check username availability
    time.sleep(2)

    # Step 4: Enter password
    pwd_field = wda.find('type == "XCUIElementTypeSecureTextField"')
    if not pwd_field:
        log.error("Password field not found")
        return None
    wda.click(pwd_field)
    time.sleep(0.5)
    wda.type_keys(password)
    time.sleep(1)

    # Step 5: Enter confirm password
    wda.tap(197, 500, dur=50)
    time.sleep(0.5)

    # Swipe up slightly to reveal confirm field if needed
    wda.client.post(f"{wda.s}/wda/dragfromtoforduration",
                    json={"fromX": 197, "fromY": 500, "toX": 197, "toY": 300, "duration": 0.3},
                    timeout=10)
    time.sleep(1)

    confirm_field = wda.find('type == "XCUIElementTypeSecureTextField"')
    if confirm_field:
        wda.click(confirm_field)
        time.sleep(0.5)
        wda.type_keys(password)
        time.sleep(1)

    # Step 6: Click submit button
    submit = wda.find('name CONTAINS "Start using Proton"')
    if not submit:
        submit = wda.find('name CONTAINS "Create"')
    if submit:
        wda.click(submit)
        log.info("Clicked submit")
    else:
        wda.client.post(f"{wda.s}/wda/dragfromtoforduration",
                        json={"fromX": 197, "fromY": 600, "toX": 197, "toY": 300, "duration": 0.3},
                        timeout=10)
        time.sleep(1)
        submit = wda.find('name CONTAINS "Start using"')
        if submit:
            wda.click(submit)
        else:
            log.error("Submit button not found")
            return None

    time.sleep(5)

    # Step 7: Handle verification dialog
    pcaptcha = wda.find('name == "pcaptcha"', timeout=5)
    if pcaptcha:
        log.info("CAPTCHA appeared, solving...")
        if not solve_captcha(wda, max_attempts=10):
            log.error("CAPTCHA solve failed")
            return None
        log.info("CAPTCHA solved!")
        time.sleep(3)

    # Step 8: Handle upsell
    upsell = wda.find('name == "No, thanks"', timeout=5)
    if upsell:
        log.info("Dismissing upsell")
        wda.click(upsell)
        time.sleep(3)

    # Step 9: Handle recovery kit
    for _ in range(5):
        checkbox = wda.find('type == "XCUIElementTypeSwitch"')
        if checkbox:
            wda.click(checkbox)
            time.sleep(1)

        cont = wda.find('name == "Continue"')
        if cont:
            wda.click(cont)
            time.sleep(3)
            continue

        skip = wda.find('name == "Skip"')
        if skip:
            wda.click(skip)
            time.sleep(3)
            continue

        welcome = wda.find('name == "Welcome"')
        if welcome:
            log.info("Account setup complete!")
            break

        time.sleep(2)

    # Step 10: Store in database
    email_id = str(uuid.uuid4())
    try:
        sync_execute(
            """INSERT INTO email_accounts (id, persona_id, provider, email, password,
               imap_host, imap_port, domain, status, phone_used)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (email_id, str(persona["id"]), "protonmail",
             encrypt(addr), encrypt(password),
             "proton.me", 0, "proton.me", "available", False),
        )
        log.info("Stored: %s (id=%s)", addr, email_id[:8])
    except Exception as e:
        log.error("DB insert failed: %s", e)
        return None

    return {
        "id": email_id,
        "email": addr,
        "password": password,
        "provider": "protonmail",
    }


# ─── Batch logic ───────────────────────────────────────────────────

def get_personas_needing_email():
    """Get personas that don't have a protonmail email account."""
    rows = sync_execute("""
        SELECT p.id, p.first_name, p.last_name, p.display_name,
               p.username_base, p.gender, p.date_of_birth, p.niche_id
        FROM personas p
        WHERE p.status IN ('active', 'ready')
          AND NOT EXISTS (
              SELECT 1 FROM email_accounts ea
              WHERE ea.persona_id = p.id
                AND ea.provider = 'protonmail'
                AND ea.status IN ('available', 'assigned')
          )
        ORDER BY p.niche_id, p.display_name
    """)
    return rows


def main():
    parser = argparse.ArgumentParser(description="Batch ProtonMail account creation")
    parser.add_argument("--limit", type=int, default=0, help="Max accounts (0=all)")
    parser.add_argument("--port", type=int, default=8100, help="WDA port")
    parser.add_argument("--delay", type=int, default=60, help="Seconds between signups")
    parser.add_argument("--dry-run", action="store_true", help="Show plan only")
    parser.add_argument("--niche", help="Filter by niche slug")
    args = parser.parse_args()

    personas = get_personas_needing_email()

    if args.niche:
        niche_row = sync_execute("SELECT id FROM niches WHERE slug = %s", (args.niche,))
        if niche_row:
            nid = str(list(niche_row[0].values())[0])
            personas = [p for p in personas if str(p["niche_id"]) == nid]

    if args.limit:
        personas = personas[:args.limit]

    if not personas:
        log.info("No personas need ProtonMail accounts")
        return

    log.info("=== %d personas need ProtonMail accounts ===", len(personas))

    if args.dry_run:
        for p in personas:
            un = generate_proton_username(p)
            print(f"  Would create {un}@proton.me for {p['display_name']}")
        return

    # Connect WDA
    wda = WDA(port=args.port)
    wda.connect()

    stats = {"success": 0, "fail": 0}

    for i, persona in enumerate(personas):
        log.info("--- [%d/%d] %s ---", i + 1, len(personas), persona["display_name"])

        result = signup_protonmail(wda, persona)
        if result:
            stats["success"] += 1
            log.info("SUCCESS: %s", result["email"])
        else:
            stats["fail"] += 1
            log.warning("FAILED: %s", persona["display_name"])

        if i < len(personas) - 1:
            log.info("Waiting %ds...", args.delay)
            time.sleep(args.delay)

    log.info("=== DONE: %d/%d success, %d failed ===",
             stats["success"], len(personas), stats["fail"])


if __name__ == "__main__":
    main()
