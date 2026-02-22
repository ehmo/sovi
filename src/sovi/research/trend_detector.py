"""Trend detection aggregator — combines signals from multiple sources."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sovi.config import load_niche_config
from sovi.models import Platform, TopicCandidate

logger = logging.getLogger(__name__)


@dataclass
class TrendSignal:
    """A single trend signal from any platform."""

    topic: str
    platform: Platform
    score: float
    source: str  # "tiktok_creative_center", "reddit_rising", "exploding_topics", etc.
    hashtags: list[str] = field(default_factory=list)
    url: str | None = None


async def scan_niche_trends(niche_slug: str) -> list[TopicCandidate]:
    """Aggregate trend signals from all sources for a given niche."""
    config = load_niche_config(niche_slug)
    candidates: list[TopicCandidate] = []

    # TikTok trending hashtags
    try:
        from sovi.research.scrapers.tiktok import fetch_trending_hashtags

        hashtags = await fetch_trending_hashtags()
        niche_keywords = [p.lower() for p in config.get("content_pillars", [])]

        for h in hashtags:
            tag = h["hashtag"].lower()
            if any(kw in tag for kw in niche_keywords):
                candidates.append(TopicCandidate(
                    topic=h["hashtag"],
                    niche_slug=niche_slug,
                    platform=Platform.TIKTOK,
                    trend_score=h.get("trend_score", 0),
                ))
    except Exception:
        logger.warning("TikTok trend scan failed for %s", niche_slug, exc_info=True)

    # Reddit rising (async version)
    try:
        from sovi.research.scrapers.reddit import scrape_rising

        reddit_config = config.get("platforms", {}).get("reddit", {})
        for sub_config in reddit_config.get("subreddits", []):
            posts = await scrape_rising(sub_config["name"], limit=10)
            for post in posts:
                if post["score"] >= 50:
                    candidates.append(TopicCandidate(
                        topic=post["title"],
                        niche_slug=niche_slug,
                        platform=Platform.REDDIT,
                        source_url=post["permalink"],
                        trend_score=float(post["score"]),
                    ))
    except Exception:
        logger.warning("Reddit trend scan failed for %s", niche_slug, exc_info=True)

    # Sort by trend score descending
    candidates.sort(key=lambda c: c.trend_score, reverse=True)
    return candidates


async def scan_all_niches() -> dict[str, list[TopicCandidate]]:
    """Scan trends for all configured niches."""
    from sovi.config import load_all_niche_configs

    configs = load_all_niche_configs()
    results: dict[str, list[TopicCandidate]] = {}

    for slug in configs:
        try:
            candidates = await scan_niche_trends(slug)
            results[slug] = candidates
            logger.info("Niche %s: %d trend candidates", slug, len(candidates))
        except Exception:
            logger.warning("Failed to scan niche %s", slug, exc_info=True)
            results[slug] = []

    return results
