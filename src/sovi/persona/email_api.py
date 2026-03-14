"""Email account creation via REST API — mail.tm provider.

No browser or CAPTCHA needed. Creates accounts via mail.tm API,
reads verification messages via the same API.
"""

from __future__ import annotations

import logging
import random
import re
import string
import time

import httpx

from sovi.crypto import encrypt
from sovi.db import sync_execute, sync_execute_one

logger = logging.getLogger(__name__)

MAILTM_BASE = "https://api.mail.tm"


# ---------------------------------------------------------------------------
# mail.tm helpers
# ---------------------------------------------------------------------------

def _get_mailtm_domain() -> str | None:
    """Fetch the currently active mail.tm domain."""
    try:
        resp = httpx.get(f"{MAILTM_BASE}/domains", timeout=15)
        data = resp.json()
        members = data.get("hydra:member", [])
        for m in members:
            if m.get("isActive"):
                return m["domain"]
    except Exception:
        logger.error("Failed to fetch mail.tm domains", exc_info=True)
    return None


def _sanitize_username(username: str) -> str:
    """Clean a username_base for use as an email local part."""
    clean = re.sub(r"[^a-zA-Z0-9._-]", "", username.replace("'", ""))
    return clean.lower() if clean else "user"


def _make_password() -> str:
    """Generate a strong random password for mail.tm accounts."""
    chars = string.ascii_letters + string.digits
    core = "".join(random.choices(chars, k=14))
    return f"Sv{core}!9"


def create_email_mailtm(persona: dict, domain: str | None = None) -> dict | None:
    """Create a mail.tm email account for a persona.

    Returns dict with email_plain, password_plain, api_token, db_id on success.
    """
    if not domain:
        domain = _get_mailtm_domain()
        if not domain:
            logger.error("No active mail.tm domain found")
            return None

    username = _sanitize_username(persona["username_base"])
    pid_suffix = str(persona["id"])[:6]
    # Random suffix to avoid collisions with any ghost accounts
    rand = "".join(random.choices(string.ascii_lowercase, k=3))
    address = f"{username}.{pid_suffix}{rand}@{domain}"
    password = _make_password()

    # Retry with exponential backoff on 429
    actual_address = address  # mail.tm may normalize (strip dots)
    for attempt in range(5):
        try:
            resp = httpx.post(
                f"{MAILTM_BASE}/accounts",
                json={"address": address, "password": password},
                timeout=15,
            )
            if resp.status_code == 201:
                # Use the address returned by API (may differ from what we sent)
                actual_address = resp.json().get("address", address)
                logger.info("Created: %s", actual_address)
                break
            elif resp.status_code == 422:
                # Address collision — generate new random suffix
                rand = "".join(random.choices(string.ascii_lowercase, k=4))
                address = f"{username}.{pid_suffix}{rand}@{domain}"
                continue
            elif resp.status_code == 429:
                wait = min(5 * (2 ** attempt), 60)
                logger.info("Rate limited, waiting %ds...", wait)
                time.sleep(wait)
                continue
            else:
                logger.error("mail.tm create failed: %s %s", resp.status_code, resp.text[:200])
                return None
        except Exception:
            logger.error("mail.tm API error", exc_info=True)
            return None
    else:
        logger.error("Failed after 5 attempts for %s", username)
        return None

    # Use the normalized address for all subsequent operations
    address = actual_address

    # Verify token auth works before storing
    time.sleep(1)
    api_token = _get_mailtm_token(address, password)
    if not api_token:
        # Retry token after longer wait
        time.sleep(3)
        api_token = _get_mailtm_token(address, password)
    if not api_token:
        logger.warning("Token auth failed for %s — account may be unusable", address)

    # Store in DB
    row = sync_execute_one(
        """INSERT INTO email_accounts
           (persona_id, provider, email, password, imap_host, imap_port, domain, status)
           VALUES (%s, %s, %s, %s, %s, %s, %s, 'available')
           RETURNING id""",
        (str(persona["id"]), "mailtm", encrypt(address), encrypt(password),
         "api.mail.tm", 0, domain),
    )
    db_id = row["id"] if row else None

    return {
        "email_plain": address,
        "password_plain": password,
        "api_token": api_token,
        "db_id": db_id,
    }


