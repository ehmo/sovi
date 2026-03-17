# Email Creation Pipeline

How to obtain Outlook and Mail.com email accounts at scale using the existing iPhone farm.

## Overview

Email creation runs as a separate pipeline from social account creation. It builds an inventory of email accounts in advance, which the social account creator draws from.

```
                    Email Creation Pipeline
                    ~~~~~~~~~~~~~~~~~~~~~~
Phase 1: Build email inventory (run days/weeks before needed)
Phase 2: Social account creator claims emails from inventory

Pipeline per email:
  cellular-data reset (fresh carrier session / IP)
    -> open Safari
    -> navigate to signup page
    -> fill form (name, username, password)
    -> handle CAPTCHA (CapSolver)
    -> handle phone verification if required (TextVerified)
    -> store credentials in email_accounts table
    -> close Safari, go home
```

## WDA Additions Needed

The current `WDASession` has no Safari/URL navigation. Need to add:

```python
# wda_client.py additions

SAFARI_BUNDLE = "com.apple.mobilesafari"

def navigate_to(self, url: str) -> None:
    """Navigate Safari to a URL via WDA."""
    self.client.post(f"{self._s}/url", json={"url": url})

def get_current_url(self) -> str | None:
    """Get current Safari URL."""
    try:
        resp = self.client.get(f"{self._s}/url")
        return resp.json().get("value")
    except Exception:
        return None

def open_safari(self, url: str) -> None:
    """Launch Safari and navigate to URL."""
    self.launch_app(SAFARI_BUNDLE)
    time.sleep(2)
    self.navigate_to(url)
    time.sleep(3)

def close_safari(self) -> None:
    """Terminate Safari."""
    self.terminate_app(SAFARI_BUNDLE)
```

## Database: email_accounts Table

```sql
CREATE TABLE email_accounts (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    provider        TEXT NOT NULL,               -- 'outlook', 'hotmail', 'mailcom'
    domain          TEXT NOT NULL,               -- 'outlook.com', 'mail.com', 'usa.com', etc.
    email           TEXT NOT NULL,               -- ENCRYPTED
    password        TEXT NOT NULL,               -- ENCRYPTED
    imap_host       TEXT NOT NULL,
    imap_port       INT NOT NULL DEFAULT 993,
    recovery_email  TEXT,                        -- ENCRYPTED, for account recovery
    status          TEXT NOT NULL DEFAULT 'available',
        -- available: ready to be claimed by social account creator
        -- assigned: linked to a social account
        -- disabled: login failed / account locked
        -- creating: creation in progress
    assigned_to     UUID REFERENCES accounts(id) ON DELETE SET NULL,
    phone_used      BOOLEAN NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX idx_email_accounts_email ON email_accounts (email);
CREATE INDEX idx_email_accounts_available ON email_accounts (status)
    WHERE status = 'available';
CREATE INDEX idx_email_accounts_domain ON email_accounts (domain);
```

## Outlook.com Signup Flow

URL: `https://signup.live.com/signup`

### Form Fields (Mobile Safari View)

The Outlook signup is a multi-step form. Each step is a separate page:

```
Step 1: Email address
  - "Get a new email address" link (switches from "use existing")
  - Text field for username
  - Dropdown to pick @outlook.com or @hotmail.com
  - [Next] button

Step 2: Password
  - Secure text field for password
  - [Next] button

Step 3: Name
  - First name text field
  - Last name text field
  - [Next] button

Step 4: Country and DOB
  - Country dropdown (pre-filled from locale)
  - Month/Day/Year dropdowns or fields
  - [Next] button

Step 5: CAPTCHA or Phone Verification
  - EITHER: Image/puzzle CAPTCHA (solve with CapSolver)
  - OR: "We need to verify your identity" -> phone number field
  - If phone: use TextVerified, enter code
  - [Next/Verify] button

Step 6: Done
  - "Your account has been created"
  - May redirect to outlook.com inbox
```

### WDA Automation Sequence

