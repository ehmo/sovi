"""Email account creation via Playwright — mail.com with CaptchaFox solver.

Uses headed Chromium + playwright-stealth to create mail.com accounts.
CaptchaFox slider CAPTCHA solved via PIL-based icon detection.
Scale factor 0.93: canvas moves 93px per 100px of slider drag.
One attempt per challenge — restarts full flow on failure.

Registration uses direct API call (POST /account/email-registration)
with exact headers extracted from Angular's registration service.
IMAP/POP3 is Premium-only — email reading done via web interface.
"""

from __future__ import annotations

import io
import json
import logging
import random
import string
import time

from PIL import Image

from sovi.crypto import encrypt
from sovi.db import sync_execute_one

logger = logging.getLogger(__name__)

SCALE_FACTOR = 0.93
MAX_CAPTCHA_ATTEMPTS = 3


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


def _drag_slider_once(page, offset: int) -> bool:
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


def _solve_captchafox(page) -> bool:
    """Solve CaptchaFox slider CAPTCHA with multiple attempts and offsets."""
    for attempt in range(6):
        canvas_area = page.query_selector(".cf-slide__action")
        if not canvas_area:
            checkbox = page.query_selector('div[role=checkbox]')
            if checkbox:
                state = checkbox.get_attribute("aria-checked")
                if state == "true":
                    return True
            return False

        img_bytes = canvas_area.screenshot()
        result = _find_icon_centers(img_bytes)

        if result:
            left_x, right_x = result
            canvas_dist = right_x - left_x
            base_offset = max(10, min(int(canvas_dist / SCALE_FACTOR), 260))
            offsets = [base_offset, base_offset + 8, base_offset - 8,
                       base_offset + 16, base_offset - 16]
        else:
            logger.info("Icon detection failed, trying common offsets")
            offsets = [120, 140, 100, 160, 80, 180]

        offset = offsets[attempt % len(offsets)]
        logger.info("CaptchaFox attempt %d: offset=%d", attempt + 1, offset)

        if _drag_slider_once(page, offset):
            logger.info("CaptchaFox solved on attempt %d (offset=%d)", attempt + 1, offset)
            return True

        time.sleep(random.uniform(1.5, 3.0))

    return False


def _extract_service_config(page) -> dict | None:
    """Extract registration service config from Angular's injector."""
    config = page.evaluate("""(() => {
        try {
            var t = getAllAngularTestabilities()[0];
            var injector = t._destroyRef;
            var config = {};
            injector.records.forEach(function(v, k) {
                if (v && v.value && typeof v.value.createAccount === 'function') {
                    var svc = v.value;
                    config.appIdentifier = svc.appIdentifier;
                    config.clientCredentialGuid = svc.clientCredentialGuid;
                    config.templateName = svc.templateName;
                    config.authToken = svc.authToken;
                    config.accountRestUrl = svc.accountRestUrl;
                    config.referrerSource = svc.referrerSource;
                    config.captchaFox = svc.captchaFox;
                }
            });
            return config;
        } catch(e) {
            return null;
        }
    })()""")
    if not config or not config.get("accountRestUrl"):
        logger.warning("Failed to extract service config")
        return None
    return config


def _extract_store_data(page) -> dict | None:
    """Extract account data from NgRx store using Angular's helper.getUserInfoFrom().

    The API expects a nested structure with fields like givenName, familyName,
    credentials.password, address.countryCode — NOT the flat store field names.
    """
    data = page.evaluate("""(() => {
        try {
            var t = getAllAngularTestabilities()[0];
            var store = null;
            var effectSvc = null;
            t._destroyRef.records.forEach(function(v, k) {
                if (!v || !v.value) return;
                if (typeof v.value.dispatch === 'function') store = v.value;
                try {
                    if (Object.keys(v.value).indexOf('submitFreemail') >= 0) effectSvc = v.value;
                } catch(e) {}
            });
            var src = store.source;
            while (src && !src._value && src.source) src = src.source;
            var state = src._value;

            var accountData;
            if (effectSvc && effectSvc.helper) {
                // Use Angular's helper for the correct nested format
                accountData = effectSvc.helper.getUserInfoFrom(
                    state.passwordGroup,
                    state.personalInfoGroup,
                    state.paymentGroup,
                    state.passwordRecoveryGroup
                );
            } else {
                // Fallback: build correct nested format manually
                accountData = {
                    givenName: state.personalInfoGroup?.firstName?.value,
                    familyName: state.personalInfoGroup?.lastName?.value,
                    gender: state.personalInfoGroup?.salutation?.value,
                    birthDate: state.personalInfoGroup?.dateOfBirth?.value,
                    address: {
                        countryCode: state.personalInfoGroup?.address?.country?.value,
                        region: state.personalInfoGroup?.address?.region?.value,
                    },
                    credentials: {
                        password: state.passwordGroup?.password?.value,
                    },
                };
            }
            accountData.emailAddress = state.aliasCheck?.selectedMailAddress;
            return accountData;
        } catch(e) {
            return null;
        }
    })()""")
    if not data or not data.get("emailAddress"):
        logger.warning("Failed to extract store data")
        return None
    return data


