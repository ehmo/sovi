#!/usr/bin/env python3
"""Monitor mail.com registration API health. Retries every 5 minutes.

Run on studio: .venv/bin/python scripts/monitor_mailcom.py
Exit when API responds with non-502.
"""
import json
import logging
import sys
import time

sys.path.insert(0, "src")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s", datefmt="%H:%M:%S")

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from sovi.persona.email_playwright import _solve_captchafox

CHECK_INTERVAL = 300  # 5 minutes

def check_api():
    """Run one signup attempt and check API response. Returns status code."""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            locale="en-US",
        )
        Stealth().apply_stealth_sync(ctx)
        page = ctx.new_page()

        try:
            page.goto("https://signup.mail.com/", wait_until="load", timeout=30000)
            time.sleep(3)
            page.fill("#given-name", "Monitor")
            page.fill("#family-name", "Check")
            page.fill("#bday-month", "06")
            page.fill("#bday-day", "15")
            page.fill("#bday-year", "1992")
            page.evaluate("document.querySelectorAll('button[type=button]')[0].click()")
            time.sleep(5)
            page.evaluate("""(() => {
                var row = document.querySelector('onereg-suggestion-item-advanced');
                if (row) row.click();
            })()""")
            time.sleep(3)
            page.evaluate("""(() => {
                var r = document.querySelectorAll('input[name=salutation]');
                if (r.length > 0) { r[0].checked = true; r[0].dispatchEvent(new Event('change', {bubbles: true})); }
                var c = document.querySelector('#country');
                if (c) { c.value = 'US'; c.dispatchEvent(new Event('change', {bubbles: true})); }
            })()""")
            time.sleep(1)
            page.evaluate("""(() => {
                var r = document.querySelector('#region');
                if (r && r.options.length > 1) { r.value = r.options[1].value; r.dispatchEvent(new Event('change', {bubbles: true})); }
            })()""")
            time.sleep(1)
            page.evaluate("document.querySelector('[data-test=progress-meter-next]').click()")
            time.sleep(5)
            page.fill("#password", "PwMonitor12!3")
            page.fill("#confirm-password", "PwMonitor12!3")
            page.evaluate("document.querySelector('[data-test=progress-meter-next]').click()")
            time.sleep(5)
            page.evaluate("document.querySelector('[data-test=progress-meter-next]').click()")
            time.sleep(5)

            # Solve captcha
            cb = page.query_selector('div[role="checkbox"]')
            if cb:
                box = cb.bounding_box()
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                time.sleep(3)
                _solve_captchafox(page)
                time.sleep(2)

            # Call createAccount with correct body format
            result = page.evaluate("""(() => {
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
                            state.paymentGroup, state.passwordRecoveryGroup
                        );
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

                    var captchaData = { userSolution: captchaToken, token: '' };
                    window.__createResult = 'PENDING';
                    svc.createAccount({ accountData: accountData, captchaData: captchaData }).subscribe({
                        next: function(r) { window.__createResult = 'OK:204'; },
                        error: function(e) { window.__createResult = 'ERR:' + (e.status||'?'); },
                        complete: function() { if (window.__createResult === 'PENDING') window.__createResult = 'COMPLETE'; }
                    });
                    return 'subscribed';
                } catch(e) { return 'Error: ' + e.message; }
            })()""")

            time.sleep(10)
            final = page.evaluate("window.__createResult")
            logging.info("API result: %s", final)

            if "ERR:502" in str(final):
                return 502
            elif "ERR:403" in str(final):
                return 403
            elif "OK:204" in str(final):
                return 204
            elif "COMPLETE" in str(final):
                return 200
            else:
                return -1

        except Exception as e:
            logging.error("Check failed: %s", e)
            return -1
        finally:
            browser.close()


if __name__ == "__main__":
    logging.info("Starting mail.com API monitor (checking every %ds)", CHECK_INTERVAL)
    while True:
        status = check_api()
        logging.info("API status: %d", status)
        if status != 502:
            logging.info("API is BACK! Status: %d", status)
            # Write a marker file
            with open("/tmp/mailcom_api_back.txt", "w") as f:
                f.write(f"status={status}\ntime={time.time()}\n")
            if status == 204 or status == 200:
                logging.info("Registration SUCCESS!")
            break
        logging.info("Still 502, sleeping %ds...", CHECK_INTERVAL)
        time.sleep(CHECK_INTERVAL)