```python
def create_outlook_account(wda, device_id=None):
    """Create an Outlook.com account via Safari on-device."""

    # 0. Generate identity
    first_name = random_first_name()      # from a names list
    last_name = random_last_name()
    domain = random.choice(["outlook.com", "hotmail.com"])
    username = generate_email_username()   # e.g. "sarah.mitchell2847"
    email = f"{username}@{domain}"
    password = generate_strong_password()

    # 1. Cellular-data reset — fresh carrier session / IP
    wda.reset_cellular_data_connection()

    # 2. Open Safari to signup page
    wda.open_safari("https://signup.live.com/signup")
    time.sleep(4)
    auto = DeviceAutomation(wda)
    auto.dismiss_popups(max_attempts=2)

    # 3. Step 1 — Email address
    #    Tap "Get a new email address"
    new_email_link = wda.find_element(
        "predicate string",
        'name CONTAINS "new email" OR name CONTAINS "New email"'
    )
    if new_email_link:
        wda.element_click(new_email_link["ELEMENT"])
        time.sleep(1)

    #    Enter username
    username_field = wda.find_element(
        "predicate string",
        'type == "XCUIElementTypeTextField"'
    )
    if username_field:
        wda.element_click(username_field["ELEMENT"])
        time.sleep(0.3)
        wda.element_value(username_field["ELEMENT"], username)
        time.sleep(0.5)

    #    Select domain (@outlook.com vs @hotmail.com)
    #    May be a dropdown — find and select
    #    If default is already correct, skip
    #    ...

    #    Tap Next
    tap_button(wda, ["Next", "next"])
    time.sleep(3)

    # 4. Step 2 — Password
    pw_field = wda.find_element(
        "predicate string",
        'type == "XCUIElementTypeSecureTextField"'
    )
    if pw_field:
        wda.element_click(pw_field["ELEMENT"])
        time.sleep(0.3)
        wda.element_value(pw_field["ELEMENT"], password)
        time.sleep(0.5)

    tap_button(wda, ["Next", "next"])
    time.sleep(3)

    # 5. Step 3 — Name
    text_fields = wda.find_elements(
        "predicate string",
        'type == "XCUIElementTypeTextField"'
    )
    if len(text_fields) >= 2:
        wda.element_click(text_fields[0]["ELEMENT"])
        wda.element_value(text_fields[0]["ELEMENT"], first_name)
        time.sleep(0.3)
        wda.element_click(text_fields[1]["ELEMENT"])
        wda.element_value(text_fields[1]["ELEMENT"], last_name)
        time.sleep(0.5)

    tap_button(wda, ["Next", "next"])
    time.sleep(3)

    # 6. Step 4 — Country and DOB
    #    Country is usually pre-filled from device locale (United States)
    #    DOB: find dropdowns/fields and set to random adult date
    #    Similar to TikTok birthday picker in account_creator.py
    set_random_dob(wda)  # helper function

    tap_button(wda, ["Next", "next"])
    time.sleep(5)  # longer wait — this is where CAPTCHA/phone decision happens

    # 7. Step 5 — CAPTCHA or Phone
    phone_required = detect_phone_verification(wda)

    if phone_required:
        # Phone verification path
        sms = request_number("microsoft")  # TextVerified
        if sms:
            phone_field = wda.find_element(
                "predicate string",
                'type == "XCUIElementTypeTextField"'
            )
            if phone_field:
                wda.element_value(phone_field["ELEMENT"], sms.phone_number)
            tap_button(wda, ["Send code", "Next"])
            time.sleep(5)

            code = wait_for_code(sms, timeout=90)
            if code:
                code_field = wda.find_element(
                    "predicate string",
                    'type == "XCUIElementTypeTextField"'
                )
                if code_field:
                    wda.element_value(code_field["ELEMENT"], code)
                tap_button(wda, ["Verify", "Next"])
            else:
                cancel_verification(sms)
                return None  # failed
    else:
        # CAPTCHA path
        screenshot = wda.screenshot()
        if screenshot:
            # Try image CAPTCHA solver
            solution = solve_image(screenshot, platform="outlook", device_id=device_id)
            # Apply solution (varies by CAPTCHA type — may need to tap specific areas)
            if solution:
                apply_captcha_solution(wda, solution)
            tap_button(wda, ["Next", "Verify"])
            time.sleep(5)

    # 8. Verify success
    #    Check if we see inbox or "account created" confirmation
    time.sleep(3)
    current_url = wda.get_current_url()
    success = current_url and ("outlook.live.com" in current_url or "account" in current_url)

    # 9. Close Safari, go home
    wda.close_safari()
    wda.press_button("home")

    if success:
        return {
            "provider": "outlook" if domain == "outlook.com" else "hotmail",
            "domain": domain,
            "email": email,
            "password": password,
            "imap_host": "imap-mail.outlook.com",
            "imap_port": 993,
            "phone_used": phone_required,
        }
    return None
```