def _call_registration_api(page, store_data: dict, svc_config: dict,
                           captcha_token: str, proxy: str | None = None,
                           ssh_host: str | None = None) -> dict:
    """Call registration API. Supports browser fetch, HTTP proxy, or SSH+curl.

    If proxy is set, uses httpx through the proxy (bypasses browser IP).
    If ssh_host is set, runs curl on remote host via SSH (bypasses local IP).
    Otherwise uses browser's fetch() (same IP as browser session).
    """
    import uuid as _uuid

    api_url = svc_config["accountRestUrl"] + "/email-registration"
    headers = {
        "Content-Type": "application/vnd.ui.mam.account.creation+json",
        "X-UI-APP": svc_config.get("appIdentifier", ""),
        "X-CCGUID": svc_config.get("clientCredentialGuid", ""),
        "X-REQUEST-ID": str(_uuid.uuid4()),
        "Template-Name": svc_config.get("templateName", ""),
        "Authorization": svc_config.get("authToken", ""),
        "cf-captcha-response": captcha_token,
    }
    if svc_config.get("referrerSource"):
        headers["Source"] = svc_config["referrerSource"]

    body = {k: v for k, v in store_data.items() if v is not None}

    if ssh_host:
        # Execute curl on remote host via SSH
        import shlex
        import subprocess
        header_args = " ".join(f'-H {shlex.quote(f"{k}: {v}")}' for k, v in headers.items())
        body_json = shlex.quote(json.dumps(body))
        cmd = f"curl -s -w '\\n%{{http_code}}' -X POST {shlex.quote(api_url)} {header_args} -d {body_json}"
        try:
            result = subprocess.run(
                ["ssh", "-o", "StrictHostKeyChecking=no", ssh_host, cmd],
                capture_output=True, text=True, timeout=30,
            )
            output = result.stdout.strip()
            lines = output.rsplit("\n", 1)
            resp_body = lines[0] if len(lines) > 1 else ""
            status = int(lines[-1]) if lines[-1].isdigit() else 0
            return {"status": status, "body": resp_body[:500]}
        except Exception as e:
            return {"error": str(e)}

    if proxy:
        # Use httpx with proxy
        import httpx
        headers["Origin"] = "https://signup.mail.com"
        headers["Referer"] = "https://signup.mail.com/"
        try:
            resp = httpx.post(api_url, headers=headers, json=body,
                              proxy=proxy, timeout=30.0)
            return {"status": resp.status_code, "body": resp.text[:500]}
        except Exception as e:
            return {"error": str(e)}

    # Default: use browser's fetch()
    return page.evaluate("""(args) => {
        var storeData = args[0];
        var config = args[1];
        var captchaToken = args[2];
        var headers = args[3];

        var body = JSON.parse(JSON.stringify(storeData));

        return fetch(config.accountRestUrl + '/email-registration', {
            method: 'POST',
            headers: headers,
            body: JSON.stringify(body),
            credentials: 'same-origin'
        }).then(function(r) {
            return r.text().then(function(t) {
                return {status: r.status, body: t.substring(0, 500)};
            });
        }).catch(function(e) {
            return {error: e.message};
        });
    }""", [store_data, svc_config, captcha_token, headers])


