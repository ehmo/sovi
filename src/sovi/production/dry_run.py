"""Dry-run production — synthetic assets for full pipeline validation.

Generates placeholder VO (sine wave), images (gradient), and script
to test the entire pipeline (DB → assembly → QC → export → persistence)
without requiring any API keys.

Usage:
    python -m sovi.production.dry_run
    python -m sovi.production.dry_run --niche personal_finance --platform tiktok
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path
from uuid import uuid4

from sovi import db
from sovi.models import (
    ContentFormat,
    GeneratedAsset,
    GeneratedScript,
    HookCategory,
    QualityReport,
)
from sovi.production.assets.transcription import words_to_ass

logger = logging.getLogger(__name__)


def _generate_synthetic_voiceover(text: str, output_dir: str, duration_s: float = 30.0) -> GeneratedAsset:
    """Generate a sine-wave audio file as placeholder VO."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    path = f"{output_dir}/{uuid4().hex[:12]}_dryrun.mp3"

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i",
        f"sine=frequency=440:duration={duration_s}",
        "-c:a", "libmp3lame", "-b:a", "128k",
        path,
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    return GeneratedAsset(asset_type="voiceover", file_path=path, duration_s=duration_s, cost_usd=0.0, model_used="dry_run_sine")


def _generate_synthetic_image(prompt: str, index: int, output_dir: str) -> GeneratedAsset:
    """Generate a gradient image with text overlay as placeholder visual."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    path = f"{output_dir}/{uuid4().hex[:12]}_dryrun.png"

    # Cycle through some colors
    colors = [
        ("0x1a1a2e", "0x16213e"),
        ("0x0f3460", "0x533483"),
        ("0x2b2d42", "0x8d99ae"),
    ]
    c1, c2 = colors[index % len(colors)]

    # Use 9:16 vertical for short-form
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i",
        f"gradients=s=1080x1920:c0={c1}:c1={c2}:duration=1:speed=1",
        "-frames:v", "1",
        "-vf", f"drawtext=text='{prompt[:40]}':fontcolor=white:fontsize=48"
               f":x=(w-text_w)/2:y=(h-text_h)/2:font=Montserrat",
        path,
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        # Fallback: plain color if gradients or drawtext not available
        cmd_fallback = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i",
            f"color=c={c1}:s=1080x1920:d=1",
            "-frames:v", "1",
            path,
        ]
        subprocess.run(cmd_fallback, capture_output=True, check=True)

    return GeneratedAsset(asset_type="image", file_path=path, cost_usd=0.0, model_used="dry_run_gradient")


def _generate_synthetic_script(topic: str, duration_s: float = 30.0) -> GeneratedScript:
    """Generate a template script without calling any API."""
    hook = f"You won't believe what happens when you try {topic[:30]}."
    body = (
        f"Here's what most people get wrong about {topic[:40]}. "
        "First, the common approach is completely backwards. "
        "Second, the data shows a surprising pattern. "
        "Third, the fix is simpler than you think. "
        "And finally, this changes everything about how you should approach it."
    )
    cta = "Follow for more insights like this."
    full = f"{hook} {body} {cta}"
    word_count = len(full.split())

    return GeneratedScript(
        script_id=uuid4(),
        hook_text=hook,
        body_text=body,
        cta_text=cta,
        full_text=full,
        word_count=word_count,
        estimated_duration_s=word_count / 2.5,
        hook_category=HookCategory.CURIOSITY_GAP,
        hook_template_id=None,
    )


def _generate_synthetic_words(script: GeneratedScript) -> list[dict]:
    """Generate fake word-level timestamps for caption testing."""
    words_list = script.full_text.split()
    words_out = []
    t = 0.0
    for w in words_list:
        dur = 0.4  # ~2.5 words per second
        words_out.append({
            "word": w,
            "start": round(t, 2),
            "end": round(t + dur, 2),
            "confidence": 0.99,
        })
        t += dur
    return words_out


async def dry_run_produce(
    topic: str | None = None,
    niche_slug: str = "personal_finance",
    platform: str = "tiktok",
    content_format: str = "faceless",
    duration_s: int = 30,
    output_dir: str | None = None,
) -> dict:
    """Run the full production pipeline with synthetic assets.

    Tests: topic selection, script generation, FFmpeg assembly,
    QC gate, platform export, and DB persistence — all without API keys.
    """
    await db.init_pool()

    from sovi.config import settings
    output_dir = output_dir or str(settings.output_dir / "dry_run")
    content_id = uuid4()

    logger.info("=== DRY RUN: Starting production ===")
    logger.info("Niche: %s | Platform: %s | Format: %s", niche_slug, platform, content_format)

    result: dict = {
        "content_id": str(content_id),
        "dry_run": True,
        "niche_slug": niche_slug,
        "platform": platform,
        "content_format": content_format,
    }

    # Step 1: Select topic from DB (or use provided)
    if not topic:
        logger.info("[1/6] Selecting topic from DB...")
        topics = await db.execute(
            """SELECT t.topic_text, t.trend_score
               FROM trending_topics t
               JOIN niches n ON t.niche_id = n.id
               WHERE n.slug = %s AND t.is_active = true
               ORDER BY t.trend_score DESC LIMIT 1""",
            (niche_slug,),
        )
        if topics:
            topic = topics[0]["topic_text"]
            logger.info("  Topic: %s (score: %s)", topic, topics[0].get("trend_score"))
        else:
            topic = f"Top 5 {niche_slug.replace('_', ' ')} tips for 2026"
            logger.info("  No DB topics, using fallback: %s", topic)
    else:
        logger.info("[1/6] Using provided topic: %s", topic[:60])
    result["topic"] = topic

    # Step 2: Generate synthetic script
    logger.info("[2/6] Generating synthetic script...")
    script = _generate_synthetic_script(topic, duration_s)
    result["script"] = {
        "hook": script.hook_text,
        "body": script.body_text[:100] + "...",
        "cta": script.cta_text,
        "word_count": script.word_count,
        "estimated_duration_s": script.estimated_duration_s,
    }
    logger.info("  Script: %d words, ~%.0fs", script.word_count, script.estimated_duration_s)

    # Step 3: Generate synthetic assets
    logger.info("[3/6] Generating synthetic assets...")
    vo = _generate_synthetic_voiceover(script.full_text, f"{output_dir}/voiceovers", duration_s)
    logger.info("  VO: %s (%.1fs)", vo.file_path, vo.duration_s or 0)

    images = []
    prompts = [
        f"Cinematic visual: {script.hook_text[:50]}",
        f"Cinematic visual: {script.body_text[:50]}",
    ]
    for i, prompt in enumerate(prompts):
        img = _generate_synthetic_image(prompt, i, f"{output_dir}/images")
        images.append(img)
        logger.info("  Image %d: %s", i + 1, img.file_path)

    # Step 4: Generate captions (synthetic word timestamps → ASS)
    logger.info("[4/6] Generating captions...")
    words = _generate_synthetic_words(script)
    ass_content = words_to_ass(words)
    ass_dir = Path(output_dir) / "captions"
    ass_dir.mkdir(parents=True, exist_ok=True)
    ass_path = str(ass_dir / f"{uuid4().hex[:12]}.ass")
    Path(ass_path).write_text(ass_content)
    logger.info("  Captions: %d word groups, ASS: %s", len(words), ass_path)

    # Step 5: Assemble video
    logger.info("[5/6] Assembling video with FFmpeg...")
    from sovi.production.assembly import assemble_faceless_narration, post_process_anti_detection

    video_path = await assemble_faceless_narration(
        voiceover_path=vo.file_path,
        image_paths=[img.file_path for img in images],
        music_path=None,  # No music in dry run
        caption_ass_path=ass_path,
        duration_s=duration_s,
        output_dir=f"{output_dir}/assembled",
    )
    logger.info("  Assembled: %s", video_path)

    # Anti-detection post-processing
    final_path = await post_process_anti_detection(video_path)
    logger.info("  Post-processed: %s", final_path)

    result["production"] = {
        "video_path": final_path,
        "total_cost_usd": 0.0,
        "duration_s": duration_s,
    }

    # Step 6: Quality check
    logger.info("[6/6] Running quality checks...")
    from sovi.production.quality import check_video_quality

    qc = await check_video_quality(final_path, platform)
    result["quality"] = {
        "passed": qc.passed,
        "score": qc.score,
        "blocking_failures": qc.blocking_failures,
    }
    logger.info("  QC: %s (score: %.3f)", "PASS" if qc.passed else "FAIL", qc.score)
    if qc.blocking_failures:
        for f in qc.blocking_failures:
            logger.warning("  BLOCKING: %s", f)

    # Platform export
    from sovi.production.assembly import export_for_platform

    export_path = await export_for_platform(final_path, platform, f"{output_dir}/exports")
    size_mb = Path(export_path).stat().st_size / (1024 * 1024)
    result["export"] = {"path": export_path, "size_mb": round(size_mb, 2)}
    logger.info("  Export: %s (%.2f MB)", export_path, size_mb)

    # Persist to DB
    niche_row = await db.execute_one("SELECT id FROM niches WHERE slug = %s", (niche_slug,))
    if niche_row:
        niche_id = niche_row["id"]
        db_quality = round(qc.score * 10, 2)
        file_paths_json = json.dumps({
            "video": final_path,
            "exports": {platform: export_path},
            "captions": ass_path,
            "dry_run": True,
        })

        await db.execute(
            """INSERT INTO content
               (id, niche_id, topic, script_text, content_format,
                production_status, quality_score, cost_usd, duration_seconds,
                file_paths)
               VALUES (%s, %s, %s, %s, %s::content_format,
                       %s::production_status, %s, %s, %s, %s::jsonb)""",
            (
                str(content_id),
                str(niche_id),
                f"[DRY RUN] {topic}",
                script.full_text,
                content_format,
                "complete" if qc.passed else "failed",
                db_quality,
                0.0,
                duration_s,
                file_paths_json,
            ),
        )
        logger.info("  DB: Saved content %s", content_id)
    else:
        logger.warning("  DB: Niche '%s' not found, skipping persistence", niche_slug)

    result["status"] = "complete" if qc.passed else "failed"

    # Verify DB round-trip
    if niche_row:
        check = await db.execute_one(
            "SELECT id, topic, production_status, quality_score FROM content WHERE id = %s",
            (str(content_id),),
        )
        if check:
            logger.info("  DB verify: id=%s status=%s quality=%.1f",
                        check["id"], check["production_status"], check["quality_score"])
            result["db_verified"] = True
        else:
            logger.error("  DB verify: FAILED — content not found after insert!")
            result["db_verified"] = False

    logger.info("\n=== DRY RUN COMPLETE ===")
    logger.info("Status: %s", result["status"])
    logger.info("Video: %s", final_path)
    logger.info("Export: %s (%.2f MB)", export_path, size_mb)

    return result


async def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="SOVI Dry Run — Pipeline Validation")
    parser.add_argument("--topic", help="Topic text (default: from DB)")
    parser.add_argument("--niche", default="personal_finance", help="Niche slug")
    parser.add_argument("--platform", default="tiktok", help="Target platform")
    parser.add_argument("--format", default="faceless", dest="content_format")
    parser.add_argument("--duration", type=int, default=30, help="Target duration (seconds)")
    args = parser.parse_args()

    try:
        result = await dry_run_produce(
            topic=args.topic,
            niche_slug=args.niche,
            platform=args.platform,
            content_format=args.content_format,
            duration_s=args.duration,
        )
        print("\n" + json.dumps(result, indent=2, default=str))
    finally:
        await db.close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(_main())