### Key Detection: Phone vs CAPTCHA

Microsoft's decision happens at Step 5. Detection approach:

```python
def detect_phone_verification(wda):
    """Check if Microsoft is asking for phone or CAPTCHA."""
    time.sleep(2)

    # Look for phone-related text/fields
    phone_el = wda.find_element(
        "predicate string",
        'name CONTAINS "phone" OR name CONTAINS "Phone" '
        'OR name CONTAINS "verify your identity" '
        'OR name CONTAINS "phone number"'
    )
    if phone_el:
        return True

    # Look for CAPTCHA indicators
    # Microsoft uses FunCaptcha or image challenges
    # If we see neither phone nor CAPTCHA, try screenshot-based detection
    return False
```

### Helper: Tap Button by Label

```python
def tap_button(wda, labels):
    """Try to find and tap a button matching any of the given labels."""
    for label in labels:
        # Try accessibility id first
        el = wda.find_element("accessibility id", label)
        if el:
            wda.element_click(el["ELEMENT"])
            return True
        # Try predicate for buttons
        el = wda.find_element(
            "predicate string",
            f'label == "{label}" AND type == "XCUIElementTypeButton"'
        )
        if el:
            wda.element_click(el["ELEMENT"])
            return True
    return False
```

## Mail.com Signup Flow

URL: `https://www.mail.com/int/` -> "Free sign up"

### Domain Selection

Mail.com offers 20+ domain choices in a dropdown during signup. This is the key diversity advantage. Domains include:

```python
MAILCOM_DOMAINS = [
    "mail.com", "email.com", "usa.com", "post.com",
    "consultant.com", "engineer.com", "dr.com", "myself.com",
    "writeme.com", "cheerful.com", "techie.com", "contractor.net",
    "accountant.com", "europe.com", "asia.com", "iname.com",
    "journalist.com", "musician.org", "activist.com", "sociologist.com",
]
```

### Form Fields (Multi-step)

```
Step 1: Choose email address
  - First name, last name
  - Desired email address text field
  - Domain dropdown (pick from 20+ options)
  - [Continue] button

Step 2: Set password
  - Password field
  - Confirm password field
  - [Continue] button

Step 3: Personal details
  - Date of birth (day/month/year dropdowns)
  - Country dropdown
  - Gender dropdown (optional)
  - [Continue] button

Step 4: Recovery (optional)
  - Mobile phone (often skippable — "Skip" or "Later")
  - OR recovery email
  - [Continue] / [Skip] button

Step 5: CAPTCHA
  - Usually a simple image CAPTCHA
  - [Continue] button

Step 6: Done
  - Account created
  - May show inbox tour — dismiss
```

### WDA Automation Sequence

