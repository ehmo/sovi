"""Populate database with seed data from niche configs and device info."""

from __future__ import annotations

import logging

import psycopg

from sovi.config import load_all_niche_configs, settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)


def populate_niche_subreddit_map() -> None:
    """Read subreddit configs from niche YAMLs and insert into niche_subreddit_map."""
    configs = load_all_niche_configs()
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            for slug, cfg in configs.items():
                # Look up niche ID
                cur.execute("SELECT id FROM niches WHERE slug = %s", (slug,))
                row = cur.fetchone()
                if not row:
                    logger.warning("Niche %s not in database, skipping", slug)
                    continue
                niche_id = row[0]

                # Get Reddit subreddit configs
                reddit_cfg = cfg.get("platforms", {}).get("reddit", {})
                subreddits = reddit_cfg.get("subreddits", [])

                for sub in subreddits:
                    name = sub["name"]
                    cur.execute(
                        """INSERT INTO niche_subreddit_map
                           (niche_id, subreddit_name, min_karma_required,
                            min_account_age_days, is_active)
                           VALUES (%s, %s, %s, %s, TRUE)
                           ON CONFLICT (niche_id, subreddit_name) DO UPDATE SET
                               min_karma_required = EXCLUDED.min_karma_required,
                               min_account_age_days = EXCLUDED.min_account_age_days""",
                        (
                            niche_id,
                            name,
                            sub.get("min_karma", 0),
                            sub.get("min_age_days", 0),
                        ),
                    )
                    logger.info("  %s -> r/%s (karma>=%d, age>=%dd)",
                                slug, name, sub.get("min_karma", 0), sub.get("min_age_days", 0))
            conn.commit()
    logger.info("Niche-subreddit map populated")


def populate_devices() -> None:
    """Ensure devices table has our iPhones with correct status."""
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE devices SET status = 'active', connected_since = now(), updated_at = now()
                   WHERE udid IN ('00008140-001975DC3678801C', '00008140-001A00141163001C')""",
            )
            conn.commit()
    logger.info("Devices status updated")


if __name__ == "__main__":
    populate_niche_subreddit_map()
    populate_devices()
    logger.info("Setup complete")
