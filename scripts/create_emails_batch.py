#!/usr/bin/env python3
"""Batch create email accounts for all personas without emails.

Uses mail.tm REST API (no browser, no CAPTCHA).

Run: cd ~/Work/ai/sovi && .venv/bin/python scripts/create_emails_batch.py [--count N] [--test]
"""
import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sovi.db import sync_execute
from sovi.persona.email_api import create_emails_batch, create_email_mailtm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def get_personas_without_email():
    """Get all personas that don't have an email account yet."""
    return sync_execute("""
        SELECT p.id, p.first_name, p.last_name, p.display_name,
               p.username_base, p.date_of_birth, p.gender, p.age,
               p.state, p.city,
               n.slug as niche_slug
        FROM personas p
        JOIN niches n ON n.id = p.niche_id
        WHERE NOT EXISTS (
            SELECT 1 FROM email_accounts ea WHERE ea.persona_id = p.id
        )
        ORDER BY n.slug, p.display_name
    """)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=0, help="Max accounts to create (0=all)")
    parser.add_argument("--test", action="store_true", help="Test with just 1 account")
    args = parser.parse_args()

    personas = get_personas_without_email()
    total = len(personas)
    print(f"\n{total} personas need email accounts")

    if total == 0:
        print("All personas have email accounts!")
        return

    for p in personas[:5]:
        print(f"  {p['display_name']:25s} ({p['niche_slug']}, {p['gender']}, {p['age']})")
    if total > 5:
        print(f"  ... and {total - 5} more")

    limit = 1 if args.test else (args.count if args.count > 0 else total)
    batch = personas[:limit]
    print(f"\nCreating {len(batch)} email accounts via mail.tm API...")

    if args.test:
        result = create_email_mailtm(batch[0])
        if result:
            print(f"\nSUCCESS: {result['email_plain']}")
        else:
            print("\nFAILED")
        return

    results = create_emails_batch(batch)
    print(f"\n{'='*60}")
    print(f"RESULTS: {len(results)}/{len(batch)} email accounts created")
    for r in results:
        print(f"  {r['email_plain']}")

    remaining = get_personas_without_email()
    print(f"\nRemaining without email: {len(remaining)}")


if __name__ == "__main__":
    main()
