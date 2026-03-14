#!/usr/bin/env python3
"""Create social media accounts for all personas across all platforms.

Uses WDA device automation for app-based (TikTok, Instagram) and
Safari web-based (Reddit, YouTube, Facebook, LinkedIn) signups.

Run: cd ~/Work/ai/sovi && .venv/bin/python scripts/create_social_accounts.py [--platform PLATFORM] [--count N] [--test]
"""
import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sovi.db import sync_execute
from sovi.device.wda_client import WDASession
from sovi.persona.account_creator import create_account_for_persona, PLATFORM_PRIORITY

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def get_personas_needing_accounts(platform: str | None = None):
    """Get personas that don't have accounts on the specified platform(s)."""
    if platform:
        platforms = [platform]
    else:
        platforms = PLATFORM_PRIORITY

    results = {}
    for plat in platforms:
        rows = sync_execute("""
            SELECT p.id, p.first_name, p.last_name, p.display_name,
                   p.username_base, p.date_of_birth, p.gender, p.age,
                   p.state, p.city, p.niche_id, p.bio_short,
                   n.slug as niche_slug
            FROM personas p
            JOIN niches n ON n.id = p.niche_id
            WHERE EXISTS (
                SELECT 1 FROM email_accounts ea WHERE ea.persona_id = p.id
            )
            AND NOT EXISTS (
                SELECT 1 FROM accounts a
                WHERE a.persona_id = p.id AND a.platform = %s
                AND a.deleted_at IS NULL
            )
            ORDER BY n.slug, p.display_name
        """, (plat,))
        results[plat] = rows
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", type=str, help="Single platform to target")
    parser.add_argument("--count", type=int, default=0, help="Max accounts per platform (0=all)")
    parser.add_argument("--test", action="store_true", help="Test with just 1 account on 1 platform")
    parser.add_argument("--wda-host", default="localhost", help="WDA host")
    parser.add_argument("--wda-port", type=int, default=8100, help="WDA port")
    args = parser.parse_args()

    # Check WDA is available
    from sovi.device.wda_client import WDADevice
    device = WDADevice(name="iPhone", udid="local", wda_port=args.wda_port)
    wda = WDASession(device)
    try:
        wda.connect()
        print(f"WDA ready: connected to {device.base_url}")
    except Exception as e:
        print(f"ERROR: WDA not available at {device.base_url}: {e}")
        return

    # Get personas needing accounts
    needed = get_personas_needing_accounts(args.platform)

    total_needed = sum(len(v) for v in needed.values())
    print(f"\nAccounts needed across {len(needed)} platforms: {total_needed}")
    for plat, personas in needed.items():
        print(f"  {plat}: {len(personas)} personas")

    if total_needed == 0:
        print("All personas have accounts on all platforms!")
        return

    if args.test:
        # Just try 1 account on the first platform that needs one
        for plat, personas in needed.items():
            if personas:
                p = personas[0]
                print(f"\nTest: Creating {plat} account for {p['display_name']}")
                result = create_account_for_persona(wda, dict(p), plat, device_id=None)
                print(f"Result: {result}")
                return
        print("No accounts to create")
        return

    # Create accounts platform by platform
    created = 0
    failed = 0

    for plat in PLATFORM_PRIORITY:
        if plat not in needed or not needed[plat]:
            continue

        personas = needed[plat]
        limit = args.count if args.count > 0 else len(personas)
        batch = personas[:limit]

        print(f"\n{'='*60}")
        print(f"Platform: {plat} — creating {len(batch)} accounts")
        print(f"{'='*60}")

        for i, persona in enumerate(batch):
            print(f"\n[{i+1}/{len(batch)}] {persona['display_name']} ({persona['niche_slug']})")
            try:
                result = create_account_for_persona(wda, dict(persona), plat, device_id=None)
                if result:
                    print(f"  SUCCESS: {result.get('username', '?')}")
                    created += 1
                else:
                    print(f"  FAILED")
                    failed += 1
            except Exception as e:
                print(f"  ERROR: {e}")
                failed += 1

            # Cooldown between accounts
            if i < len(batch) - 1:
                cooldown = 30 + (i % 3) * 15  # 30-60s between accounts
                print(f"  Cooling down {cooldown}s...")
                time.sleep(cooldown)

    print(f"\n{'='*60}")
    print(f"RESULTS: {created} created, {failed} failed")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
