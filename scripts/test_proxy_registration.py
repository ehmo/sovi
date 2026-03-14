#!/usr/bin/env python3
"""Test mail.com registration through a SOCKS5 proxy.

Tries multiple free proxy approaches:
1. SSH dynamic port forwarding (if available)
2. Free proxy lists

Run: cd ~/Work/ai/sovi && .venv/bin/python scripts/test_proxy_registration.py
"""
import json
import logging
import random
import string
import subprocess
import sys
import time

sys.path.insert(0, "src")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("mailcom")


def check_ip_via_proxy(proxy_url):
    """Check what IP we get through a proxy using httpx."""
    import httpx
    try:
        resp = httpx.get("https://api.ipify.org?format=json", proxy=proxy_url, timeout=10)
        return resp.json().get("ip")
    except Exception as e:
        return f"ERROR: {e}"


def test_with_playwright_proxy(proxy_server):
    """Test full registration flow through proxy."""
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth
    from sovi.persona.email_playwright import _solve_captchafox

    first = random.choice(["Emma", "Sophia", "Olivia", "Ava", "Isabella"])
    last = random.choice(["Miller", "Davis", "Wilson", "Moore", "Taylor"])

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            proxy={"server": proxy_server},
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="en-US",
        )
        Stealth().apply_stealth_sync(context)
        page = context.new_page()

        # Check IP
        try:
            page.goto("https://api.ipify.org?format=json", wait_until="load", timeout=15000)
            ip = page.evaluate("document.body.innerText")
            logger.info("Proxy IP: %s", ip)
        except Exception as e:
            logger.error("Proxy connection failed: %s", e)
            browser.close()
            return None

        # Navigate to signup
        try:
            page.goto("https://signup.mail.com/", wait_until="load", timeout=60000)
            time.sleep(5)
        except Exception as e:
            logger.error("Signup page load failed: %s", e)
            browser.close()
            return None

        # Check for blocking
        title = page.evaluate("document.title")
        if "sorry" in title.lower() or "reject" in title.lower():
            logger.error("Proxy IP blocked by mail.com: %s", title[:80])
            browser.close()
            return None

        has_form = page.evaluate('!!document.querySelector("#given-name")')
        if not has_form:
            logger.error("No signup form found. Title: %s", title[:80])
            browser.close()
            return None

        logger.info("Signup form loaded, filling...")

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
        time.sleep(8)

        chosen = page.evaluate("""(() => {
            var row = document.querySelector('onereg-suggestion-item-advanced');
            if (row) { var t = row.querySelector('.onereg-suggestion-item-advanced__text'); row.click(); return t ? t.textContent : null; }
            return null;
        })()""")
        logger.info("Email: %s", chosen)
        if not chosen:
            browser.close()
            return None
        time.sleep(5)

        page.evaluate("""(() => {
            var radios = document.querySelectorAll('input[name=salutation]');
            if (radios.length > 0) { radios[0].checked = true; radios[0].dispatchEvent(new Event('change', {bubbles: true})); }
            var country = document.querySelector('#country');
            if (country) { country.value = 'DE'; country.dispatchEvent(new Event('change', {bubbles: true})); }
        })()""")
        time.sleep(2)
        # DE doesn't have regions, skip region selection
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
        logger.info("Config extracted")

        # Solve captcha
        cb = page.query_selector('div[role="checkbox"]')
        if not cb:
            logger.error("No captcha checkbox")
            browser.close()
            return None
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
            return None
        time.sleep(2)

        captcha_token = page.evaluate("window.captchafox?.getResponse() || ''")
        logger.info("Captcha token: %s...", captcha_token[:40])

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

        # Call API
        api_result = page.evaluate("""(args) => {
            var storeData = args[0];
            var config = args[1];
            var captchaToken = args[2];

            function uuid() {
                return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
                    var r = Math.random() * 16 | 0;
                    return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
                });
            }

            var headers = {
                'Content-Type': 'application/vnd.ui.mam.account.creation+json',
                'X-UI-APP': config.appIdentifier || '',
                'X-CCGUID': config.clientCredentialGuid || '',
                'X-REQUEST-ID': uuid(),
                'Template-Name': config.templateName || '',
                'Authorization': config.authToken || '',
                'cf-captcha-response': captchaToken
            };

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
        }""", [store_data, svc_config, captcha_token])

        logger.info("API result: status=%s", api_result.get("status"))
        logger.info("API body: %s", api_result.get("body", "")[:200])

        if api_result.get("status") == 204:
            logger.info("=== SUCCESS! Email: %s, Password: %s ===", store_data.get("emailAddress"), pw)
            # Login
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
            }""", [store_data["emailAddress"], pw])
            time.sleep(15)
            activate = page.query_selector("#continueButton")
            if activate:
                activate.click()
                time.sleep(15)
            return {"email": store_data["emailAddress"], "password": pw}

        logger.error("Registration failed: %s", api_result)
        browser.close()
        return None


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy", help="SOCKS5 proxy URL (e.g. socks5://host:port)")
    parser.add_argument("--ssh-host", help="SSH host for dynamic port forwarding")
    args = parser.parse_args()

    proxy_url = args.proxy

    if args.ssh_host and not proxy_url:
        # Set up SSH dynamic port forwarding
        port = random.randint(10000, 20000)
        logger.info("Setting up SSH SOCKS proxy on port %d via %s...", port, args.ssh_host)
        proc = subprocess.Popen(
            ["ssh", "-D", str(port), "-N", "-f", args.ssh_host],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        time.sleep(3)
        proxy_url = f"socks5://127.0.0.1:{port}"
        logger.info("SSH proxy ready: %s", proxy_url)

    if not proxy_url:
        logger.error("No proxy specified. Use --proxy or --ssh-host")
        sys.exit(1)

    # Test proxy
    ip = check_ip_via_proxy(proxy_url)
    logger.info("Proxy IP check: %s", ip)

    result = test_with_playwright_proxy(proxy_url)
    if result:
        logger.info("=== ACCOUNT CREATED ===")
        logger.info("Email: %s", result["email"])
        logger.info("Password: %s", result["password"])
    else:
        logger.error("Registration failed")


if __name__ == "__main__":
    main()
