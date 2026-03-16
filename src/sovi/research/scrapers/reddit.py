"""Reddit scraper — public JSON endpoints (no API key) + optional PRAW.

Uses Reddit's public .json suffix on URLs which works without authentication
for ~60-80 requests before rate limiting. Falls back to PRAW when configured.
Stores results in the trending_topics table.
"""

from __future__ import annotations

import asyncio
import logging
import random

import httpx

from sovi.config import load_all_niche_configs, settings
from sovi.research.scrapers import random_ua

logger = logging.getLogger(__name__)

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
    return {"User-Agent": random_ua()}


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
            await asyncio.sleep(random.uniform(1.0, 2.5))
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
                await asyncio.sleep(random.uniform(1.0, 2.0))
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    logger.warning("Rate limited on r/%s, waiting 30s", name)
                    await asyncio.sleep(30)
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
    from sovi.research.scrapers import save_trending_to_db as _save

    # Normalize reddit post dicts to the shared schema
    items = [
        {
            "topic_text": p["title"][:500],
            "hashtag": f"r/{p['subreddit']}",
            "trend_score": float(p["score"]),
            "niche_slug": p.get("niche_slug"),
        }
        for p in posts
    ]
    return _save(items, platform)


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
