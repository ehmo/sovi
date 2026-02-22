# Authentication System

## Overview

SOVI uses a layered auth approach for managed social media accounts:
- **Email + Password** for initial signup and login
- **TOTP** for ongoing 2FA (stored encrypted in DB)
- **Disposable SMS** for one-time phone verification during signup (then discarded)
- **CAPTCHA solving** for automated signup/login flows
- **AES-256-GCM** encryption for all credentials at rest

## TOTP Management (`src/sovi/auth/totp.py`)

Uses `pyotp` library for RFC 6238 TOTP.

| Function | Description |
|----------|-------------|
| `generate_secret()` | New base32-encoded secret (32 chars) |
| `get_code(secret)` | Current 6-digit TOTP code |
| `verify_code(secret, code)` | Verify code (±1 time window) |
| `get_provisioning_uri(secret, username)` | `otpauth://` URI for QR enrollment |

**Usage in the system:**
1. During account creation, `generate_secret()` creates a TOTP secret
2. The secret is encrypted with AES-256-GCM and stored in `accounts.totp_secret_enc`
3. During login, the secret is decrypted and `get_code()` generates the current OTP
4. Future: Enable TOTP in platform settings after account creation

## Email Verification (`src/sovi/auth/email_verifier.py`)

IMAP-based verification code extraction for signup flows.

### Configuration

```python
@dataclass
class ImapConfig:
    host: str         # IMAP server hostname
    username: str     # Email account username
    password: str     # Email account password
    port: int = 993   # IMAP port (993 for SSL)
    ssl: bool = True
```

### Platform Patterns

**TikTok** (from `no-reply@tiktok.com`, `verify@tiktok.com`):
- `verification code:\s+(\d{4,6})`
- `code is:\s+(\d{4,6})`
- `(\d{6}).*verify`

**Instagram** (from `security@mail.instagram.com`, `no-reply@mail.instagram.com`):
- `confirmation code:\s+(\d{4,6})`
- `security code:\s+(\d{4,6})`
- `(\d{6}).*Instagram`

### poll_for_code()

Polling loop that:
1. Connects to IMAP server
2. Searches for UNSEEN emails from platform senders
3. Fetches recent messages (up to 5)
4. Applies regex patterns to extract code
5. Retries every `poll_interval` seconds until `timeout`

**Parameters:**
- `imap_config` — IMAP credentials
- `platform` — "tiktok" or "instagram"
- `target_email` — Filter for catch-all domains
- `timeout` — Max wait (default 120s)
- `poll_interval` — Poll frequency (default 5s)

## SMS Verification (`src/sovi/auth/sms_verifier.py`)

Disposable phone numbers via TextVerified API. Used once during signup, then discarded (ongoing 2FA uses TOTP).

### API

Base URL: `https://www.textverified.com/api`
Auth: Bearer token from `settings.textverified_api_key`

### Service Names

| Platform | TextVerified Service |
|----------|---------------------|
| tiktok | "TikTok" |
| instagram | "Instagram" |

### Functions

**`request_number(platform)`** → `SmsVerification | None`
- POST `/Verifications` with `{"id": "TikTok"}`
- Returns `SmsVerification(verification_id, phone_number, service)`

**`wait_for_code(verification, timeout=120)`** → `str | None`
- Polls GET `/Verifications/{id}` every 5s
- Extracts code from `data.code` or regex from `data.sms`

**`cancel_verification(verification)`** → `bool`
- PUT `/Verifications/{id}/Cancel`
- Releases the number back to the pool

## CAPTCHA Solving (`src/sovi/auth/captcha_solver.py`)

CapSolver API integration for automated CAPTCHA bypass.

### API

Base URL: `https://api.capsolver.com`
Auth: `clientKey` from `settings.capsolver_api_key`

### Task Flow

1. `_create_task(type, params)` → POST `/createTask` → returns `task_id`
2. `_get_result(task_id, timeout)` → Poll POST `/getTaskResult` until `status == "ready"`

### Solve Functions

**`solve_slide(screenshot_png)`**
- Task type: `AntiSliderTaskByImage`
- Input: Base64-encoded screenshot
- Output: Slide coordinates
- Used for: TikTok slide puzzles during signup

**`solve_image(screenshot_png, question)`**
- Task type: `ImageToTextTask`
- Input: Base64 screenshot + question text (e.g., "select all buses")
- Output: Solution coordinates
- Used for: Image recognition CAPTCHAs

**`solve_funcaptcha(public_key, page_url)`**
- Task type: `FunCaptchaTaskProxyLess`
- Input: Arkose Labs public key + page URL
- Output: Token string
- Timeout: 120s (FunCaptcha takes longer)
- Used for: Some TikTok login flows

All solve functions emit events to `system_events` on failure for dashboard visibility.

## Credential Encryption (`src/sovi/crypto.py`)

AES-256-GCM encryption for sensitive fields stored in PostgreSQL.

### Key Management

- Master key: `SOVI_MASTER_KEY` environment variable
- Format: Base64-encoded 32 bytes
- Generate: `python -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"`

### Functions

**`encrypt(plaintext)`** → `str`
1. Generate random 12-byte nonce
2. Encrypt with AES-256-GCM (no associated data)
3. Return `base64(nonce + ciphertext)`

**`decrypt(token)`** → `str`
1. Base64-decode the token
2. Split: first 12 bytes = nonce, rest = ciphertext
3. Decrypt with AES-256-GCM

### Encrypted Fields in DB

| Table | Column | Contains |
|-------|--------|----------|
| accounts | email_enc | Account email address |
| accounts | password_enc | Account password |
| accounts | totp_secret_enc | TOTP base32 secret |
| accounts | proxy_credentials | Proxy auth string |