```python
def create_mailcom_account(wda, target_domain=None, device_id=None):
    """Create a mail.com account via Safari on-device."""

    first_name = random_first_name()
    last_name = random_last_name()
    domain = target_domain or random.choice(MAILCOM_DOMAINS)
    username = generate_email_username()
    email = f"{username}@{domain}"
    password = generate_strong_password()

    # 1. Cellular-data reset
    wda.reset_cellular_data_connection()

    # 2. Open signup page
    wda.open_safari("https://www.mail.com/int/")
    time.sleep(4)

    # Tap "Free sign up" or similar CTA
    auto = DeviceAutomation(wda)
    auto.dismiss_popups(max_attempts=2)

    signup_btn = wda.find_element(
        "predicate string",
        'name CONTAINS "sign up" OR name CONTAINS "Sign up" '
        'OR name CONTAINS "register" OR name CONTAINS "Register"'
    )
    if signup_btn:
        wda.element_click(signup_btn["ELEMENT"])
        time.sleep(3)

    # 3. Step 1 — Name and email address
    text_fields = wda.find_elements(
        "predicate string",
        'type == "XCUIElementTypeTextField"'
    )
    # First name, last name, email address — typically 3 text fields
    if len(text_fields) >= 3:
        wda.element_value(text_fields[0]["ELEMENT"], first_name)
        time.sleep(0.3)
        wda.element_value(text_fields[1]["ELEMENT"], last_name)
        time.sleep(0.3)
        wda.element_click(text_fields[2]["ELEMENT"])
        wda.element_value(text_fields[2]["ELEMENT"], username)
        time.sleep(0.5)

    # Select domain from dropdown
    # The domain picker may be a native select or a custom dropdown
    # Strategy: find the dropdown, tap it, scroll to target domain, tap it
    select_domain_from_dropdown(wda, domain)

    tap_button(wda, ["Continue", "Next"])
    time.sleep(3)

    # 4. Step 2 — Password
    pw_fields = wda.find_elements(
        "predicate string",
        'type == "XCUIElementTypeSecureTextField"'
    )
    if len(pw_fields) >= 1:
        wda.element_value(pw_fields[0]["ELEMENT"], password)
        time.sleep(0.3)
    if len(pw_fields) >= 2:
        wda.element_value(pw_fields[1]["ELEMENT"], password)
        time.sleep(0.3)

    tap_button(wda, ["Continue", "Next"])
    time.sleep(3)

    # 5. Step 3 — Personal details (DOB, country)
    set_random_dob(wda)

    tap_button(wda, ["Continue", "Next"])
    time.sleep(3)

    # 6. Step 4 — Recovery (try to skip)
    tap_button(wda, ["Skip", "Later", "Not now", "Continue"])
    time.sleep(3)

    # 7. Step 5 — CAPTCHA
    screenshot = wda.screenshot()
    if screenshot:
        solution = solve_image(screenshot, platform="mailcom", device_id=device_id)
        if solution:
            apply_captcha_solution(wda, solution)
    tap_button(wda, ["Continue", "Create", "Submit"])
    time.sleep(5)

    # 8. Verify success
    current_url = wda.get_current_url()
    success = current_url and "mail.com" in current_url

    wda.close_safari()
    wda.press_button("home")

    if success:
        return {
            "provider": "mailcom",
            "domain": domain,
            "email": email,
            "password": password,
            "imap_host": "imap.mail.com",
            "imap_port": 993,
            "phone_used": False,
        }
    return None
```

### Domain Dropdown Selection

Mail.com's domain picker is a `<select>` element rendered as a native iOS picker in Safari:

```python
def select_domain_from_dropdown(wda, target_domain):
    """Select a domain from the mail.com dropdown."""
    # Find the dropdown / picker trigger
    # In mobile Safari, <select> elements show as tappable fields
    # that open a native UIPickerView when tapped
    domain_picker = wda.find_element(
        "predicate string",
        'name CONTAINS "domain" OR name CONTAINS "@" '
        'OR type == "XCUIElementTypeOther" AND value CONTAINS ".com"'
    )
    if domain_picker:
        wda.element_click(domain_picker["ELEMENT"])
        time.sleep(1)

    # Once the picker is open, it's a XCUIElementTypePickerWheel
    picker = wda.find_element("class chain", "**/XCUIElementTypePickerWheel")
    if picker:
        wda.client.post(
            f"/session/{wda.session_id}/element/{picker['ELEMENT']}/value",
            json={"value": [target_domain]},
        )
        time.sleep(0.5)

    # Tap Done to close the picker
    tap_button(wda, ["Done", "done"])
    time.sleep(0.5)
```

