"""TikTok trend discovery — multiple fallback sources.

Sources (in priority order):
1. TikTok Creative Center API (requires Business account cookies)
2. TikTok web search autocomplete (public)
3. Google Trends suggestions for TikTok-adjacent topics
"""

from __future__ import annotations

import logging
import random
import time

import httpx

from sovi.config import load_all_niche_configs, settings

logger = logging.getLogger(__name__)

_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
]


# ---------------------------------------------------------------------------
# TikTok Creative Center (requires auth — may fail)
# ---------------------------------------------------------------------------


async def fetch_creative_center_hashtags(country: str = "US", limit: int = 50) -> list[dict]:
    """Fetch from Creative Center API. Requires session cookies; may return empty/403."""
    url = "https://ads.tiktok.com/creative_radar_api/v1/popular_trend/hashtag/list"
    params = {"page": 1, "limit": limit, "country_code": country, "period": 7}

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, params=params, headers={"User-Agent": random.choice(_USER_AGENTS)})
        data = resp.json()

    if data.get("code") != 0:
        logger.debug("Creative Center returned code %s: %s", data.get("code"), data.get("msg"))
        return []

    return [
        {
            "hashtag": item.get("hashtag_name", ""),
            "video_count": item.get("video_count", 0),
            "view_count": item.get("view_count", 0),
            "trend_score": item.get("trend_value", 0),
            "is_promoted": item.get("is_promoted", False),
            "source": "creative_center",
        }
        for item in data.get("data", {}).get("list", [])
    ]


# ---------------------------------------------------------------------------
# TikTok search suggest (public, no auth)
# ---------------------------------------------------------------------------


async def fetch_google_autocomplete(query: str) -> list[str]:
    """Get Google search autocomplete suggestions for a query."""
    url = "https://www.google.com/complete/search"
    params = {"q": query, "client": "firefox"}
    headers = {"User-Agent": random.choice(_USER_AGENTS)}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code != 200:
                return []
            data = resp.json()
        # Response format: [query, [suggestions], ...]
        return data[1] if len(data) > 1 else []
    except Exception:
        return []


async def fetch_niche_tiktok_suggestions(niche_slug: str | None = None) -> list[dict]:
    """Get trending suggestions for niche topics via Google Autocomplete.

    Queries "tiktok <pillar>" and "<pillar> trending" to find what's hot.
    """
    configs = load_all_niche_configs()
    if niche_slug:
        configs = {k: v for k, v in configs.items() if k == niche_slug}

    results: list[dict] = []
    seen: set[str] = set()

    for slug, cfg in configs.items():
        pillars = cfg.get("content_pillars", [])
        # Use first 3 pillars to avoid too many requests
        for pillar in pillars[:3]:
            clean = pillar.replace("_", " ")
            queries = [f"tiktok {clean}", f"{clean} trending 2026"]

            for q in queries:
                suggestions = await fetch_google_autocomplete(q)
                for s in suggestions:
                    if s.lower() not in seen:
                        seen.add(s.lower())
                        results.append({
                            "hashtag": s,
                            "source": "google_autocomplete",
                            "niche_slug": slug,
                            "trend_score": 2.0,
                        })
                time.sleep(random.uniform(0.3, 0.8))

    return results


# ---------------------------------------------------------------------------
# Unified scraper — tries all sources
# ---------------------------------------------------------------------------


async def scrape_tiktok_trends() -> list[dict]:
    """Scrape TikTok trends from all available sources."""
    all_trends: list[dict] = []

    # Try Creative Center first (usually fails without auth)
    try:
        cc_hashtags = await fetch_creative_center_hashtags()
        if cc_hashtags:
            all_trends.extend(cc_hashtags)
            logger.info("Creative Center: %d hashtags", len(cc_hashtags))
    except Exception:
        logger.debug("Creative Center unavailable (expected)")

    # Google Autocomplete for niche-relevant TikTok trends
    try:
        suggestions = await fetch_niche_tiktok_suggestions()
        if suggestions:
            all_trends.extend(suggestions)
            logger.info("Google Autocomplete suggestions: %d", len(suggestions))
    except Exception:
        logger.warning("Google Autocomplete failed", exc_info=True)

    return all_trends


# ---------------------------------------------------------------------------
# Database persistence
# ---------------------------------------------------------------------------


def save_trending_to_db(trends: list[dict]) -> int:
    """Save TikTok trending data as trending_topics in the database."""
    import psycopg

    if not trends:
        return 0

    niche_ids: dict[str, str] = {}
    count = 0

    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, slug FROM niches")
            for row in cur.fetchall():
                niche_ids[row[1]] = str(row[0])

            for t in trends:
                niche_slug = t.get("niche_slug")
                niche_id = niche_ids.get(niche_slug) if niche_slug else None

                cur.execute(
                    """INSERT INTO trending_topics
                       (platform, topic_text, hashtag, trend_score, niche_id, detected_at, is_active)
                       VALUES ('tiktok', %s, %s, %s, %s, now(), TRUE)
                       ON CONFLICT (platform, topic_text, niche_id)
                       WHERE is_active = true
                       DO UPDATE SET
                           trend_score = GREATEST(trending_topics.trend_score, EXCLUDED.trend_score),
                           hashtag = EXCLUDED.hashtag,
                           detected_at = now()""",
                    (
                        t.get("hashtag", ""),
                        f"#{t.get('hashtag', '')}",
                        float(t.get("trend_score", 0)),
                        niche_id,
                    ),
                )
                count += 1
            conn.commit()
    return count


# ---------------------------------------------------------------------------
# CLI runner
# ---------------------------------------------------------------------------


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    logger.info("=== TikTok Trend Scraper ===")
    trends = await scrape_tiktok_trends()
    logger.info("Total trends collected: %d", len(trends))

    # Group by source
    by_source: dict[str, int] = {}
    for t in trends:
        src = t.get("source", "unknown")
        by_source[src] = by_source.get(src, 0) + 1
    for src, count in by_source.items():
        logger.info("  %s: %d trends", src, count)

    for i, t in enumerate(trends[:10], 1):
        logger.info("  %d. %s [%s]", i, t.get("hashtag", ""), t.get("source", ""))

    if trends:
        saved = save_trending_to_db(trends)
        logger.info("Saved %d trends to database", saved)


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())