def _submit_login_form(page, email: str, password: str) -> bool:
    """Submit the hidden login form after successful registration."""
    page.evaluate("""(args) => {
        var form = document.querySelector('onereg-login-form form');
        if (form) {
            var inputs = form.querySelectorAll('input');
            for (var i = 0; i < inputs.length; i++) {
                var inp = inputs[i];
                if (inp.name === 'username') inp.value = args[0];
                if (inp.name === 'password') inp.value = args[1];
                if (inp.name === 'successURL') inp.value = 'https://navigator-lxa.mail.com/login';
                if (inp.name === 'service') inp.value = 'mailint';
            }
            form.submit();
        }
    }""", [email, password])
    time.sleep(15)

    # Click activate button if present
    activate = page.query_selector("#continueButton")
    if activate:
        logger.info("Clicking activate button...")
        activate.click()
        time.sleep(15)

    url = page.url
    title = page.evaluate("document.title")
    logger.info("After login: URL=%s, Title=%s", url[:100], title[:60])
    return "mail" in title.lower() or "navigator" in url or "logout" not in url


def _attempt_signup(page, first: str, last: str, month: str, day: str, year: str, gender: str,
                    proxy: str | None = None, ssh_host: str | None = None) -> tuple[str, str] | None:
    """Run one full mail.com signup attempt via direct API. Returns (email, password) or None."""
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
        var row = document.querySelector('onereg-suggestion-item-advanced');
        if (row) { var t = row.querySelector('.onereg-suggestion-item-advanced__text'); row.click(); return t ? t.textContent : null; }
        return null;
    })()""")
    if not chosen:
        logger.warning("No email suggestion found")
        return None
    chosen = chosen.strip()
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

    # Step 5: Skip phone recovery
    page.evaluate("document.querySelector('[data-test=progress-meter-next]').click()")
    time.sleep(random.uniform(4, 6))

    # Step 6: Extract service config BEFORE captcha
    svc_config = _extract_service_config(page)
    if not svc_config:
        logger.warning("Could not extract service config")
        return None
    logger.info("Service config extracted (appId=%s)", svc_config.get("appIdentifier", "?")[:40])

    # Step 7: Solve CaptchaFox
    cb = page.query_selector('div[role="checkbox"]')
    if not cb or not cb.bounding_box():
        logger.warning("CaptchaFox checkbox not found")
        return None

    box = cb.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=8)
    time.sleep(0.4)
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    time.sleep(3)

    if not _solve_captchafox(page):
        logger.warning("CaptchaFox solve failed")
        return None
    time.sleep(2)

    # Step 8: Get captcha token and store data
    captcha_token = page.evaluate("window.captchafox?.getResponse() || ''")
    if not captcha_token:
        logger.warning("No captcha token available")
        return None
    logger.info("Captcha token: %s...", captcha_token[:40])

    store_data = _extract_store_data(page)
    if not store_data:
        logger.warning("Could not extract store data")
        return None

    # Step 9: Call registration API (via proxy/ssh if configured)
    api_result = _call_registration_api(page, store_data, svc_config, captcha_token,
                                        proxy=proxy, ssh_host=ssh_host)
    logger.info("API response: status=%s", api_result.get("status"))

    if api_result.get("status") == 204:
        logger.info("Registration SUCCESS (204) for %s", chosen)
        time.sleep(3)

        # Submit login form and activate
        _submit_login_form(page, chosen, password)
        return (chosen, password)

    if api_result.get("status") == 403:
        logger.warning("Rate limited (403) — need to wait or change IP")
        return None

    logger.warning("API error: status=%s body=%s",
                   api_result.get("status"), api_result.get("body", "")[:200])
    return None


def login_web(email: str, password: str, page=None, browser=None, context=None) -> dict | None:
    """Login to mail.com web interface. Returns page object or None.

    If page/browser not provided, creates new browser session.
    Returns dict with 'page', 'browser', 'context' keys on success.
    Caller is responsible for closing browser when done.
    """
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    own_playwright = False
    pw_instance = None

    if not page:
        own_playwright = True
        pw_instance = sync_playwright().start()
        browser = pw_instance.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="en-US",
        )
        Stealth().apply_stealth_sync(context)
        page = context.new_page()

    page.goto("https://signup.mail.com/", wait_until="load", timeout=30000)
    time.sleep(3)

    _submit_login_form(page, email, password)
    url = page.url
    if "logout" in url:
        logger.warning("Login failed for %s — redirected to logout", email)
        if own_playwright:
            browser.close()
            pw_instance.stop()
        return None

    return {"page": page, "browser": browser, "context": context,
            "pw_instance": pw_instance if own_playwright else None}


def read_inbox_web(email: str, password: str, max_emails: int = 10) -> list[dict]:
    """Read inbox emails via mail.com web interface.

    Returns list of dicts with 'subject', 'from', 'body' keys.
    IMAP/POP3 is Premium-only on mail.com — this uses the web UI.
    """
    session = login_web(email, password)
    if not session:
        return []

    page = session["page"]
    results = []

    try:
        time.sleep(5)
        # Find mail frame (3c-lxa.mail.com)
        mail_frame = None
        for frame in page.frames:
            if "3c-lxa" in frame.url and "folder" in frame.url:
                mail_frame = frame
                break

        if not mail_frame:
            logger.warning("No mail frame found")
            return results

        # Read email subjects from inbox
        inbox_data = mail_frame.evaluate("""(() => {
            var subjects = document.querySelectorAll('.subject');
            return Array.from(subjects).map(function(s) {
                var row = s.closest('a, tr, [onclick], [role=row], .maillist-row');
                return {
                    text: s.textContent?.trim()?.substring(0, 120),
                    rowId: row?.getAttribute('data-mailid') || row?.id || ''
                };
            });
        })()""")

        for item in inbox_data[:max_emails]:
            results.append({
                "subject": item.get("text", ""),
                "from": "",
                "body": "",
            })

    finally:
        if session.get("pw_instance"):
            session["browser"].close()
            session["pw_instance"].stop()
        elif session.get("browser"):
            session["browser"].close()

    return results


def read_latest_email_web(email: str, password: str, subject_contains: str = "") -> str | None:
    """Read the body of the latest email matching subject filter.

    Returns email body text or None if not found.
    Useful for extracting verification codes from social platform signups.
    """
    session = login_web(email, password)
    if not session:
        return None

    page = session["page"]

    try:
        time.sleep(5)
        mail_frame = None
        for frame in page.frames:
            if "3c-lxa" in frame.url and "folder" in frame.url:
                mail_frame = frame
                break

        if not mail_frame:
            return None

        # Click matching email
        click_result = mail_frame.evaluate("""(filter) => {
            var subjects = document.querySelectorAll('.subject');
            for (var i = 0; i < subjects.length; i++) {
                var text = subjects[i].textContent?.trim() || '';
                if (!filter || text.toLowerCase().indexOf(filter.toLowerCase()) > -1) {
                    var row = subjects[i].closest('a, tr, [onclick], [role=row], .maillist-row');
                    if (row) { row.click(); return 'clicked'; }
                    subjects[i].click();
                    return 'clicked-subject';
                }
            }
            return 'not-found';
        }""", subject_contains)

        if "not-found" in click_result:
            return None

        time.sleep(5)

        # Read email body from nested iframe (iframe_2 in the mail frame)
        body = mail_frame.evaluate("""(() => {
            var iframes = document.querySelectorAll('iframe');
            for (var i = 0; i < iframes.length; i++) {
                try {
                    var iframeBody = iframes[i].contentDocument?.body?.innerText;
                    if (iframeBody && iframeBody.length > 50) return iframeBody.substring(0, 5000);
                } catch(e) {}
            }
            return document.body?.innerText?.substring(0, 5000) || '';
        })()""")

        return body if body else None

    finally:
        if session.get("pw_instance"):
            session["browser"].close()
            session["pw_instance"].stop()
        elif session.get("browser"):
            session["browser"].close()


def poll_for_verification_code(email: str, password: str, subject_contains: str,
                                code_pattern: str = r'\b\d{4,8}\b',
                                timeout: int = 120, interval: int = 15) -> str | None:
    """Poll inbox for verification code via web interface.

    Args:
        email: Mail.com email address
        password: Account password
        subject_contains: Filter emails by subject
        code_pattern: Regex pattern for the verification code
        timeout: Max seconds to wait
        interval: Seconds between polls

    Returns the first matching code or None.
    """
    import re

    start = time.time()
    while time.time() - start < timeout:
        body = read_latest_email_web(email, password, subject_contains)
        if body:
            match = re.search(code_pattern, body)
            if match:
                return match.group(0)
        time.sleep(interval)
    return None


def create_email_mailcom(persona: dict, max_attempts: int = MAX_CAPTCHA_ATTEMPTS,
                         proxy: str | None = None,
                         ssh_host: str | None = None) -> dict | None:
    """Create a mail.com email account for a persona.

    Args:
        persona: Dict with first_name, last_name, date_of_birth, gender.
        max_attempts: Max captcha retry attempts.
        proxy: Optional SOCKS5 proxy URL for API call (e.g. 'socks5://user:pass@host:port').
        ssh_host: Optional SSH host to run API call via curl (e.g. 'user@host').

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

    launch_kwargs = {
        "headless": False,
        "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    }
    if proxy:
        launch_kwargs["proxy"] = {"server": proxy}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
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
                result = _attempt_signup(page, first, last, month, day, year, gender,
                                        proxy=proxy, ssh_host=ssh_host)
                if result:
                    email, pw = result
                    browser.close()

                    domain = email.split("@")[1] if "@" in email else "mail.com"
                    row = sync_execute_one(
                        """INSERT INTO email_accounts
                           (persona_id, provider, email, password, imap_host, imap_port, domain, status)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, 'available')
                           RETURNING id""",
                        (str(persona["id"]), "mailcom", encrypt(email), encrypt(pw),
                         "web-only", 0, domain),
                    )
                    db_id = row["id"] if row else None
                    logger.info("Created %s (db id=%s)", email, db_id)
                    return {"email_plain": email, "password_plain": pw, "db_id": db_id}
            except Exception as e:
                logger.warning("Attempt %d error: %s", attempt, e)

            time.sleep(random.uniform(2, 5))

        browser.close()
        return None


def create_emails_batch(personas: list[dict], proxy: str | None = None,
                        ssh_host: str | None = None) -> list[dict]:
    """Create mail.com email accounts for a batch of personas.

    Args:
        personas: List of persona dicts.
        proxy: Optional SOCKS5 proxy URL for API calls.
        ssh_host: Optional SSH host for API calls via curl.

    Recreates browser context every few personas or on crash for resilience.
    Returns list of successfully created account dicts.
    """
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    results = []
    total = len(personas)
    REFRESH_EVERY = 5

    launch_kwargs = {
        "headless": False,
        "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    }
    if proxy:
        launch_kwargs["proxy"] = {"server": proxy}

    with sync_playwright() as p:
        browser = None
        page = None

        def _new_browser():
            nonlocal browser, page
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            browser = p.chromium.launch(**launch_kwargs)
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                locale="en-US",
            )
            Stealth().apply_stealth_sync(context)
            page = context.new_page()
            return page

        page = _new_browser()

        for i, persona in enumerate(personas):
            if i > 0 and i % REFRESH_EVERY == 0:
                logger.info("Refreshing browser (every %d personas)", REFRESH_EVERY)
                page = _new_browser()

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
                    result = _attempt_signup(page, first, last, month, day, year, gender,
                                            proxy=proxy, ssh_host=ssh_host)
                    if result:
                        email, pw = result
                        domain = email.split("@")[1] if "@" in email else "mail.com"
                        row = sync_execute_one(
                            """INSERT INTO email_accounts
                               (persona_id, provider, email, password, imap_host, imap_port, domain, status)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, 'available')
                               RETURNING id""",
                            (str(persona["id"]), "mailcom", encrypt(email), encrypt(pw),
                             "web-only", 0, domain),
                        )
                        db_id = row["id"] if row else None
                        logger.info("Created %s (db id=%s)", email, db_id)
                        results.append({"email_plain": email, "password_plain": pw, "db_id": db_id})
                        success = True
                        break
                except Exception as e:
                    logger.warning("Attempt %d error: %s", attempt, e)
                    if "closed" in str(e).lower() or "crash" in str(e).lower():
                        logger.info("Browser crashed, recreating...")
                        try:
                            page = _new_browser()
                        except Exception:
                            logger.error("Failed to recreate browser", exc_info=True)
                            break

                time.sleep(random.uniform(2, 5))

            if not success:
                logger.warning("Failed all %d attempts for %s %s", MAX_CAPTCHA_ATTEMPTS, first, last)

            if i < total - 1:
                time.sleep(random.uniform(5, 15))

        if browser:
            try:
                browser.close()
            except Exception:
                pass

    logger.info("Created %d/%d email accounts", len(results), total)
    return results
