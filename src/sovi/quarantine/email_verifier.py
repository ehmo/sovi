"""Email verification code extraction via IMAP.

Polls an IMAP inbox for verification emails from TikTok/Instagram,
extracts the verification code using platform-specific regex patterns.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from imapclient import IMAPClient

logger = logging.getLogger(__name__)

# Platform-specific patterns for extracting verification codes from emails
PLATFORM_PATTERNS: dict[str, list[re.Pattern]] = {
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
}

PLATFORM_SENDERS: dict[str, list[str]] = {
    "tiktok": ["no-reply@tiktok.com", "verify@tiktok.com"],
    "instagram": ["security@mail.instagram.com", "no-reply@mail.instagram.com"],
}


@dataclass
class ImapConfig:
    host: str
    username: str
    password: str
    port: int = 993
    ssl: bool = True


def poll_for_code(
    imap_config: ImapConfig,
    platform: str,
    target_email: str | None = None,
    timeout: int = 120,
    poll_interval: int = 5,
) -> str | None:
    """Poll IMAP inbox for a verification code.

    Args:
        imap_config: IMAP server credentials.
        platform: 'tiktok' or 'instagram'.
        target_email: If set, only check emails TO this address (for catch-all domains).
        timeout: Max seconds to wait.
        poll_interval: Seconds between polls.

    Returns:
        The extracted verification code, or None if not found within timeout.
    """
    patterns = PLATFORM_PATTERNS.get(platform, [])
    senders = PLATFORM_SENDERS.get(platform, [])

    if not patterns:
        logger.error("No email patterns configured for platform: %s", platform)
        return None

    deadline = time.time() + timeout
    start_time = time.time()

    while time.time() < deadline:
        try:
            with IMAPClient(imap_config.host, port=imap_config.port, ssl=imap_config.ssl) as client:
                client.login(imap_config.username, imap_config.password)
                client.select_folder("INBOX")

                # Search for recent emails from platform senders
                criteria = ["UNSEEN", "SINCE", time.strftime("%d-%b-%Y")]
                if senders:
                    # Search for each sender separately
                    for sender in senders:
                        messages = client.search(criteria + ["FROM", sender])
                        if not messages:
                            continue

                        # Get the most recent messages
                        for msg_id in sorted(messages, reverse=True)[:5]:
                            response = client.fetch([msg_id], ["BODY[TEXT]", "ENVELOPE"])
                            if msg_id not in response:
                                continue

                            body = response[msg_id].get(b"BODY[TEXT]", b"")
                            if isinstance(body, bytes):
                                body = body.decode("utf-8", errors="replace")

                            # Try each pattern
                            for pattern in patterns:
                                match = pattern.search(body)
                                if match:
                                    code = match.group(1)
                                    logger.info("Found %s verification code: %s", platform, code)
                                    return code

        except Exception:
            logger.warning("IMAP poll error", exc_info=True)

        elapsed = time.time() - start_time
        logger.debug("No code yet after %.0fs, retrying in %ds", elapsed, poll_interval)
        time.sleep(poll_interval)

    logger.warning("Timed out waiting for %s verification email (%ds)", platform, timeout)
    return None
