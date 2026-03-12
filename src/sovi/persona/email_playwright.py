"""Email account creation via Playwright — mail.com with CaptchaFox solver.

Uses headed Chromium + playwright-stealth to create mail.com accounts.
CaptchaFox slider CAPTCHA solved via PIL-based icon detection.
Scale factor 0.93: canvas moves 93px per 100px of slider drag.
One attempt per challenge — restarts full flow on failure.
Stores credentials encrypted in email_accounts table.
"""

from __future__ import annotations

import io
import logging
import random
import string
import time

from PIL import Image

from sovi.crypto import encrypt
from sovi.db import sync_execute_one

logger = logging.getLogger(__name__)

SCALE_FACTOR = 0.93
MAX_CAPTCHA_ATTEMPTS = 5


def _generate_password() -> str:
    """Generate a strong random password meeting mail.com requirements."""
    pw = [
        random.choice(string.ascii_uppercase),
        random.choice(string.ascii_lowercase),
        random.choice(string.digits),
        random.choice("!@#$%"),
    ]
    pw.extend(random.choices(string.ascii_letters + string.digits + "!@#$%", k=12))
    random.shuffle(pw)
    return "".join(pw)


def _find_icon_centers(img_bytes: bytes) -> tuple[float, float] | None:
    """Find center X of source and target icons via non-white pixel clusters."""
    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    w, h = img.size
    pixels = img.load()
    col_scores = []
    for x in range(w):
        score = 0
        for y in range(h):
            r, g, b, a = pixels[x, y]
            if a > 128 and (r < 220 or g < 220 or b < 220):
                score += 1
        col_scores.append(score)
    threshold = max(col_scores) * 0.15 if max(col_scores) > 0 else 1
    clusters = []
    in_cluster = False
    start = 0
    for x, score in enumerate(col_scores):
        if score >= threshold:
            if not in_cluster:
                start = x
                in_cluster = True
        else:
            if in_cluster:
                clusters.append((start, x - 1))
                in_cluster = False
    if in_cluster:
        clusters.append((start, w - 1))
    clusters = [(s, e) for s, e in clusters if e - s >= 8]
    if len(clusters) >= 2:
        return (clusters[0][0] + clusters[0][1]) / 2, (clusters[-1][0] + clusters[-1][1]) / 2
    return None


def _drag_slider(page, offset: int) -> bool:
    """Drag CaptchaFox slider with human-like movement. Returns True if solved."""
    try:
        page.wait_for_selector(".cf-slider__button", state="visible", timeout=5000)
    except Exception:
        return False
    time.sleep(0.5)
    btn = page.query_selector(".cf-slider__button")
    if not btn:
        return False
    box = btn.bounding_box()
    if not box:
        return False

    sx = box["x"] + box["width"] / 2
    sy = box["y"] + box["height"] / 2
    tx = sx + offset

    page.mouse.move(sx, sy, steps=random.randint(4, 8))
    time.sleep(random.uniform(0.2, 0.4))
    page.mouse.down()
    time.sleep(random.uniform(0.1, 0.2))

    steps = random.randint(25, 40)
    for i in range(steps):
        p = (i + 1) / steps
        if p < 0.9:
            eased = 1 - (1 - p / 0.9) ** 2.5
            factor = eased * 0.95
        else:
            factor = 0.95 + (p - 0.9) / 0.1 * 0.05
        cx = sx + (tx - sx) * factor
        cy = sy + random.uniform(-1, 1)
        page.mouse.move(cx, cy)
        time.sleep(random.uniform(0.01, 0.03))

    page.mouse.move(tx, sy)
    time.sleep(random.uniform(0.3, 0.6))
    page.mouse.up()
    time.sleep(2)

    state = page.evaluate(
        "document.querySelector('div[role=checkbox]')?.getAttribute('aria-checked')"
    )
    return state == "true"


