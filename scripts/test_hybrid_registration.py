#!/usr/bin/env python3
"""Hybrid registration: browser on studio IP, API call through proxy.

1. Browser navigates signup (studio IP - fast, reliable captcha)
2. Extract service config, store data, captcha token
3. Make registration API call from Python via proxy (VPS IP - not rate limited)
4. Login back in browser

Run: cd ~/Work/ai/sovi && .venv/bin/python scripts/test_hybrid_registration.py --proxy socks5://192.168.5.216:18080
"""
import json
import logging
import random
import string
import sys
import time
import uuid

sys.path.insert(0, "src")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("mailcom")

import httpx
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from sovi.persona.email_playwright import _solve_captchafox


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy", required=True, help="SOCKS5 proxy URL")
    args = parser.parse_args()

    first = random.choice(["Emma", "Sophia", "Olivia", "Ava", "Isabella"])
    last = random.choice(["Miller", "Davis", "Wilson", "Moore", "Taylor"])

    with sync_playwright() as p:
        # Browser runs WITHOUT proxy (studio IP, fast)
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

        page.goto("https://signup.mail.com/", wait_until="load", timeout=30000)
        time.sleep(5)

        page.fill("#given-name", first)
        time.sleep(0.2)
        page.fill("#family-name", last)
        time.sleep(0.2)
        bm = f"{random.randint(1,12):02d}"
        bd = f"{random.randint(1,28):02d}"
        by = str(random.randint(1985, 1998))
        page.fill("#bday-month", bm)
        page.fill("#bday-day", bd)
        page.fill("#bday-year", by)
        page.evaluate("document.querySelectorAll('button[type=button]')[0].click()")
        time.sleep(5)

        chosen = page.evaluate("""(() => {
            var row = document.querySelector('onereg-suggestion-item-advanced');
            if (row) { var t = row.querySelector('.onereg-suggestion-item-advanced__text'); row.click(); return t ? t.textContent : null; }
            return null;
        })()""")
        logger.info("Email: %s", chosen)
        if not chosen:
            browser.close()
            return
        chosen = chosen.strip()
        time.sleep(5)

        page.evaluate("""(() => {
            var radios = document.querySelectorAll('input[name=salutation]');
            if (radios.length > 0) { radios[0].checked = true; radios[0].dispatchEvent(new Event('change', {bubbles: true})); }
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
        time.sleep(5)

        pw = "Pw" + "".join(random.choices(string.ascii_letters + string.digits, k=10)) + "!3"
        page.fill("#password", pw)
        time.sleep(0.3)
        page.fill("#confirm-password", pw)
        time.sleep(0.3)
        page.evaluate("document.querySelector('[data-test=progress-meter-next]').click()")
        time.sleep(5)
        logger.info("Password: %s", pw)
        page.evaluate("document.querySelector('[data-test=progress-meter-next]').click()")
        time.sleep(5)

        # Extract service config
        svc_config = page.evaluate("""(() => {
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
        })()""")
        logger.info("Config: accountRestUrl=%s", svc_config.get("accountRestUrl"))

        # Solve captcha (on studio IP - fast and reliable)
        cb = page.query_selector('div[role="checkbox"]')
        if not cb:
            logger.error("No captcha checkbox")
            browser.close()
            return
        box = cb.bounding_box()
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        time.sleep(3)

        try:
            solved = _solve_captchafox(page)
        except Exception as e:
            logger.info("Captcha error: %s", str(e)[:100])
            solved = page.evaluate("document.querySelector('div[role=checkbox]')?.getAttribute('aria-checked')") == "true"

        if not solved:
            logger.error("Captcha not solved")
            browser.close()
            return
        time.sleep(2)

        captcha_token = page.evaluate("window.captchafox?.getResponse() || ''")
        logger.info("Captcha token: %s...", captcha_token[:40])

        # Extract store data
        store_data = page.evaluate("""(() => {
            var t = getAllAngularTestabilities()[0];
            var store = null;
            t._destroyRef.records.forEach(function(v, k) {
                if (v && v.value && typeof v.value.dispatch === 'function') store = v.value;
            });
            var src = store.source;
            while (src && !src._value && src.source) src = src.source;
            var state = src._value;
            return {
                emailAddress: state.aliasCheck?.selectedMailAddress,
                password: state.passwordGroup?.password?.value,
                firstName: state.personalInfoGroup?.firstName?.value,
                lastName: state.personalInfoGroup?.lastName?.value,
                dateOfBirth: state.personalInfoGroup?.dateOfBirth?.value,
                salutation: state.personalInfoGroup?.salutation?.value,
                country: state.personalInfoGroup?.address?.country?.value,
                region: state.personalInfoGroup?.address?.region?.value
            };
        })()""")
        logger.info("Store data: %s", json.dumps(store_data)[:200])

        # Extract cookies from browser for the API call
        cookies = context.cookies()
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies if "mail.com" in c.get("domain", ""))
        logger.info("Cookies: %d mail.com cookies", len([c for c in cookies if "mail.com" in c.get("domain", "")]))

        # === API CALL VIA PYTHON + PROXY (not through browser) ===
        logger.info("\n=== Calling API via proxy ===")

        api_url = svc_config["accountRestUrl"] + "/email-registration"
        headers = {
            "Content-Type": "application/vnd.ui.mam.account.creation+json",
            "X-UI-APP": svc_config.get("appIdentifier", ""),
            "X-CCGUID": svc_config.get("clientCredentialGuid", ""),
            "X-REQUEST-ID": str(uuid.uuid4()),
            "Template-Name": svc_config.get("templateName", ""),
            "Authorization": svc_config.get("authToken", ""),
            "cf-captcha-response": captcha_token,
            "Origin": "https://signup.mail.com",
            "Referer": "https://signup.mail.com/",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }
        if svc_config.get("referrerSource"):
            headers["Source"] = svc_config["referrerSource"]
        if cookie_str:
            headers["Cookie"] = cookie_str

        # Clean store data (remove undefined/null)
        body = {k: v for k, v in store_data.items() if v is not None}

        logger.info("API URL: %s", api_url)
        logger.info("Headers: %s", {k: v[:60] if isinstance(v, str) else v for k, v in headers.items()})
        logger.info("Body: %s", json.dumps(body)[:300])

        try:
            resp = httpx.post(
                api_url,
                headers=headers,
                json=body,
                proxy=args.proxy,
                timeout=30.0,
            )
            logger.info("API response: status=%d", resp.status_code)
            logger.info("API body: %s", resp.text[:300])

            if resp.status_code == 204:
                logger.info("=== REGISTRATION SUCCESS (204)! ===")
                logger.info("Email: %s", chosen)
                logger.info("Password: %s", pw)

                # Login via browser
                time.sleep(3)
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
                }""", [chosen, pw])
                time.sleep(15)

                title = page.evaluate("document.title")
                logger.info("After login: Title=%s, URL=%s", title[:60], page.url[:100])

                activate = page.query_selector("#continueButton")
                if activate:
                    activate.click()
                    time.sleep(15)

            elif resp.status_code == 403:
                logger.error("Rate limited (403)")
            elif resp.status_code == 502:
                logger.error("Server error (502) — may be proxy IP issue")
            else:
                logger.error("Unexpected: %d", resp.status_code)

        except Exception as e:
            logger.error("API call failed: %s", e)

        page.screenshot(path="/tmp/mailcom_hybrid.png")
        browser.close()


if __name__ == "__main__":
    main()
