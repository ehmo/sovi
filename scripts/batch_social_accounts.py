#!/usr/bin/env python3
"""Batch social account creation for all personas.

Creates platform accounts across all niches, using both iPhones in parallel.
Supports resume — checks existing accounts before creating.

Usage (on studio):
    .venv/bin/python scripts/batch_social_accounts.py --platform tiktok
    .venv/bin/python scripts/batch_social_accounts.py --platform instagram
    .venv/bin/python scripts/batch_social_accounts.py --platform reddit
    .venv/bin/python scripts/batch_social_accounts.py --all
    .venv/bin/python scripts/batch_social_accounts.py --platform tiktok --phone b
    .venv/bin/python scripts/batch_social_accounts.py --platform tiktok --parallel
"""
import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, "src")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("batch_social")

from sovi.db import sync_execute
from sovi.device.wda_client import WDADevice, WDASession
from sovi.persona.account_creator import create_account_for_persona

# Phone config
PHONES = {
    "a": {"wda_port": 8101, "device_id": "4ae20247-3918-4afd-8a71-7830c1e6f37c", "name": "iPhone-A"},
    "b": {"wda_port": 8100, "device_id": "37c2a7d5-4840-437f-9e78-92cee843b3a5", "name": "iPhone-B"},
}

PLATFORM_ORDER = ["tiktok", "instagram", "reddit", "youtube_shorts", "facebook", "linkedin", "x_twitter"]

# App-based platforms (need WDA app interaction)
APP_PLATFORMS = {"tiktok", "instagram", "x_twitter"}
# Web-based platforms (Safari)
WEB_PLATFORMS = {"reddit", "youtube_shorts", "facebook", "linkedin"}


def get_personas_needing_platform(platform: str) -> list[dict]:
    """Get personas that have an email account but don't have a platform account yet."""
    rows = sync_execute(
        """SELECT p.id, p.first_name, p.last_name, p.display_name,
                  p.username_base, p.gender, p.date_of_birth, p.niche_id,
                  p.bio_short, p.occupation, p.interests, p.personality
           FROM personas p
           WHERE p.status IN ('active', 'ready')
             AND EXISTS (
                 SELECT 1 FROM email_accounts ea
                 WHERE ea.persona_id = p.id
                   AND ea.status = 'available'
             )
             AND NOT EXISTS (
                 SELECT 1 FROM accounts a
                 WHERE a.persona_id = p.id
                   AND a.platform = %s
                   AND a.deleted_at IS NULL
             )
           ORDER BY p.niche_id, p.display_name""",
        (platform,),
    )
    return rows


def create_on_phone(phone_key: str, persona: dict, platform: str) -> dict | None:
    """Create account for one persona on one phone."""
    phone = PHONES[phone_key]
    device = WDADevice(
        name=phone["name"],
        udid=phone_key,  # Not used for connection, just identification
        wda_port=phone["wda_port"],
    )
    wda = WDASession(device)

    # Connect and health check
    try:
        wda.connect()
        if not wda.is_ready():
            logger.error("%s not ready", phone["name"])
            return None
    except Exception:
        logger.error("%s WDA not available", phone["name"], exc_info=True)
        return None

    logger.info(
        "Creating %s account for %s on %s",
        platform, persona["display_name"], phone["name"],
    )

    result = create_account_for_persona(
        wda, persona, platform, device_id=phone["device_id"],
    )

    if result:
        logger.info(
            "SUCCESS: %s/%s for %s (account_id=%s)",
            platform, result.get("username", "?"),
            persona["display_name"], result["id"],
        )
    else:
        logger.warning(
            "FAILED: %s for %s on %s",
            platform, persona["display_name"], phone["name"],
        )

    return result


def run_sequential(personas: list[dict], platform: str, phone_key: str) -> dict:
    """Run account creation sequentially on one phone."""
    stats = {"success": 0, "fail": 0, "total": len(personas)}

    for i, persona in enumerate(personas):
        logger.info(
            "--- [%d/%d] %s: %s ---",
            i + 1, stats["total"], platform, persona["display_name"],
        )
        result = create_on_phone(phone_key, persona, platform)
        if result:
            stats["success"] += 1
        else:
            stats["fail"] += 1

        # Brief pause between accounts (IP rotation happens inside signup)
        if i < len(personas) - 1:
            delay = 10
            logger.info("Waiting %ds before next account...", delay)
            time.sleep(delay)

    return stats


def run_parallel(personas: list[dict], platform: str) -> dict:
    """Run account creation on both phones in parallel.

    Alternates personas between phones. Each phone processes sequentially.
    """
    phone_a_personas = personas[::2]  # Even indices
    phone_b_personas = personas[1::2]  # Odd indices

    logger.info(
        "Parallel mode: iPhone-A gets %d, iPhone-B gets %d personas",
        len(phone_a_personas), len(phone_b_personas),
    )

    stats = {"success": 0, "fail": 0, "total": len(personas)}

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {}
        if phone_a_personas:
            futures[executor.submit(run_sequential, phone_a_personas, platform, "a")] = "a"
        if phone_b_personas:
            futures[executor.submit(run_sequential, phone_b_personas, platform, "b")] = "b"

        for future in as_completed(futures):
            phone = futures[future]
            try:
                phone_stats = future.result()
                stats["success"] += phone_stats["success"]
                stats["fail"] += phone_stats["fail"]
                logger.info("Phone %s done: %s", phone, phone_stats)
            except Exception:
                logger.error("Phone %s crashed", phone, exc_info=True)

    return stats


def main():
    parser = argparse.ArgumentParser(description="Batch social account creation")
    parser.add_argument("--platform", choices=PLATFORM_ORDER, help="Platform to create accounts for")
    parser.add_argument("--all", action="store_true", help="Create for all platforms in order")
    parser.add_argument("--phone", choices=["a", "b"], default="b", help="Phone to use (default: b)")
    parser.add_argument("--parallel", action="store_true", help="Use both phones in parallel")
    parser.add_argument("--limit", type=int, default=0, help="Max accounts to create (0=all)")
    parser.add_argument("--niche", help="Only create for this niche slug")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be created")
    args = parser.parse_args()

    if not args.platform and not args.all:
        parser.error("Specify --platform or --all")

    platforms = PLATFORM_ORDER if args.all else [args.platform]

    for platform in platforms:
        personas = get_personas_needing_platform(platform)

        if args.niche:
            # Filter by niche
            niche_row = sync_execute(
                "SELECT id FROM niches WHERE slug = %s", (args.niche,)
            )
            if niche_row:
                niche_id = str(list(niche_row[0].values())[0])
                personas = [p for p in personas if str(p["niche_id"]) == niche_id]

        if args.limit:
            personas = personas[:args.limit]

        if not personas:
            logger.info("No personas need %s accounts — skipping", platform)
            continue

        logger.info(
            "=== %s: %d personas need accounts ===",
            platform.upper(), len(personas),
        )

        if args.dry_run:
            for p in personas:
                print(f"  Would create {platform} for: {p['display_name']}")
            continue

        if args.parallel:
            stats = run_parallel(personas, platform)
        else:
            stats = run_sequential(personas, platform, args.phone)

        logger.info(
            "=== %s COMPLETE: %d/%d success, %d failed ===",
            platform.upper(), stats["success"], stats["total"], stats["fail"],
        )

    logger.info("Batch creation finished.")


if __name__ == "__main__":
    main()
