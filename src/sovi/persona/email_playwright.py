"""Email account creation via Playwright (headless browser on Mac).

Alternative to WDA-based on-device creation. Uses headless Chromium
to create Outlook/Mail.com accounts directly from the Mac Studio.
Stores credentials encrypted in email_accounts table.
"""

from __future__ import annotations

import logging
import random
import string
import time

from sovi.crypto import encrypt
from sovi.db import sync_execute_one

logger = logging.getLogger(__name__)

# Provider configs
PROVIDERS = {
    "outlook": {
        "signup_url": "https://signup.live.com/signup",
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
        "domains": ["outlook.com", "hotmail.com"],
    },
    "mailcom": {
        "signup_url": "https://www.mail.com/int/",
        "imap_host": "imap.mail.com",
        "imap_port": 993,
        "domains": [
            "mail.com", "email.com", "usa.com", "post.com",
            "engineer.com", "consultant.com", "myself.com",
        ],
    },
}


def _generate_password() -> str:
    """Generate a strong random password."""
    chars = string.ascii_letters + string.digits + "!@#$%"
    pw = [
        random.choice(string.ascii_uppercase),
        random.choice(string.ascii_lowercase),
        random.choice(string.digits),
        random.choice("!@#$%"),
    ]
    pw.extend(random.choices(chars, k=12))
    random.shuffle(pw)
    return "".join(pw)


def _derive_email_username(persona: dict) -> str:
    """Derive an email username from persona data."""
    first = persona["first_name"].lower().replace(" ", "")
    last = persona["last_name"].lower().replace(" ", "")
    age = persona.get("age", 28)
    birth_year = str(2026 - age)[-2:]
    variants = [
        f"{first}.{last}",
        f"{first}{last}{birth_year}",
        f"{first}.{last}{birth_year}",
        f"{first}_{last}",
        f"{first}{last[0]}{birth_year}",
    ]
    return random.choice(variants)


def _human_type(page, selector: str, text: str) -> None:
    """Type text with human-like delays."""
    page.click(selector)
    time.sleep(random.uniform(0.2, 0.5))
    for char in text:
        page.keyboard.type(char, delay=random.randint(30, 120))


def create_email_playwright(
    persona: dict,
    provider: str = "outlook",
) -> dict | None:
    """Create an email account using headless Playwright browser.

    Returns email_account dict on success, None on failure.
    """
    from playwright.sync_api import sync_playwright

    if provider not in PROVIDERS:
        logger.error("Unknown provider: %s", provider)
        return None

    config = PROVIDERS[provider]
    persona_id = str(persona["id"])
    username = _derive_email_username(persona)
    domain = random.choice(config["domains"])
    email_address = f"{username}@{domain}"
    password = _generate_password()

    logger.info("Creating %s email for %s: %s", provider, persona.get("display_name"), email_address)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="en-US",
            )
            page = context.new_page()

            if provider == "outlook":
                success = _signup_outlook_pw(page, persona, username, domain, password)
            elif provider == "mailcom":
                success = _signup_mailcom_pw(page, persona, username, domain, password)
            else:
                success = False

            browser.close()

        if not success:
            logger.warning("Signup failed for %s", email_address)
            return None

        # Store in DB
        row = sync_execute_one(
            """INSERT INTO email_accounts
               (persona_id, provider, email, password, imap_host, imap_port, domain, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'available')
               RETURNING id, provider, domain, status""",
            (
                persona_id, provider,
                encrypt(email_address), encrypt(password),
                config["imap_host"], config["imap_port"],
                domain,
            ),
        )

        if row:
            logger.info("Email created and stored: %s (id=%s)", email_address, row["id"])
            return {**dict(row), "email_plain": email_address}

        return None

    except Exception:
        logger.error("Playwright email creation failed for %s", persona.get("display_name"), exc_info=True)
        return None


