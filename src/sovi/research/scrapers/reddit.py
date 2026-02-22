"""Reddit scraper — public JSON endpoints (no API key) + optional PRAW.

Uses Reddit's public .json suffix on URLs which works without authentication
for ~60-80 requests before rate limiting. Falls back to PRAW when configured.
Stores results in the trending_topics table.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import UTC, datetime

import httpx

from sovi.config import load_all_niche_configs, settings

logger = logging.getLogger(__name__)

# Reddit blocks requests without a real-looking User-Agent
_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
]

# Target subreddits for Reddit Story format
STORY_SUBREDDITS = [
    "AmItheAsshole",
    "MaliciousCompliance",
    "ProRevenge",
    "TIFU",
    "nosleep",
    "TrueOffMyChest",
    "relationship_advice",
    "pettyrevenge",
    "entitledparents",
]


def _get_headers() -> dict[str, str]:
    return {"User-Agent": random.choice(_USER_AGENTS)}


# ---------------------------------------------------------------------------
# Public JSON scraping (no auth needed)
# ---------------------------------------------------------------------------


async def fetch_subreddit_json(
    subreddit: str,
    sort: str = "hot",
    limit: int = 25,
    time_filter: str | None = None,
) -> list[dict]:
    """Fetch posts from a subreddit using Reddit's public JSON API.

    Args:
        subreddit: Name without r/ prefix.
        sort: One of 'hot', 'new', 'rising', 'top'.
        limit: Max posts (Reddit caps at 100).
        time_filter: For 'top' sort — 'hour', 'day', 'week', 'month', 'year', 'all'.
    """
    url = f"https://www.reddit.com/r/{subreddit}/{sort}.json"
    params: dict[str, str | int] = {"limit": min(limit, 100), "raw_json": 1}
    if time_filter and sort == "top":
        params["t"] = time_filter

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        resp = await client.get(url, params=params, headers=_get_headers())
        resp.raise_for_status()
        data = resp.json()

    posts = []
    for child in data.get("data", {}).get("children", []):
        p = child.get("data", {})
        if not p:
            continue
        posts.append({
            "id": p.get("id", ""),
            "title": p.get("title", ""),
            "selftext": (p.get("selftext") or "")[:5000],
            "score": p.get("score", 0),
            "upvote_ratio": p.get("upvote_ratio", 0),
            "num_comments": p.get("num_comments", 0),
            "url": p.get("url", ""),
            "permalink": f"https://reddit.com{p.get('permalink', '')}",
            "created_utc": p.get("created_utc", 0),
            "subreddit": subreddit,
            "is_video": p.get("is_video", False),
            "over_18": p.get("over_18", False),
            "link_flair_text": p.get("link_flair_text", ""),
            "word_count": len((p.get("selftext") or "").split()) if p.get("selftext") else 0,
        })
    return posts


async def scrape_rising(subreddit: str, limit: int = 25) -> list[dict]:
    """Scrape rising posts from a subreddit for early trend detection."""
    return await fetch_subreddit_json(subreddit, sort="rising", limit=limit)


async def scrape_hot(subreddit: str, limit: int = 25) -> list[dict]:
    """Scrape hot posts from a subreddit."""
    return await fetch_subreddit_json(subreddit, sort="hot", limit=limit)


async def scrape_top_stories(
    subreddit: str,
    time_filter: str = "week",
    limit: int = 50,
    min_score: int = 1000,
) -> list[dict]:
    """Scrape top story posts for Reddit Story video format."""
    posts = await fetch_subreddit_json(subreddit, sort="top", limit=limit, time_filter=time_filter)
    stories = []
    for p in posts:
        if p["score"] < min_score:
            continue
        if not p["selftext"] or p["word_count"] < 50:
            continue
        if p["over_18"]:
            continue
        stories.append(p)
    return stories


async def scrape_all_stories(
    time_filter: str = "week",
    limit_per_sub: int = 25,
    min_score: int = 500,
) -> list[dict]:
    """Scrape top stories from all story subreddits."""
    all_stories: list[dict] = []
    for sub in STORY_SUBREDDITS:
        try:
            stories = await scrape_top_stories(sub, time_filter, limit_per_sub, min_score)
            all_stories.extend(stories)
            logger.info("  r/%s: %d stories", sub, len(stories))
            # Polite delay to avoid 429s
            time.sleep(random.uniform(1.0, 2.5))
        except Exception:
            logger.warning("Failed to scrape r/%s", sub, exc_info=True)
            continue
    return sorted(all_stories, key=lambda s: s["score"], reverse=True)


# ---------------------------------------------------------------------------
# Niche-aware scraping — uses niche YAML configs
# ---------------------------------------------------------------------------


async def scrape_niche_subreddits(niche_slug: str | None = None) -> list[dict]:
    """Scrape rising/hot from subreddits defined in niche configs.

    If niche_slug is provided, only scrape for that niche.
    Otherwise scrape all niches.
    """
    configs = load_all_niche_configs()
    if niche_slug:
        configs = {k: v for k, v in configs.items() if k == niche_slug}

    all_posts: list[dict] = []
    for slug, cfg in configs.items():
        reddit_cfg = cfg.get("platforms", {}).get("reddit", {})
        subreddits = reddit_cfg.get("subreddits", [])

        for sub_cfg in subreddits:
            name = sub_cfg["name"]
            try:
                # Rising gives us early trend signals
                rising = await scrape_rising(name, limit=15)
                # Hot gives us currently popular content
                hot = await scrape_hot(name, limit=15)

                # Deduplicate by post ID
                seen: set[str] = set()
                for post in rising + hot:
                    if post["id"] not in seen:
                        post["niche_slug"] = slug
                        all_posts.append(post)
                        seen.add(post["id"])

                logger.info("r/%s (%s): %d posts", name, slug, len(seen))
                time.sleep(random.uniform(1.0, 2.0))
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    logger.warning("Rate limited on r/%s, waiting 30s", name)
                    time.sleep(30)
                else:
                    logger.warning("HTTP %d on r/%s", e.response.status_code, name)
            except Exception:
                logger.warning("Failed r/%s", name, exc_info=True)

    return sorted(all_posts, key=lambda p: p["score"], reverse=True)


# ---------------------------------------------------------------------------
# Database persistence
# ---------------------------------------------------------------------------


def save_trending_to_db(posts: list[dict], platform: str = "reddit") -> int:
    """Save scraped posts as trending_topics in the database.

    Returns the number of rows inserted/updated.
    """
    import psycopg

    if not posts:
        return 0

    # Map niche slugs to IDs
    niche_ids: dict[str, str] = {}
    count = 0

    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            # Prefetch niche IDs
            cur.execute("SELECT id, slug FROM niches")
            for row in cur.fetchall():
                niche_ids[row[1]] = str(row[0])

            for post in posts:
                niche_slug = post.get("niche_slug")
                niche_id = niche_ids.get(niche_slug) if niche_slug else None

                cur.execute(
                    """INSERT INTO trending_topics
                       (platform, topic_text, hashtag, trend_score, niche_id, detected_at, is_active)
                       VALUES (%s, %s, %s, %s, %s, now(), TRUE)
                       ON CONFLICT (platform, topic_text, niche_id)
                       WHERE is_active = true
                       DO UPDATE SET
                           trend_score = GREATEST(trending_topics.trend_score, EXCLUDED.trend_score),
                           detected_at = now()""",
                    (
                        platform,
                        post["title"][:500],
                        f"r/{post['subreddit']}",
                        float(post["score"]),
                        niche_id,
                    ),
                )
                count += 1
            conn.commit()
    return count


# ---------------------------------------------------------------------------
# PRAW-based scraping (requires Reddit API credentials)
# ---------------------------------------------------------------------------


def _has_praw_credentials() -> bool:
    return bool(settings.reddit_client_id and settings.reddit_client_secret)


def get_reddit_client():
    """Create an authenticated Reddit client via PRAW."""
    import praw

    return praw.Reddit(
        client_id=settings.reddit_client_id,
        client_secret=settings.reddit_client_secret,
        username=settings.reddit_username,
        password=settings.reddit_password,
        user_agent="sovi/0.1.0",
    )


def praw_scrape_rising(subreddit_name: str, limit: int = 25) -> list[dict]:
    """Scrape rising posts via PRAW (requires API credentials)."""
    reddit = get_reddit_client()
    sub = reddit.subreddit(subreddit_name)
    posts = []
    for post in sub.rising(limit=limit):
        posts.append({
            "id": post.id,
            "title": post.title,
            "selftext": post.selftext[:5000] if post.selftext else "",
            "score": post.score,
            "upvote_ratio": post.upvote_ratio,
            "num_comments": post.num_comments,
            "url": post.url,
            "permalink": f"https://reddit.com{post.permalink}",
            "created_utc": post.created_utc,
            "subreddit": subreddit_name,
            "is_video": post.is_video,
            "over_18": post.over_18,
        })
    return posts


# ---------------------------------------------------------------------------
# CLI runner
# ---------------------------------------------------------------------------


async def _main() -> None:
    """Run the Reddit scraper and store results."""
    import asyncio

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    logger.info("=== Reddit Research Scraper ===")

    # Scrape niche subreddits
    posts = await scrape_niche_subreddits()
    logger.info("Total posts collected: %d", len(posts))

    # Show top 10
    for i, p in enumerate(posts[:10], 1):
        logger.info(
            "  %d. [%d] r/%s: %s",
            i, p["score"], p["subreddit"], p["title"][:80],
        )

    # Save to DB
    saved = save_trending_to_db(posts)
    logger.info("Saved %d trending topics to database", saved)

    # Also scrape stories
    logger.info("=== Story Scraping ===")
    stories = await scrape_all_stories(min_score=500)
    logger.info("Total stories: %d", len(stories))
    for i, s in enumerate(stories[:5], 1):
        logger.info(
            "  %d. [%d] r/%s: %s (%d words)",
            i, s["score"], s["subreddit"], s["title"][:60], s["word_count"],
        )


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())
