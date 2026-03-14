#!/usr/bin/env python3
"""Batch register mail.com accounts for all personas.

Uses studio browser for form filling + CaptchaFox solving.
Uses phone cellular IP (via WebInspector) for API calls to bypass rate limiting.
Rotates phone IP via airplane mode toggle between accounts.

Usage: ssh studio 'cd ~/Work/ai/sovi && .venv/bin/python scripts/register_batch.py'
"""
import asyncio
import concurrent.futures
import json
import logging
import os
import random
import sys
import time
import uuid

sys.path.insert(0, "src")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from sovi.persona.email_playwright import _solve_captchafox, _generate_password

# Phone config
UDID_B = "00008140-001975DC3678801C"
WDA_B = "http://localhost:8100"
ACCOUNTS_PER_IP = 5  # Rotate IP after this many accounts


def get_personas():
    """Get all personas that need email accounts."""
    from sovi.db import sync_execute
    rows = sync_execute("""
        SELECT p.id, p.first_name, p.last_name, p.date_of_birth, p.gender,
               p.niche, p.username_base
        FROM personas p
        LEFT JOIN email_accounts ea ON ea.persona_id = p.id::text
        WHERE ea.id IS NULL
        ORDER BY p.niche, p.first_name
    """)
    return [dict(r) for r in rows] if rows else []


def rotate_phone_ip():
    """Toggle airplane mode on the phone to get a fresh cellular IP."""
    import requests
    try:
        # Open Control Center and toggle airplane mode
        sid_resp = requests.post(f"{WDA_B}/session",
            json={"capabilities": {"alwaysMatch": {"bundleId": "com.apple.Preferences"}}},
            timeout=10)
        sid = sid_resp.json()["value"]["sessionId"]
        # Swipe down from top-right for Control Center
        requests.post(f"{WDA_B}/session/{sid}/wda/swipe",
            json={"fromX": 350, "fromY": 0, "toX": 350, "toY": 400, "duration": 0.3}, timeout=10)
        time.sleep(1)
        # Tap airplane mode icon (top-left of control center grid)
        requests.post(f"{WDA_B}/session/{sid}/wda/tap", json={"x": 68, "y": 180}, timeout=10)
        time.sleep(3)
        # Toggle back off
        requests.post(f"{WDA_B}/session/{sid}/wda/tap", json={"x": 68, "y": 180}, timeout=10)
        time.sleep(8)  # Wait for cellular to reconnect
        # Close control center
        requests.post(f"{WDA_B}/session/{sid}/wda/swipe",
            json={"fromX": 200, "fromY": 800, "toX": 200, "toY": 400, "duration": 0.3}, timeout=10)
        time.sleep(1)
        logger.info("Phone IP rotated via airplane mode toggle")
        return True
    except Exception as e:
        logger.warning("Failed to rotate phone IP: %s", e)
        return False


async def phone_api_call(api_url, headers_dict, body_dict):
    """Execute registration API call from phone's Safari via WebInspector."""
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.webinspector import WebinspectorService

    lockdown = create_using_usbmux(serial=UDID_B)
    if asyncio.iscoroutine(lockdown):
        lockdown = await lockdown

    inspector = WebinspectorService(lockdown=lockdown)
    await asyncio.wait_for(inspector.connect(), timeout=15)
    await asyncio.sleep(2)

    pages = await inspector.get_open_pages()
    target_app_id = None
    target_page = None
    for app_id, app_pages in pages.items():
        if "Safari" in str(app_id) or "safari" in str(app_id).lower():
            page_list = list(app_pages) if not isinstance(app_pages, list) else app_pages
            if page_list:
                target_app_id = app_id
                target_page = page_list[0]
                break

    if not target_page:
        await inspector.close()
        return {"error": "no safari pages"}

    app = await inspector.open_app("com.apple.mobilesafari")
    session = await inspector.inspector_session(app, target_page)
    await asyncio.sleep(1)

    fetch_js = f"""
    (function() {{
        window.__apiResult = 'PENDING';
        fetch('{api_url}', {{
            method: 'POST',
            headers: {json.dumps(headers_dict)},
            body: JSON.stringify({json.dumps(body_dict)}),
            credentials: 'omit'
        }}).then(function(r) {{
            return r.text().then(function(t) {{
                window.__apiResult = 'OK:' + r.status + ':' + t.substring(0, 500);
            }});
        }}).catch(function(e) {{
            window.__apiResult = 'ERR:' + e.message;
        }});
        return 'started';
    }})()
    """

    await session.runtime_evaluate(fetch_js)

    for _ in range(30):
        await asyncio.sleep(2)
        poll = await session.runtime_evaluate("window.__apiResult")
        val = poll if isinstance(poll, str) else (
            poll.get("result", {}).get("value", "PENDING") if isinstance(poll, dict) else str(poll))
        if isinstance(val, str) and val != "PENDING":
            try:
                await inspector.close()
            except Exception:
                pass
            return val

    try:
        await inspector.close()
    except Exception:
        pass
    return "TIMEOUT"