def _select_custom_dropdown(page, button_id: str, value: str) -> None:
    """Click a custom MS dropdown button and select an option by text."""
    # Use force=True to bypass label interception
    page.click(f"#{button_id}", force=True)
    time.sleep(0.5)
    # The dropdown renders a list — click the matching option
    option = page.query_selector(f'div[role="option"]:has-text("{value}"), li[role="option"]:has-text("{value}")')
    if option:
        option.click()
    else:
        # Fallback: try clicking by exact text match in any visible element
        page.click(f'text="{value}"')
    time.sleep(0.3)


def _signup_outlook_pw(page, persona: dict, username: str, domain: str, password: str) -> bool:
    """Outlook signup flow via Playwright (2026 MS signup UI)."""
    month_names = [
        "", "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ]

    try:
        page.goto("https://signup.live.com/signup", wait_until="load", timeout=30000)
        time.sleep(3)

        # Step 1: Enter username (page has separate username + domain fields)
        email_sel = 'input[name="Email"], input[type="email"]'
        page.wait_for_selector(email_sel, timeout=15000)
        # Use highly unique username with random suffix
        unique_username = f"{username}{random.randint(1000, 9999)}"
        full_email = f"{unique_username}@{domain}"
        _human_type(page, email_sel, full_email)
        time.sleep(0.5)

        page.click('button[type="submit"]')
        time.sleep(5)
        page.screenshot(path="/tmp/outlook_step1.png")

        # Check if username was taken — try suggestions or retry
        for attempt in range(3):
            page_text = page.inner_text("body")
            if "already taken" in page_text.lower():
                # Try clicking a suggested alternative
                suggestions = page.query_selector_all('button[data-testid*="suggestion"], a[class*="suggestion"]')
                if suggestions:
                    suggestions[0].click()
                    time.sleep(1)
                    page.click('button[type="submit"]')
                    time.sleep(5)
                else:
                    # Clear and try with different suffix
                    email_input = page.query_selector(email_sel)
                    if email_input:
                        email_input.fill("")
                    unique_username = f"{username}{random.randint(10000, 99999)}"
                    full_email = f"{unique_username}@{domain}"
                    _human_type(page, email_sel, full_email)
                    time.sleep(0.5)
                    page.click('button[type="submit"]')
                    time.sleep(5)
            else:
                break

        # Step 2: Password
        try:
            pw_sel = 'input[name="Password"], input[type="password"]'
            page.wait_for_selector(pw_sel, timeout=10000)
            _human_type(page, pw_sel, password)
            time.sleep(0.5)
            page.click('button[type="submit"]')
            time.sleep(4)
            page.screenshot(path="/tmp/outlook_step2.png")
        except Exception:
            logger.warning("Password step failed")
            page.screenshot(path="/tmp/outlook_pw_fail.png")
            return False

        # Step 3: Country/Region + DOB (no name step in 2026 flow)
        try:
            # Wait for BirthMonth dropdown button
            page.wait_for_selector('#BirthMonthDropdown', timeout=10000)

            dob = persona.get("date_of_birth", "1995-06-15")
            parts = dob.split("-") if isinstance(dob, str) else ["1995", "06", "15"]
            month_idx = int(parts[1])
            day = int(parts[2])
            year = parts[0]

            # Select month via custom dropdown
            _select_custom_dropdown(page, "BirthMonthDropdown", month_names[month_idx])
            time.sleep(0.3)

            # Select day via custom dropdown
            _select_custom_dropdown(page, "BirthDayDropdown", str(day))
            time.sleep(0.3)

            # Type year in input
            year_sel = 'input[name="BirthYear"]'
            page.click(year_sel)
            page.fill(year_sel, year)
            time.sleep(0.5)

            page.click('button[type="submit"]')
            time.sleep(4)
            page.screenshot(path="/tmp/outlook_step3.png")
        except Exception as e:
            logger.warning("DOB step failed: %s", e)
            page.screenshot(path="/tmp/outlook_dob_fail.png")

        # Step 4: Name (comes AFTER DOB in 2026 MS flow)
        try:
            first_sel = 'input[name="firstNameInput"], input[name="FirstName"], input#firstNameInput'
            page.wait_for_selector(first_sel, timeout=10000)
            _human_type(page, first_sel, persona["first_name"])
            time.sleep(0.3)

            last_sel = 'input[name="lastNameInput"], input[name="LastName"], input#lastNameInput'
            _human_type(page, last_sel, persona["last_name"])
            time.sleep(0.5)
            page.click('button[type="submit"]')
            time.sleep(5)
            page.screenshot(path="/tmp/outlook_step4.png")
        except Exception as e:
            logger.warning("Name step failed: %s", e)
            page.screenshot(path="/tmp/outlook_name_fail.png")

        # Step 5: Handle CAPTCHA — "Press and hold" challenge
        page.screenshot(path="/tmp/outlook_step5_pre.png")
        page_text = page.inner_text("body")

        if "prove you're human" in page_text.lower() or "press and hold" in page_text.lower():
            logger.info("Attempting press-and-hold CAPTCHA...")
            try:
                # The CAPTCHA is inside an hsprotect iframe, rendered as a canvas
                captcha_frame = None
                for frame in page.frames:
                    if "hsprotect" in frame.url:
                        captcha_frame = frame
                        break

                if captcha_frame:
                    # Find the iframe element in the parent page to get its position
                    iframe_el = page.query_selector('iframe[src*="hsprotect"]')
                    if iframe_el:
                        iframe_box = iframe_el.bounding_box()
                        if iframe_box:
                            # The press-and-hold button is roughly centered in the iframe
                            # Click at the center-bottom area of the iframe
                            cx = iframe_box["x"] + iframe_box["width"] / 2
                            cy = iframe_box["y"] + iframe_box["height"] * 0.7
                            hold_duration = random.uniform(10, 14)
                            logger.info("Pressing at (%d, %d) for %.1fs", cx, cy, hold_duration)
                            page.mouse.move(cx, cy)
                            time.sleep(random.uniform(0.3, 0.6))
                            page.mouse.down()
                            time.sleep(hold_duration)
                            page.mouse.up()
                            time.sleep(8)
                            page.screenshot(path="/tmp/outlook_captcha_result.png")
                            logger.info("Press-and-hold completed")
                        else:
                            logger.warning("iframe has no bounding box")
                    else:
                        logger.warning("hsprotect iframe element not found in parent page")
                else:
                    logger.warning("No hsprotect iframe found")
            except Exception as e:
                logger.warning("CAPTCHA handling failed: %s", e)

        # Re-check page state after CAPTCHA attempt
        page.screenshot(path="/tmp/outlook_final.png")
        url = page.url
        page_text = page.inner_text("body")

        # Still on challenge
        if "prove you're human" in page_text.lower():
            logger.warning("CAPTCHA not solved at %s", url)
            return False

        if "challenge" in url or "captcha" in url.lower():
            logger.warning("Hit secondary CAPTCHA at %s", url)
            return False

        if "proofs" in url or "verify" in url:
            logger.warning("Hit phone verification at %s", url)
            return False

        # If we see "Your account has been created" or similar success indicators
        if "account" in page_text.lower() and ("created" in page_text.lower() or "welcome" in page_text.lower()):
            logger.info("Outlook signup succeeded for %s", full_email)
            return True

        # If we're on outlook.com or a mail page, success
        if "outlook.live.com" in url or "outlook.com" in url:
            logger.info("Outlook signup succeeded (redirected to inbox) for %s", full_email)
            return True

        # If still on signup page, the flow didn't complete
        if "signup.live.com" in url:
            logger.warning("Signup incomplete — still on signup page: %s", url)
            return False

        logger.info("Outlook signup flow completed for %s (url: %s)", full_email, url)
        return True

    except Exception:
        logger.error("Outlook signup error", exc_info=True)
        try:
            page.screenshot(path="/tmp/outlook_error.png")
        except Exception:
            pass
        return False


def _signup_mailcom_pw(page, persona: dict, username: str, domain: str, password: str) -> bool:
    """Mail.com signup flow via Playwright."""
    try:
        page.goto("https://www.mail.com/int/", wait_until="load", timeout=30000)
        time.sleep(2)

        # Click Sign up / Free email
        try:
            signup_btn = page.query_selector('a[href*="signup"], a:has-text("Sign up"), a:has-text("Free email"), button:has-text("Sign up")')
            if signup_btn:
                signup_btn.click()
                time.sleep(3)
        except Exception:
            # Try direct signup URL
            page.goto("https://signup.mail.com/", wait_until="load", timeout=30000)
            time.sleep(2)

        # Enter desired email
        email_input = page.query_selector('input[name="emailAddress"], input[name="localPart"], input[placeholder*="email"]')
        if email_input:
            email_input.click()
            time.sleep(0.3)
            _human_type(page, f'#{email_input.get_attribute("id") or "emailAddress"}', username)
            time.sleep(1)

        # Click check / next
        try:
            page.click('button:has-text("Check"), button:has-text("Next"), button[type="submit"]')
            time.sleep(3)
        except Exception:
            pass

        # Fill personal info
        for field_name, value in [
            ("firstName", persona["first_name"]),
            ("lastName", persona["last_name"]),
        ]:
            try:
                field = page.query_selector(f'input[name="{field_name}"]')
                if field:
                    field.click()
                    time.sleep(0.2)
                    field.fill(value)
                    time.sleep(0.3)
            except Exception:
                pass

        # Password
        pw_field = page.query_selector('input[name="password"], input[type="password"]')
        if pw_field:
            pw_field.click()
            time.sleep(0.2)
            pw_field.fill(password)
            time.sleep(0.3)

        # DOB
        dob = persona.get("date_of_birth", "1995-06-15")
        if isinstance(dob, str):
            parts = dob.split("-")
            for sel, val in [
                ('select[name="birthMonth"]', parts[1].lstrip("0")),
                ('select[name="birthDay"]', parts[2].lstrip("0")),
                ('input[name="birthYear"]', parts[0]),
            ]:
                try:
                    el = page.query_selector(sel)
                    if el:
                        if sel.startswith("select"):
                            page.select_option(sel, val)
                        else:
                            el.fill(val)
                        time.sleep(0.2)
                except Exception:
                    pass

        # Gender
        gender = persona.get("gender", "female")
        try:
            gender_radio = page.query_selector(f'input[value="{gender}"], input[value="{gender[0].upper()}"]')
            if gender_radio:
                gender_radio.click()
                time.sleep(0.2)
        except Exception:
            pass

        # Submit
        try:
            page.click('button:has-text("Create"), button:has-text("Register"), button[type="submit"]')
            time.sleep(5)
        except Exception:
            pass

        page.screenshot(path="/tmp/mailcom_signup_result.png")

        url = page.url
        if "captcha" in url.lower() or "verify" in url.lower():
            logger.warning("Hit CAPTCHA/verification at %s", url)
            return False

        logger.info("Mail.com signup flow completed for %s@%s", username, domain)
        return True

    except Exception:
        logger.error("Mail.com signup error", exc_info=True)
        page.screenshot(path="/tmp/mailcom_signup_error.png")
        return False


def create_emails_batch(
    personas: list[dict],
    provider: str = "outlook",
    max_per_run: int = 10,
) -> list[dict]:
    """Create email accounts for a batch of personas.

    Returns list of successfully created email account dicts.
    """
    results = []
    for i, persona in enumerate(personas[:max_per_run]):
        logger.info("Creating email %d/%d for %s", i + 1, min(len(personas), max_per_run), persona.get("display_name"))
        result = create_email_playwright(persona, provider)
        if result:
            results.append(result)
        # Delay between signups to avoid rate limiting
        time.sleep(random.uniform(5, 15))

    logger.info("Created %d/%d emails", len(results), min(len(personas), max_per_run))
    return results