def _attempt_signup(page, first: str, last: str, month: str, day: str, year: str, gender: str) -> tuple[str, str] | None:
    """Run one full mail.com signup attempt. Returns (email, password) or None."""
    page.goto("https://signup.mail.com/", wait_until="load", timeout=30000)
    time.sleep(random.uniform(3, 5))

    # Step 1: Name + DOB
    page.fill("#given-name", first)
    time.sleep(random.uniform(0.2, 0.5))
    page.fill("#family-name", last)
    time.sleep(random.uniform(0.2, 0.5))
    page.fill("#bday-month", month)
    page.fill("#bday-day", day)
    page.fill("#bday-year", year)
    page.evaluate("document.querySelectorAll('button[type=button]')[0].click()")
    time.sleep(random.uniform(4, 6))

    # Step 2: Pick email suggestion
    chosen = page.evaluate("""(() => {
        const row = document.querySelector('onereg-suggestion-item-advanced');
        if (row) { const t = row.querySelector('.onereg-suggestion-item-advanced__text'); row.click(); return t ? t.textContent : null; }
        return null;
    })()""")
    if not chosen:
        return None
    logger.info("Email suggestion: %s", chosen)
    time.sleep(random.uniform(4, 6))

    # Step 3: Salutation + Country + State
    sal_idx = "0" if gender == "female" else "1"
    page.evaluate("""(() => {
        var radios = document.querySelectorAll('input[name=salutation]');
        var idx = """ + sal_idx + """;
        if (radios.length > idx) { radios[idx].checked = true; radios[idx].dispatchEvent(new Event('change', {bubbles: true})); }
        var country = document.querySelector('#country');
        if (country) { country.value = 'US'; country.dispatchEvent(new Event('change', {bubbles: true})); }
    })()""")
    time.sleep(2)
    page.evaluate("""(() => {
        var r = document.querySelector('#region');
        if (r && r.options.length > 1) { r.value = r.options[1].value; r.dispatchEvent(new Event('change', {bubbles: true})); }
    })()""")
    time.sleep(1)
    page.evaluate("document.querySelector('[data-test=progress-meter-next]').click()")
    time.sleep(random.uniform(4, 6))

    # Step 4: Password
    password = _generate_password()
    page.fill("#password", password)
    time.sleep(0.5)
    page.fill("#confirm-password", password)
    time.sleep(0.5)
    page.evaluate("document.querySelector('[data-test=progress-meter-next]').click()")
    time.sleep(random.uniform(4, 6))

    # Step 5: Skip phone
    page.evaluate("document.querySelector('[data-test=progress-meter-next]').click()")
    time.sleep(random.uniform(4, 6))

    # Step 6: CaptchaFox checkbox
    cb = page.query_selector('div[role="checkbox"]')
    if not cb or not cb.bounding_box():
        return None
    box = cb.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=8)
    time.sleep(0.4)
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    time.sleep(3)

    # Step 7: Solve slider
    canvas_area = page.query_selector(".cf-slide__action")
    if not canvas_area:
        return None
    img_bytes = canvas_area.screenshot()
    result = _find_icon_centers(img_bytes)
    if not result:
        return None

    left_x, right_x = result
    canvas_dist = right_x - left_x
    slider_offset = max(10, min(int(canvas_dist / SCALE_FACTOR), 260))

    if not _drag_slider(page, slider_offset):
        return None

    # Step 8: Click "Agree and continue"
    time.sleep(2)
    page.evaluate("document.querySelector('[data-test=create-mailbox-create-button]')?.click()")
    time.sleep(3)
    try:
        btn = page.query_selector("[data-test=create-mailbox-create-button]")
        if btn:
            btn.click(force=True, timeout=5000)
    except Exception:
        pass
    time.sleep(15)

    return (chosen, password)


def create_email_mailcom(persona: dict, max_attempts: int = MAX_CAPTCHA_ATTEMPTS) -> dict | None:
    """Create a mail.com email account for a persona.

    Returns dict with email_plain, password_plain, db_id on success, None on failure.
    """
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    first = persona["first_name"]
    last = persona["last_name"]
    dob = str(persona["date_of_birth"])  # YYYY-MM-DD
    gender = persona.get("gender", "female")
    parts = dob.split("-")
    month, day, year = parts[1], parts[2], parts[0]

    logger.info("Creating mail.com account for %s %s", first, last)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="en-US",
        )
        Stealth().apply_stealth_sync(context)
        page = context.new_page()

        for attempt in range(1, max_attempts + 1):
            logger.info("Attempt %d/%d for %s %s", attempt, max_attempts, first, last)
            try:
                result = _attempt_signup(page, first, last, month, day, year, gender)
                if result:
                    email, pw = result
                    browser.close()

                    # Store in DB
                    domain = email.split("@")[1] if "@" in email else "mail.com"
                    row = sync_execute_one(
                        """INSERT INTO email_accounts
                           (persona_id, provider, email, password, imap_host, imap_port, domain, status)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, 'available')
                           RETURNING id""",
                        (str(persona["id"]), "mailcom", encrypt(email), encrypt(pw),
                         "imap.mail.com", 993, domain),
                    )
                    db_id = row["id"] if row else None
                    logger.info("Created %s (db id=%s)", email, db_id)
                    return {"email_plain": email, "password_plain": pw, "db_id": db_id}
            except Exception as e:
                logger.warning("Attempt %d error: %s", attempt, e)

            time.sleep(random.uniform(2, 5))

        browser.close()
        return None


def create_emails_batch(personas: list[dict]) -> list[dict]:
    """Create mail.com email accounts for a batch of personas.

    Uses a single browser instance for efficiency.
    Returns list of successfully created account dicts.
    """
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    results = []
    total = len(personas)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="en-US",
        )
        Stealth().apply_stealth_sync(context)
        page = context.new_page()

        for i, persona in enumerate(personas):
            first = persona["first_name"]
            last = persona["last_name"]
            dob = str(persona["date_of_birth"])
            gender = persona.get("gender", "female")
            parts = dob.split("-")
            month, day, year = parts[1], parts[2], parts[0]

            logger.info("[%d/%d] %s %s", i + 1, total, first, last)
            success = False

            for attempt in range(1, MAX_CAPTCHA_ATTEMPTS + 1):
                try:
                    result = _attempt_signup(page, first, last, month, day, year, gender)
                    if result:
                        email, pw = result
                        domain = email.split("@")[1] if "@" in email else "mail.com"
                        row = sync_execute_one(
                            """INSERT INTO email_accounts
                               (persona_id, provider, email, password, imap_host, imap_port, domain, status)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, 'available')
                               RETURNING id""",
                            (str(persona["id"]), "mailcom", encrypt(email), encrypt(pw),
                             "imap.mail.com", 993, domain),
                        )
                        db_id = row["id"] if row else None
                        logger.info("Created %s (db id=%s)", email, db_id)
                        results.append({"email_plain": email, "password_plain": pw, "db_id": db_id})
                        success = True
                        break
                except Exception as e:
                    logger.warning("Attempt %d error: %s", attempt, e)

                time.sleep(random.uniform(2, 5))

            if not success:
                logger.warning("Failed all %d attempts for %s %s", MAX_CAPTCHA_ATTEMPTS, first, last)

            if i < total - 1:
                time.sleep(random.uniform(5, 15))

        browser.close()

    logger.info("Created %d/%d email accounts", len(results), total)
    return results
