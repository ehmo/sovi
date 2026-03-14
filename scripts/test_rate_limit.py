#!/usr/bin/env python3
"""Quick test: is mail.com rate limit still active?

Run: cd ~/Work/ai/sovi && .venv/bin/python scripts/test_rate_limit.py
"""
import sys
import time

sys.path.insert(0, "src")

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )
    ctx = browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        locale="en-US",
    )
    Stealth().apply_stealth_sync(ctx)
    page = ctx.new_page()
    page.goto("https://signup.mail.com/", wait_until="load", timeout=30000)
    time.sleep(3)

    page.fill("#given-name", "Test")
    page.fill("#family-name", "Account")
    page.fill("#bday-month", "06")
    page.fill("#bday-day", "15")
    page.fill("#bday-year", "1992")
    page.evaluate("document.querySelectorAll('button[type=button]')[0].click()")
    time.sleep(5)

    chosen = page.evaluate("""(() => {
        var row = document.querySelector('onereg-suggestion-item-advanced');
        if (row) { var t = row.querySelector('.onereg-suggestion-item-advanced__text'); row.click(); return t ? t.textContent : null; }
        return null;
    })()""")
    print(f"Email suggestion: {chosen}")

    if not chosen:
        print("BLOCKED: No email suggestions shown")
        browser.close()
        sys.exit(1)

    # Navigate to captcha step
    page.evaluate("""(() => {
        var radios = document.querySelectorAll('input[name=salutation]');
        if (radios.length > 0) { radios[0].checked = true; radios[0].dispatchEvent(new Event('change', {bubbles: true})); }
        var country = document.querySelector('#country');
        if (country) { country.value = 'US'; country.dispatchEvent(new Event('change', {bubbles: true})); }
    })()""")
    time.sleep(1)
    page.evaluate("""(() => {
        var r = document.querySelector('#region');
        if (r && r.options.length > 1) { r.value = r.options[1].value; r.dispatchEvent(new Event('change', {bubbles: true})); }
    })()""")
    time.sleep(1)
    page.evaluate("document.querySelector('[data-test=progress-meter-next]').click()")
    time.sleep(3)
    page.fill("#password", "TestAcc0unt!7")
    page.fill("#confirm-password", "TestAcc0unt!7")
    page.evaluate("document.querySelector('[data-test=progress-meter-next]').click()")
    time.sleep(3)
    page.evaluate("document.querySelector('[data-test=progress-meter-next]').click()")
    time.sleep(3)

    # Extract service config
    config = page.evaluate("""(() => {
        try {
            var t = getAllAngularTestabilities()[0];
            var injector = t._destroyRef;
            var config = {};
            injector.records.forEach(function(v, k) {
                if (v && v.value && typeof v.value.createAccount === 'function') {
                    config.accountRestUrl = v.value.accountRestUrl;
                    config.authToken = v.value.authToken?.substring(0, 40);
                }
            });
            return config;
        } catch(e) { return {"error": e.message}; }
    })()""")
    print(f"Config: {config}")

    # Test API with empty body — 400 = reachable, 403 = rate limited
    url = config.get("accountRestUrl", "https://signup.mail.com/account")
    test_result = page.evaluate("""(url) => {
        return fetch(url + '/email-registration', {
            method: 'POST',
            headers: {'Content-Type': 'application/vnd.ui.mam.account.creation+json'},
            body: '{}',
            credentials: 'same-origin'
        }).then(function(r) {
            return r.text().then(function(t) {
                return {status: r.status, body: t.substring(0, 200)};
            });
        }).catch(function(e) { return {error: e.message}; });
    }""", url)
    print(f"API test: {test_result}")

    status = test_result.get("status")
    if status == 403:
        print("RATE LIMITED — still blocked")
    elif status == 400:
        print("NOT RATE LIMITED — API reachable (400 = bad request, expected with empty body)")
    elif status == 401:
        print("NOT RATE LIMITED — API reachable (401 = auth needed)")
    else:
        print(f"Status {status} — unclear")

    browser.close()
