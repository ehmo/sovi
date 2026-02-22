"""Unified video production runner — topic to finished video.

Orchestrates: topic selection → script gen → asset gen → assembly → QC → export.

Usage:
    # Produce from a specific topic
    python -m sovi.production.produce_video --topic "3 AI tools for budgeting" --niche personal_finance

    # Produce from latest trending topic in DB
    python -m sovi.production.produce_video --from-db --niche personal_finance

    # Produce a Reddit story video
    python -m sovi.production.produce_video --reddit-story --niche true_crime
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from uuid import UUID, uuid4

from sovi import db
from sovi.config import settings
from sovi.models import (
    ContentFormat,
    GeneratedScript,
    HookCategory,
    Platform,
    ScriptRequest,
    TopicCandidate,
    VideoTier,
)

logger = logging.getLogger(__name__)

# Mapping of format to factory function import path
FORMAT_FACTORIES = {
    ContentFormat.FACELESS: "sovi.production.formats.faceless:produce_faceless_narration",
    ContentFormat.REDDIT_STORY: "sovi.production.formats.reddit_story:produce_reddit_story",
    ContentFormat.CAROUSEL: "sovi.production.formats.carousel:produce_carousel",
}


async def select_topic_from_db(
    niche_slug: str | None = None,
    platform: str = "tiktok",
    limit: int = 1,
) -> list[dict]:
    """Select top trending topics from database that haven't been used yet."""
    await db.init_pool()

    niche_sub = "(SELECT id FROM niches WHERE slug = %s LIMIT 1)"
    query = f"""
        SELECT t.id, t.platform, t.topic_text, t.trend_score,
               t.hashtag, t.detected_at
        FROM trending_topics t
        LEFT JOIN content c ON c.topic = t.topic_text AND c.niche_id = {niche_sub}
        WHERE c.id IS NULL
    """
    params: list = [niche_slug or "general"]

    if niche_slug:
        query += f" AND t.niche_id = {niche_sub}"
        params.append(niche_slug)

    query += " ORDER BY t.trend_score DESC, t.discovered_at DESC LIMIT %s"
    params.append(limit)

    return await db.execute(query, tuple(params))


async def produce_video(
    topic: str,
    niche_slug: str,
    platform: str = "tiktok",
    content_format: str = "faceless",
    tier: str = "low_mid",
    duration_s: int = 45,
    use_elevenlabs: bool = False,
    output_dir: str | None = None,
) -> dict:
    """Full production pipeline: topic → script → assets → assembly → QC → export.

    Returns dict with all production artifacts and metadata.
    """
    await db.init_pool()
    output_dir = output_dir or str(settings.output_dir)
    content_id = uuid4()
    video_tier = VideoTier(tier)

    logger.info("=== Starting production: %s ===", topic[:60])
    logger.info("Niche: %s | Platform: %s | Format: %s | Tier: %s",
                niche_slug, platform, content_format, tier)

    result: dict = {
        "content_id": str(content_id),
        "topic": topic,
        "niche_slug": niche_slug,
        "platform": platform,
        "content_format": content_format,
        "tier": tier,
    }

    # Step 1: Generate script
    logger.info("[1/5] Generating script...")
    from sovi.production.scriptwriter import generate_script_for_topic

    script = await generate_script_for_topic(
        topic=topic,
        niche_slug=niche_slug,
        platform=platform,
        duration_s=duration_s,
        content_format=content_format,
    )
    result["script"] = {
        "hook": script.hook_text,
        "body": script.body_text[:200],
        "cta": script.cta_text,
        "word_count": script.word_count,
        "estimated_duration_s": script.estimated_duration_s,
        "hook_category": script.hook_category.value,
    }
    logger.info("  Script: %d words, ~%.0fs, hook: %s",
                script.word_count, script.estimated_duration_s, script.hook_category.value)

    # Step 2: Produce format-specific content
    fmt = ContentFormat(content_format)
    logger.info("[2/5] Producing %s format...", fmt.value)

    if fmt == ContentFormat.FACELESS:
        from sovi.production.formats.faceless import produce_faceless_narration

        production = await produce_faceless_narration(
            script=script,
            tier=video_tier,
            use_elevenlabs=use_elevenlabs,
            output_dir=output_dir,
        )
    elif fmt == ContentFormat.REDDIT_STORY:
        # Reddit stories handle their own script adaptation
        from sovi.production.formats.reddit_story import produce_reddit_story

        production = await produce_reddit_story(
            story={"title": topic, "selftext": script.full_text},
            output_dir=output_dir,
        )
    else:
        raise ValueError(f"Format {fmt.value} not yet implemented in production runner")

    result["production"] = {
        "video_path": production.get("video_path"),
        "total_cost_usd": production.get("total_cost_usd", 0),
        "duration_s": production.get("duration_s", 0),
    }
    logger.info("  Video: %s | Cost: $%.4f | Duration: %.1fs",
                production.get("video_path"), production.get("total_cost_usd", 0),
                production.get("duration_s", 0))

    # Step 3: Quality check
    logger.info("[3/5] Running quality checks...")
    from sovi.production.quality import check_video_quality

    video_path = production["video_path"]
    qc = await check_video_quality(video_path, platform)
    result["quality"] = {
        "passed": qc.passed,
        "score": qc.score,
        "blocking_failures": qc.blocking_failures,
    }
    logger.info("  QC: %s (score: %.3f)", "PASS" if qc.passed else "FAIL", qc.score)
    if qc.blocking_failures:
        for f in qc.blocking_failures:
            logger.warning("  BLOCKING: %s", f)

    # Step 4: Platform exports
    logger.info("[4/5] Exporting for platforms...")
    from sovi.production.assembly import export_for_platform

    exports = {}
    target_platforms = [platform]
    for plat in target_platforms:
        export_path = await export_for_platform(
            video_path, plat, output_dir=f"{output_dir}/exports",
        )
        exports[plat] = export_path
        size_mb = Path(export_path).stat().st_size / (1024 * 1024)
        logger.info("  %s: %s (%.2f MB)", plat, export_path, size_mb)
    result["exports"] = exports

    # Step 5: Persist to database
    logger.info("[5/5] Saving to database...")
    niche_row = await db.execute_one(
        "SELECT id FROM niches WHERE slug = %s", (niche_slug,),
    )
    niche_id = niche_row["id"] if niche_row else None

    if not niche_id:
        logger.warning("Niche '%s' not found in DB, skipping DB save", niche_slug)
    else:
        # Python enum values match DB enum values directly
        db_format = content_format

        # quality_score in DB is 0-10 scale, QC returns 0-1
        db_quality = round(qc.score * 10, 2)

        file_paths_json = json.dumps({
            "video": video_path,
            "exports": exports,
            "captions": production.get("caption_path"),
        })

        await db.execute(
            """INSERT INTO content
               (id, niche_id, topic, script_text, hook_id, content_format,
                production_status, quality_score, cost_usd, duration_seconds,
                file_paths)
               VALUES (%s, %s, %s, %s, %s, %s::content_format,
                       %s::production_status, %s, %s, %s, %s::jsonb)""",
            (
                str(content_id),
                str(niche_id),
                topic,
                script.full_text,
                str(script.hook_template_id) if script.hook_template_id else None,
                db_format,
                "complete" if qc.passed else "failed",
                db_quality,
                production.get("total_cost_usd", 0),
                production.get("duration_s", 0),
                file_paths_json,
            ),
        )
        logger.info("  Saved content %s", content_id)

    result["status"] = "complete" if qc.passed else "failed"

    logger.info("\n=== Production Complete ===")
    logger.info("Content ID: %s", content_id)
    logger.info("Video: %s", video_path)
    logger.info("Status: %s", result["status"])

    return result


