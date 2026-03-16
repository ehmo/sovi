"""Shared utilities for research scrapers."""

from __future__ import annotations

import random

# Common user-agent pool for web scraping
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
]


def random_ua() -> str:
    """Return a random user-agent string."""
    return random.choice(USER_AGENTS)


def save_trending_to_db(
    items: list[dict],
    platform: str,
    *,
    topic_key: str = "topic_text",
    hashtag_key: str = "hashtag",
    score_key: str = "trend_score",
    niche_key: str = "niche_slug",
) -> int:
    """Upsert scraped items into the trending_topics table.

    Each item dict should have keys matching the *_key params.
    Returns the number of rows inserted/updated.
    """
    import psycopg

    from sovi.config import settings

    if not items:
        return 0

    niche_ids: dict[str, str] = {}
    count = 0

    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, slug FROM niches")
            for row in cur.fetchall():
                niche_ids[row[1]] = str(row[0])

            for item in items:
                niche_slug = item.get(niche_key)
                niche_id = niche_ids.get(niche_slug) if niche_slug else None

                cur.execute(
                    """INSERT INTO trending_topics
                       (platform, topic_text, hashtag, trend_score, niche_id, detected_at, is_active)
                       VALUES (%s, %s, %s, %s, %s, now(), TRUE)
                       ON CONFLICT (platform, topic_text, niche_id)
                       WHERE is_active = true
                       DO UPDATE SET
                           trend_score = GREATEST(trending_topics.trend_score, EXCLUDED.trend_score),
                           hashtag = EXCLUDED.hashtag,
                           detected_at = now()""",
                    (
                        platform,
                        item.get(topic_key, ""),
                        item.get(hashtag_key, ""),
                        float(item.get(score_key, 0)),
                        niche_id,
                    ),
                )
                count += 1
            conn.commit()
    return count
