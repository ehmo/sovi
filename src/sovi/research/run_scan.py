"""Run a full research scan across all platforms and niches.

Usage:
    python -m sovi.research.run_scan
    python -m sovi.research.run_scan --reddit-only
    python -m sovi.research.run_scan --tiktok-only
    python -m sovi.research.run_scan --stories
"""

from __future__ import annotations

import argparse
import asyncio
import logging

logger = logging.getLogger(__name__)


async def run_reddit_scan() -> int:
    """Scrape Reddit niche subreddits and store trending topics."""
    from sovi.research.scrapers.reddit import save_trending_to_db, scrape_niche_subreddits

    logger.info("=== Reddit Niche Scan ===")
    posts = await scrape_niche_subreddits()
    logger.info("Collected %d Reddit posts", len(posts))

    for i, p in enumerate(posts[:10], 1):
        logger.info("  %d. [%d] r/%s: %s", i, p["score"], p["subreddit"], p["title"][:80])

    saved = save_trending_to_db(posts)
    logger.info("Saved %d Reddit trends to DB", saved)
    return saved


async def run_tiktok_scan() -> int:
    """Scrape TikTok trends from Creative Center, search suggest, and Google Trends."""
    from sovi.research.scrapers.tiktok import save_trending_to_db, scrape_tiktok_trends

    logger.info("=== TikTok Trend Scan ===")
    trends = await scrape_tiktok_trends()
    logger.info("Collected %d TikTok trends", len(trends))

    for i, t in enumerate(trends[:10], 1):
        logger.info("  %d. %s [%s]", i, t.get("hashtag", ""), t.get("source", ""))

    saved = save_trending_to_db(trends)
    logger.info("Saved %d TikTok trends to DB", saved)
    return saved


async def run_story_scan() -> None:
    """Scrape Reddit stories for video content sourcing."""
    from sovi.research.scrapers.reddit import scrape_all_stories

    logger.info("=== Reddit Story Scan ===")
    stories = await scrape_all_stories(min_score=500)
    logger.info("Collected %d stories", len(stories))

    for i, s in enumerate(stories[:10], 1):
        logger.info(
            "  %d. [%d] r/%s: %s (%d words)",
            i, s["score"], s["subreddit"], s["title"][:60], s["word_count"],
        )


async def run_full_scan() -> None:
    """Run all research scrapers."""
    reddit_count = await run_reddit_scan()
    tiktok_count = await run_tiktok_scan()
    logger.info("=== Scan Complete: %d Reddit + %d TikTok trends ===", reddit_count, tiktok_count)


def main() -> None:
    parser = argparse.ArgumentParser(description="SOVI Research Scanner")
    parser.add_argument("--reddit-only", action="store_true")
    parser.add_argument("--tiktok-only", action="store_true")
    parser.add_argument("--stories", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.reddit_only:
        asyncio.run(run_reddit_scan())
    elif args.tiktok_only:
        asyncio.run(run_tiktok_scan())
    elif args.stories:
        asyncio.run(run_story_scan())
    else:
        asyncio.run(run_full_scan())


if __name__ == "__main__":
    main()