def _get_mailtm_token(address: str, password: str) -> str | None:
    """Get JWT token for mail.tm API access."""
    try:
        resp = httpx.post(
            f"{MAILTM_BASE}/token",
            json={"address": address, "password": password},
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json().get("token")
        logger.debug("Token failed for %s: %s", address, resp.status_code)
    except Exception:
        logger.warning("Failed to get mail.tm token for %s", address)
    return None


def read_messages(address: str, password: str) -> list[dict]:
    """Read all messages from a mail.tm inbox."""
    token = _get_mailtm_token(address, password)
    if not token:
        return []
    try:
        resp = httpx.get(
            f"{MAILTM_BASE}/messages",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json().get("hydra:member", [])
    except Exception:
        logger.warning("Failed to read mail.tm messages for %s", address)
    return []


def read_message_body(address: str, password: str, message_id: str) -> str | None:
    """Read the full body of a specific message."""
    token = _get_mailtm_token(address, password)
    if not token:
        return None
    try:
        resp = httpx.get(
            f"{MAILTM_BASE}/messages/{message_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("text") or data.get("html", "")
    except Exception:
        logger.warning("Failed to read message %s", message_id)
    return None


def poll_for_code_mailtm(
    address: str,
    password: str,
    platform: str,
    timeout: int = 120,
    poll_interval: int = 5,
) -> str | None:
    """Poll mail.tm inbox for a verification code from a social platform."""
    patterns = {
        "tiktok": [
            re.compile(r"verification code[:\s]+(\d{4,6})", re.IGNORECASE),
            re.compile(r"code is[:\s]+(\d{4,6})", re.IGNORECASE),
            re.compile(r"\b(\d{6})\b.*verify", re.IGNORECASE),
        ],
        "instagram": [
            re.compile(r"confirmation code[:\s]+(\d{4,6})", re.IGNORECASE),
            re.compile(r"security code[:\s]+(\d{4,6})", re.IGNORECASE),
            re.compile(r"\b(\d{6})\b.*Instagram", re.IGNORECASE),
        ],
        "reddit": [
            re.compile(r"verification code[:\s]+(\d{4,6})", re.IGNORECASE),
            re.compile(r"\b(\d{6})\b", re.IGNORECASE),
        ],
        "facebook": [
            re.compile(r"confirmation code[:\s]+(\d{4,6})", re.IGNORECASE),
            re.compile(r"FB-(\d{5,6})", re.IGNORECASE),
        ],
    }
    platform_patterns = patterns.get(platform, [re.compile(r"\b(\d{4,6})\b")])

    senders = {
        "tiktok": ["tiktok.com"],
        "instagram": ["instagram.com", "mail.instagram.com"],
        "reddit": ["reddit.com", "redditmail.com"],
        "facebook": ["facebook.com", "facebookmail.com"],
        "x_twitter": ["twitter.com", "x.com"],
        "youtube_shorts": ["google.com", "youtube.com"],
        "linkedin": ["linkedin.com"],
    }
    platform_senders = senders.get(platform, [])

    deadline = time.time() + timeout
    seen_ids: set[str] = set()

    while time.time() < deadline:
        token = _get_mailtm_token(address, password)
        if not token:
            time.sleep(poll_interval)
            continue

        try:
            resp = httpx.get(
                f"{MAILTM_BASE}/messages",
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
            if resp.status_code != 200:
                time.sleep(poll_interval)
                continue

            messages = resp.json().get("hydra:member", [])
            for msg in messages:
                msg_id = msg.get("id", "")
                if msg_id in seen_ids:
                    continue
                seen_ids.add(msg_id)

                sender = msg.get("from", {}).get("address", "")
                if platform_senders and not any(s in sender for s in platform_senders):
                    continue

                body = read_message_body(address, password, msg_id)
                if not body:
                    continue

                for pattern in platform_patterns:
                    match = pattern.search(body)
                    if match:
                        code = match.group(1)
                        logger.info("Found %s code: %s (from %s)", platform, code, sender)
                        return code

        except Exception:
            logger.warning("mail.tm poll error", exc_info=True)

        time.sleep(poll_interval)

    logger.warning("Timed out waiting for %s code at %s (%ds)", platform, address, timeout)
    return None


# ---------------------------------------------------------------------------
# Batch operations
# ---------------------------------------------------------------------------

def create_emails_batch(personas: list[dict]) -> list[dict]:
    """Create mail.tm email accounts for a batch of personas.

    Uses 8s delay between accounts to respect rate limits.
    Returns list of successfully created account dicts.
    """
    domain = _get_mailtm_domain()
    if not domain:
        logger.error("No active mail.tm domain — cannot create emails")
        return []

    logger.info("Using mail.tm domain: %s", domain)
    results = []
    total = len(personas)
    token_ok = 0

    for i, persona in enumerate(personas):
        logger.info("[%d/%d] %s %s", i + 1, total, persona["first_name"], persona["last_name"])
        result = create_email_mailtm(persona, domain=domain)
        if result:
            results.append(result)
            if result.get("api_token"):
                token_ok += 1
        else:
            logger.warning("Failed: %s %s", persona["first_name"], persona["last_name"])

        # Respect rate limits: 8s between accounts
        if i < total - 1:
            time.sleep(8)

    logger.info("Created %d/%d emails (%d with verified token)", len(results), total, token_ok)
    return results