def phone_api_call_sync(api_url, headers_dict, body_dict):
    """Run async phone_api_call in a separate thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(phone_api_call(api_url, headers_dict, body_dict))
    finally:
        loop.close()


def ensure_phone_on_signup():
    """Navigate phone Safari to signup.mail.com for same-origin fetch."""
    import requests
    try:
        sid = requests.post(f"{WDA_B}/session",
            json={"capabilities": {"alwaysMatch": {"bundleId": "com.apple.mobilesafari"}}},
            timeout=10).json()["value"]["sessionId"]
        requests.post(f"{WDA_B}/session/{sid}/wda/tap", json={"x": 200, "y": 85}, timeout=10)
        time.sleep(1)
        requests.post(f"{WDA_B}/session/{sid}/wda/keys",
            json={"value": list("https://signup.mail.com/\n")}, timeout=10)
        time.sleep(8)
    except Exception as e:
        logger.warning("Failed to navigate phone: %s", e)


def register_one(page, persona, use_phone=True):
    """Register one mail.com account. Returns (email, password) or None."""
    first = persona["first_name"]
    last = persona["last_name"]
    dob = str(persona["date_of_birth"])
    gender = persona.get("gender", "female")
    parts = dob.split("-")
    month, day, year = parts[1], parts[2], parts[0]
    sal_idx = "0" if gender == "female" else "1"

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

    # Step 2: Pick email
    chosen = page.evaluate("""(() => {
        var row = document.querySelector('onereg-suggestion-item-advanced');
        if (row) { var t = row.querySelector('.onereg-suggestion-item-advanced__text'); row.click(); return t ? t.textContent : null; }
        return null;
    })()""")
    if not chosen:
        logger.warning("No email suggestion for %s %s", first, last)
        return None
    chosen = chosen.strip()
    logger.info("Email suggestion: %s", chosen)
    time.sleep(random.uniform(4, 6))

    # Step 3: Personal info
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

    # Step 5: Skip recovery
    page.evaluate("document.querySelector('[data-test=progress-meter-next]').click()")
    time.sleep(random.uniform(4, 6))

    # Step 6: Solve CaptchaFox
    cb = page.query_selector('div[role="checkbox"]')
    if not cb or not cb.bounding_box():
        logger.warning("CaptchaFox not found")
        return None
    box = cb.bounding_box()
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    time.sleep(3)
    if not _solve_captchafox(page):
        logger.warning("CaptchaFox solve failed")
        return None
    time.sleep(2)

    # Step 7: Extract data with correct format
    data = page.evaluate("""(() => {
        try {
            var t = getAllAngularTestabilities()[0];
            var svc = null, store = null, effectSvc = null;
            t._destroyRef.records.forEach(function(v, k) {
                if (!v || !v.value) return;
                if (v.value.createAccount && typeof v.value.createAccount === 'function' && v.value.accountRestUrl) svc = v.value;
                if (typeof v.value.dispatch === 'function') store = v.value;
                try { if (Object.keys(v.value).indexOf('submitFreemail') >= 0) effectSvc = v.value; } catch(e) {}
            });
            var src = store.source;
            while (src && !src._value && src.source) src = src.source;
            var state = src._value;
            var captchaToken = state.captcha?.userSolution?.value || '';

            var accountData;
            if (effectSvc && effectSvc.helper) {
                accountData = effectSvc.helper.getUserInfoFrom(
                    state.passwordGroup, state.personalInfoGroup,
                    state.paymentGroup, state.passwordRecoveryGroup);
            } else {
                accountData = {
                    givenName: state.personalInfoGroup?.firstName?.value,
                    familyName: state.personalInfoGroup?.lastName?.value,
                    gender: state.personalInfoGroup?.salutation?.value,
                    birthDate: state.personalInfoGroup?.dateOfBirth?.value,
                    address: { countryCode: state.personalInfoGroup?.address?.country?.value, region: state.personalInfoGroup?.address?.region?.value },
                    credentials: { password: state.passwordGroup?.password?.value },
                };
            }
            accountData.emailAddress = state.aliasCheck?.selectedMailAddress;

            return {
                config: {
                    accountRestUrl: svc.accountRestUrl,
                    appIdentifier: svc.appIdentifier,
                    clientCredentialGuid: svc.clientCredentialGuid,
                    templateName: svc.templateName,
                    authToken: svc.authToken,
                    referrerSource: svc.referrerSource || '',
                },
                accountData: accountData,
                captchaToken: captchaToken,
            };
        } catch(e) { return null; }
    })()""")

    if not data:
        logger.warning("Failed to extract registration data")
        return None

    config = data["config"]
    account_data = data["accountData"]
    captcha = data["captchaToken"]
    api_url = config["accountRestUrl"] + "/email-registration"

    headers = {
        "Content-Type": "application/vnd.ui.mam.account.creation+json",
        "X-UI-APP": config["appIdentifier"],
        "X-CCGUID": config["clientCredentialGuid"],
        "X-REQUEST-ID": str(uuid.uuid4()),
        "Template-Name": config["templateName"],
        "Authorization": config["authToken"],
        "cf-captcha-response": captcha,
    }
    if config.get("referrerSource"):
        headers["Source"] = config["referrerSource"]

    body = {k: v for k, v in account_data.items() if v is not None}

    # Step 8: Make API call
    if use_phone:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(phone_api_call_sync, api_url, headers, body)
            try:
                result = future.result(timeout=120)
            except Exception as e:
                logger.error("Phone API call failed: %s", e)
                return None
    else:
        # Direct from browser
        result = page.evaluate("""(args) => {
            return fetch(args.url, {
                method: 'POST',
                headers: args.headers,
                body: JSON.stringify(args.body),
                credentials: 'same-origin'
            }).then(function(r) {
                return r.text().then(function(t) {
                    return 'OK:' + r.status + ':' + t.substring(0, 500);
                });
            }).catch(function(e) {
                return 'ERR:' + e.message;
            });
        }""", {"url": api_url, "headers": headers, "body": body})

    logger.info("API result: %s", str(result)[:200])

    if isinstance(result, str) and "OK:204" in result:
        logger.info("SUCCESS: %s", chosen)
        return (chosen, password)
    elif isinstance(result, str) and "OK:403" in result:
        logger.warning("Rate limited (403) — need IP rotation")
        return None
    elif isinstance(result, str) and "OK:502" in result:
        logger.error("Server error (502) — API is down")
        return None
    else:
        logger.warning("Unexpected result: %s", str(result)[:200])
        return None


def main():
    personas = get_personas()
    if not personas:
        logger.info("No personas need email accounts")
        return

    logger.info("Found %d personas needing email accounts", len(personas))

    # Navigate phone to signup.mail.com
    ensure_phone_on_signup()

    results = []
    failed = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="en-US",
        )
        Stealth().apply_stealth_sync(ctx)
        page = ctx.new_page()

        for i, persona in enumerate(personas):
            logger.info("\n=== Persona %d/%d: %s %s (%s) ===",
                       i + 1, len(personas), persona["first_name"], persona["last_name"], persona["niche"])

            # Rotate phone IP every N accounts
            if i > 0 and i % ACCOUNTS_PER_IP == 0:
                logger.info("Rotating phone IP...")
                rotate_phone_ip()
                ensure_phone_on_signup()

            try:
                result = register_one(page, persona, use_phone=True)
                if result:
                    email, pw = result
                    results.append({"persona_id": persona["id"], "email": email, "password": pw})
                    logger.info("Created: %s", email)
                else:
                    failed.append(persona["id"])
                    logger.warning("Failed for %s %s", persona["first_name"], persona["last_name"])
            except Exception as e:
                logger.error("Error for %s %s: %s", persona["first_name"], persona["last_name"], e)
                failed.append(persona["id"])

            # Random delay between accounts
            time.sleep(random.uniform(5, 15))

        browser.close()

    # Summary
    logger.info("\n=== Summary ===")
    logger.info("Created: %d/%d", len(results), len(personas))
    logger.info("Failed: %d", len(failed))
    for r in results:
        logger.info("  %s", r["email"])

    # Save results
    with open("/tmp/email_accounts.json", "w") as f:
        json.dump(results, f, indent=2)
    logger.info("Results saved to /tmp/email_accounts.json")


if __name__ == "__main__":
    main()