async def produce_from_db(
    niche_slug: str,
    platform: str = "tiktok",
    content_format: str = "faceless",
    tier: str = "low_mid",
    duration_s: int = 45,
) -> dict:
    """Select a trending topic from DB and produce a video."""
    topics = await select_topic_from_db(niche_slug=niche_slug, platform=platform)
    if not topics:
        logger.warning("No unused trending topics found for niche: %s", niche_slug)
        return {"status": "no_topics"}

    topic = topics[0]
    logger.info("Selected topic: %s (score: %s)", topic["topic_text"], topic.get("trend_score"))

    return await produce_video(
        topic=topic["topic_text"],
        niche_slug=niche_slug,
        platform=platform,
        content_format=content_format,
        tier=tier,
        duration_s=duration_s,
    )


async def _main() -> None:
    parser = argparse.ArgumentParser(description="SOVI Video Production Runner")
    parser.add_argument("--topic", help="Topic text to produce")
    parser.add_argument("--niche", default="personal_finance", help="Niche slug")
    parser.add_argument("--platform", default="tiktok", help="Target platform")
    parser.add_argument("--format", default="faceless",
                        dest="content_format", help="Content format")
    parser.add_argument("--tier", default="low_mid", help="Video quality tier")
    parser.add_argument("--duration", type=int, default=45, help="Target duration (seconds)")
    parser.add_argument("--from-db", action="store_true", help="Select topic from trending DB")
    parser.add_argument("--elevenlabs", action="store_true", help="Use ElevenLabs for VO")
    args = parser.parse_args()

    try:
        if args.from_db:
            result = await produce_from_db(
                niche_slug=args.niche,
                platform=args.platform,
                content_format=args.content_format,
                tier=args.tier,
                duration_s=args.duration,
            )
        elif args.topic:
            result = await produce_video(
                topic=args.topic,
                niche_slug=args.niche,
                platform=args.platform,
                content_format=args.content_format,
                tier=args.tier,
                duration_s=args.duration,
                use_elevenlabs=args.elevenlabs,
            )
        else:
            parser.error("Either --topic or --from-db is required")
            return

        print(json.dumps(result, indent=2, default=str))

    finally:
        await db.close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(_main())
