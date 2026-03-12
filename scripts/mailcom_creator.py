"""Mail.com account creator — production version.

Creates email accounts via headless Playwright with CaptchaFox slider solving.
Scale factor 0.93: canvas moves 93px per 100px of slider drag.
One attempt per challenge — restarts full flow on failure.
"""
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from PIL import Image
import io, time, random, string, sys

SCALE_FACTOR = 0.93


def gen_pw():
    pw = [random.choice(string.ascii_uppercase), random.choice(string.ascii_lowercase),
          random.choice(string.digits), random.choice("!@#$%")]
    pw.extend(random.choices(string.ascii_letters + string.digits + "!@#$%", k=12))
    random.shuffle(pw)
    return "".join(pw)


def find_icon_centers(img_bytes):
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


def drag_slider(page, offset):
    """Drag slider with human-like movement. Returns True if CAPTCHA solved."""
    try:
        page.wait_for_selector('.cf-slider__button', state='visible', timeout=5000)
    except Exception:
        return False
    time.sleep(0.5)
    btn = page.query_selector('.cf-slider__button')
    if not btn:
        return False
    box = btn.bounding_box()
    if not box:
        return False

    sx = box['x'] + box['width'] / 2
    sy = box['y'] + box['height'] / 2
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

    state = page.evaluate("document.querySelector('div[role=checkbox]')?.getAttribute('aria-checked')")
    return state == 'true'


def attempt_signup(page, first, last, month, day, year, gender):
    """Run one full signup attempt. Returns (email, password) or None."""
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
        print("    No email suggestion found")
        return None
    print(f"    Email: {chosen}")
    time.sleep(random.uniform(4, 6))

    # Step 3: Salutation + Country + State
    sal_idx = "0" if gender == "f" else "1"
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
    password = gen_pw()
    page.fill("#password", password)
    time.sleep(0.5)
    page.fill("#confirm-password", password)
    time.sleep(0.5)
    page.evaluate("document.querySelector('[data-test=progress-meter-next]').click()")
    time.sleep(random.uniform(4, 6))

    # Step 5: Skip phone
    page.evaluate("document.querySelector('[data-test=progress-meter-next]').click()")
    time.sleep(random.uniform(4, 6))

    # Step 6: Trigger CaptchaFox
    cb = page.query_selector('div[role="checkbox"]')
    if not cb:
        print("    No CaptchaFox checkbox found")
        return None
    box = cb.bounding_box()
    if not box:
        print("    Checkbox has no bounding box")
        return None
    page.mouse.move(box['x'] + box['width']/2, box['y'] + box['height']/2, steps=8)
    time.sleep(0.4)
    page.mouse.click(box['x'] + box['width']/2, box['y'] + box['height']/2)
    time.sleep(3)

    # Step 7: Solve slider
    canvas_area = page.query_selector('.cf-slide__action')
    if not canvas_area:
        print("    No slider canvas area")
        return None
    img_bytes = canvas_area.screenshot()
    result = find_icon_centers(img_bytes)
    if not result:
        print("    CV could not find icons")
        return None

    left_x, right_x = result
    canvas_dist = right_x - left_x
    slider_offset = int(canvas_dist / SCALE_FACTOR)
    slider_offset = max(10, min(slider_offset, 260))
    print(f"    Slider: canvas_dist={canvas_dist:.0f}, offset={slider_offset}")

    if not drag_slider(page, slider_offset):
        print("    Slider solve failed")
        return None
    print("    CAPTCHA solved!")

    # Step 8: Click "Agree and continue" (may be disabled in DOM, use JS click)
    time.sleep(2)
    page.evaluate("document.querySelector('[data-test=create-mailbox-create-button]')?.click()")
    time.sleep(3)
    # Double-check — try force click too
    create_btn = page.query_selector('[data-test=create-mailbox-create-button]')
    if create_btn:
        try:
            create_btn.click(force=True, timeout=5000)
        except Exception:
            pass
    time.sleep(15)

    return (chosen, password)


def create_mailcom_account(first, last, month, day, year, gender="m", max_attempts=5):
    """Create a mail.com account with retry on CAPTCHA failure."""
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
            print(f"  Attempt {attempt}/{max_attempts} for {first} {last}...")
            try:
                result = attempt_signup(page, first, last, month, day, year, gender)
                if result:
                    email, pw = result
                    print(f"  SUCCESS: {email}")
                    browser.close()
                    return (email, pw)
                else:
                    print(f"  Attempt {attempt} failed")
            except Exception as e:
                print(f"  Attempt {attempt} error: {e}")

            # Small delay between attempts
            time.sleep(random.uniform(2, 5))

        browser.close()
        return None


if __name__ == "__main__":
    # Test with a single persona
    result = create_mailcom_account("Aisha", "Johnson", "07", "22", "1994", "f")
    if result:
        print(f"\n=== ACCOUNT CREATED: {result[0]} / {result[1]} ===")
    else:
        print("\n=== ALL ATTEMPTS FAILED ===")