## Orchestrator: Email Batch Creator

Ties everything together. Runs as a CLI command or scheduled job:

```python
# src/sovi/device/email_creator.py

PROVIDER_WEIGHTS = {
    "outlook": 0.30,
    "hotmail": 0.25,
    "mailcom": 0.25,    # distributed across 10+ domains
    "custom":  0.20,    # catch-all domains, no creation needed
}

def pick_provider_and_domain():
    """Weighted random selection of provider + specific domain."""
    r = random.random()
    if r < 0.30:
        return "outlook", "outlook.com"
    elif r < 0.55:
        return "hotmail", "hotmail.com"
    elif r < 0.80:
        domain = random.choice(MAILCOM_DOMAINS)
        return "mailcom", domain
    else:
        domain = random.choice(CUSTOM_CATCHALL_DOMAINS)
        return "custom", domain

def create_email_batch(wda, count=5, device_id=None):
    """Create a batch of email accounts on one device.

    Recommended: max 5-8 per device per day.
    """
    created = []

    for i in range(count):
        provider, domain = pick_provider_and_domain()

        if provider == "custom":
            # Catch-all domains don't need account creation
            # Just generate an address and store it
            username = generate_email_username()
            result = {
                "provider": "custom",
                "domain": domain,
                "email": f"{username}@{domain}",
                "password": CATCHALL_IMAP_PASSWORD,  # shared inbox password
                "imap_host": "imap.purelymail.com",
                "imap_port": 993,
                "phone_used": False,
            }
        elif provider in ("outlook", "hotmail"):
            result = create_outlook_account(wda, domain=domain, device_id=device_id)
        elif provider == "mailcom":
            result = create_mailcom_account(wda, target_domain=domain, device_id=device_id)
        else:
            continue

        if result:
            # Store in DB (encrypted)
            store_email_account(result)
            created.append(result)
            logger.info("Created email %d/%d: %s@%s", i + 1, count, "***", result["domain"])
        else:
            logger.warning("Failed to create email %d/%d (%s/%s)", i + 1, count, provider, domain)

        # Cooldown between creations (look human)
        if i < count - 1:
            time.sleep(random.uniform(60, 180))  # 1-3 min between accounts

    return created
```

## Scheduling and Capacity

### Daily Limits (Conservative)

| Per Device | Value | Rationale |
|-----------|-------|-----------|
| Outlook accounts/day | 3-4 | Microsoft rate-limits per IP range |
| Mail.com accounts/day | 3-4 | Similar rate limiting |
| Total emails/device/day | 6-8 | Alternating providers |

### Fleet Capacity

| Devices | Emails/Day | Days to 500 | Notes |
|---------|-----------|-------------|-------|
| 2 | 12-16 | 31-42 | Current setup |
| 4 | 24-32 | 16-21 | |
| 8 | 48-64 | 8-11 | Target setup |

### Scheduling vs Warming

Email creation and social account warming compete for device time. Options:

1. **Dedicated time blocks**: 6am-10am = email creation, rest = warming
2. **Dedicated devices**: 2 of 8 devices do email creation, 6 do warming
3. **Batch mode**: Run email creation for 1-2 weeks before starting warming campaigns

Option 3 (batch mode) is best. Build the full email inventory before you need it:

```
Week 1-2:  All 8 devices create emails (6-8/device/day = ~400-500 total)
Week 3+:   Switch to social account creation + warming
           Email creation runs only when inventory drops below threshold
```

## Inventory Management

### Claim Pattern (Used by Social Account Creator)

```sql
-- Claim an available email, preferring diverse domains
SELECT id, email, password, imap_host, imap_port, domain
FROM email_accounts
WHERE status = 'available'
ORDER BY
    -- Prefer domains with fewer assigned accounts (diversity)
    (SELECT COUNT(*) FROM email_accounts e2
     WHERE e2.domain = email_accounts.domain AND e2.status = 'assigned') ASC,
    created_at ASC  -- older emails first (aged = better)
LIMIT 1
FOR UPDATE SKIP LOCKED;

-- Then: UPDATE email_accounts SET status = 'assigned', assigned_to = $account_id
```

### Inventory Health Query

```sql
SELECT
    domain,
    COUNT(*) FILTER (WHERE status = 'available') AS available,
    COUNT(*) FILTER (WHERE status = 'assigned') AS assigned,
    COUNT(*) FILTER (WHERE status = 'disabled') AS disabled,
    COUNT(*) AS total
FROM email_accounts
GROUP BY domain
ORDER BY available DESC;
```

### Auto-Refill Trigger

When available inventory drops below a threshold, the scheduler can switch devices to email creation mode:

```python
MINIMUM_EMAIL_INVENTORY = 20  # always keep 20 emails in reserve

def check_email_inventory():
    """Check if we need to create more emails."""
    row = sync_execute_one(
        "SELECT COUNT(*) as cnt FROM email_accounts WHERE status = 'available'"
    )
    return row["cnt"] if row else 0

# In scheduler loop:
# if check_email_inventory() < MINIMUM_EMAIL_INVENTORY:
#     switch device to email creation mode
```

## Human-Likeness Considerations

### Username Generation for Emails

Don't use the same niche-prefix pattern as social usernames. Email usernames should look like real people:

```python
def generate_email_username():
    """Generate a realistic email username."""
    patterns = [
        # firstname.lastname + digits
        lambda: f"{random_first_name().lower()}.{random_last_name().lower()}{random.randint(1, 99)}",
        # firstnamelastname
        lambda: f"{random_first_name().lower()}{random_last_name().lower()}",
        # first initial + lastname + digits
        lambda: f"{random_first_name()[0].lower()}{random_last_name().lower()}{random.randint(10, 999)}",
        # lastname + firstname digits
        lambda: f"{random_last_name().lower()}{random_first_name().lower()[:3]}{random.randint(1, 99)}",
    ]
    return random.choice(patterns)()
```

### Timing Variation

Between each form field, add human-like delays:

```python
# Between fields: 0.3-1.5s (reading the next label)
# Between pages: 2-5s (page loading + reading)
# Before CAPTCHA: 3-8s (looking at the challenge)
# Between account creations: 60-180s (doing other things)
```

### Don't Create at 3am

Schedule email creation during normal hours (8am-11pm local time) to match when real people sign up for email accounts.

## Error Handling

### Username Taken

Outlook and Mail.com will show an error if the username is already taken. Detection:

```python
# After entering username and tapping Next, check for error message
error_el = wda.find_element(
    "predicate string",
    'name CONTAINS "already" OR name CONTAINS "taken" '
    'OR name CONTAINS "not available" OR name CONTAINS "try another"'
)
if error_el:
    # Generate new username and retry
    ...
```

### Account Locked Immediately

If Microsoft locks an account right after creation (suspicious activity), mark it as `disabled` in the DB and move on. Don't retry with the same IP.

### CAPTCHA Failure

If CapSolver can't solve the CAPTCHA:
1. Take a new screenshot and retry (CAPTCHA may have refreshed)
2. If 2 failures, abandon this attempt
3. Reset cellular data for a fresh IP and try again later

### Safari State Recovery

If Safari gets stuck (popup, redirect, etc.):
```python
# Nuclear option: kill Safari, go home, start fresh
wda.terminate_app("com.apple.mobilesafari")
wda.press_button("home")
time.sleep(2)
# Clear Safari data via Settings if needed
```
