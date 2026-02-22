"""Temporal activity definitions — external API calls wrapped as activities."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

from temporalio import activity

from sovi.models import (
    ContentFormat,
    DistributionRequest,
    EngagementSnapshot,
    GeneratedAsset,
    GeneratedScript,
    Platform,
    PlatformExport,
    QualityReport,
    ScriptRequest,
    TopicCandidate,
    VideoTier,
)


# === Research Activities ===


@activity.defn
async def scan_trends(niche_slug: str) -> list[TopicCandidate]:
    """Scan platforms for trending topics in a niche."""
    activity.logger.info("Scanning trends for niche=%s", niche_slug)
    from sovi.research.trend_detector import scan_niche_trends

    return await scan_niche_trends(niche_slug)


# === Script Activities ===


@activity.defn
async def generate_script(request: ScriptRequest) -> GeneratedScript:
    """Generate a video script via Claude API."""
    activity.logger.info("Generating script for topic=%s", request.topic.topic)
    from sovi.hooks.selector import select_hook_template
    from sovi.production.scriptwriter import generate_script as _gen

    # Select hook template for this niche/platform
    hook = await select_hook_template(
        niche_slug=request.topic.niche_slug,
        platform=request.target_platforms[0].value if request.target_platforms else None,
    )
    return await _gen(request, hook_template=hook)


@activity.defn
async def select_hook(niche_slug: str, platform: str, category: str | None = None) -> UUID | None:
    """Select a hook template using Thompson Sampling."""
    activity.logger.info("Selecting hook for niche=%s platform=%s", niche_slug, platform)
    from sovi.hooks.selector import select_hook_template

    result = await select_hook_template(niche_slug, platform, category)
    if result and result.get("id"):
        return UUID(str(result["id"]))
    return None


# === Asset Generation Activities ===


@activity.defn
async def generate_voiceover(script_text: str, voice_id: str | None = None) -> GeneratedAsset:
    """Generate voiceover via ElevenLabs or OpenAI TTS."""
    activity.heartbeat()
    activity.logger.info("Generating voiceover, len=%d chars", len(script_text))
    from sovi.config import settings
    from sovi.production.assets.voice_gen import generate_voiceover as _gen_vo

    use_elevenlabs = bool(settings.elevenlabs_api_key)
    return await _gen_vo(
        script_text,
        voice_id=voice_id,
        use_elevenlabs=use_elevenlabs,
    )


@activity.defn
async def generate_images(prompts: list[str], tier: str = "budget") -> list[GeneratedAsset]:
    """Generate images via FLUX 2 on fal.ai."""
    activity.heartbeat()
    activity.logger.info("Generating %d images at tier=%s", len(prompts), tier)
    from sovi.production.assets.image_gen import generate_images_batch

    return await generate_images_batch(prompts, tier=VideoTier(tier))


@activity.defn
async def generate_video_clip(prompt: str, duration_s: float = 5.0, tier: str = "low_mid") -> GeneratedAsset:
    """Generate a video clip via tiered model selection on fal.ai."""
    activity.heartbeat()
    activity.logger.info("Generating video clip tier=%s duration=%.1fs", tier, duration_s)
    from sovi.production.assets.video_gen import generate_video

    return await generate_video(prompt, duration_s=duration_s, tier=VideoTier(tier))


@activity.defn
async def transcribe_audio(audio_path: str) -> dict:
    """Transcribe audio via Deepgram Nova-3, returning word-level timestamps."""
    activity.logger.info("Transcribing %s", audio_path)
    from sovi.production.assets.transcription import transcribe

    return await transcribe(audio_path)


@activity.defn
async def select_background_music(mood: str, duration_s: float) -> GeneratedAsset:
    """Select background music from pre-generated library."""
    activity.logger.info("Selecting music mood=%s duration=%.1fs", mood, duration_s)
    from sovi.production.assets.music import select_background_music as _select

    path = _select(mood=mood)
    return GeneratedAsset(
        asset_type="music",
        file_path=path or "",
        duration_s=duration_s,
        cost_usd=0.0,
        model_used="library",
    )


# === Assembly Activities ===


@activity.defn
async def assemble_video(
    assets: list[GeneratedAsset],
    transcript: dict,
    format_type: str,
    duration_s: float,
) -> str:
    """Assemble final video with FFmpeg (VO + visuals + captions + music)."""
    activity.heartbeat()
    activity.logger.info("Assembling video format=%s", format_type)
    from sovi.production.assembly import (
        assemble_faceless_narration,
        post_process_anti_detection,
    )
    from sovi.production.assets.transcription import words_to_ass

    # Extract assets by type
    voiceover = next((a for a in assets if a.asset_type == "voiceover"), None)
    images = [a for a in assets if a.asset_type == "image"]
    music = next((a for a in assets if a.asset_type == "music"), None)

    if not voiceover:
        raise RuntimeError("No voiceover asset found")

    # Generate ASS captions from transcript
    ass_path = None
    words = transcript.get("words", [])
    if words:
        ass_content = words_to_ass(words)
        ass_dir = Path("output/captions")
        ass_dir.mkdir(parents=True, exist_ok=True)
        ass_path = str(ass_dir / f"{uuid4().hex[:12]}.ass")
        Path(ass_path).write_text(ass_content)

    music_path = music.file_path if music and music.file_path else None

    video_path = await assemble_faceless_narration(
        voiceover_path=voiceover.file_path,
        image_paths=[img.file_path for img in images],
        music_path=music_path,
        caption_ass_path=ass_path,
        duration_s=duration_s,
    )

    activity.heartbeat()
    final_path = await post_process_anti_detection(video_path)
    return final_path


@activity.defn
async def export_for_platform(video_path: str, platform: str) -> PlatformExport:
    """Export video with platform-specific specs via FFmpeg."""
    activity.logger.info("Exporting for platform=%s", platform)
    from sovi.production.assembly import export_for_platform as _export

    exported_path = await _export(video_path, platform)
    return PlatformExport(
        platform=Platform(platform),
        file_path=exported_path,
    )


# === Quality Activities ===


@activity.defn
async def quality_check(video_path: str, platform: str) -> QualityReport:
    """Run automated quality checks on assembled video."""
    activity.logger.info("Quality checking %s for %s", video_path, platform)
    from sovi.production.quality import check_video_quality

    return await check_video_quality(video_path, platform)


# === Distribution Activities ===


@activity.defn
async def distribute(request: DistributionRequest) -> dict:
    """Post content to a platform via Late API."""
    activity.logger.info(
        "Distributing content_id=%s to %s via account=%s",
        request.content_id, request.platform, request.account_id,
    )
    from sovi.distribution.poster import post_via_late

    return await post_via_late(request)


# === Analytics Activities ===


@activity.defn
async def collect_metrics(distribution_id: UUID, platform: str) -> EngagementSnapshot:
    """Collect engagement metrics for a distributed piece of content."""
    activity.logger.info("Collecting metrics for distribution=%s", distribution_id)
    from sovi.distribution.poster import get_post_analytics

    data = await get_post_analytics(str(distribution_id))
    return EngagementSnapshot(
        distribution_id=distribution_id,
        views=data.get("views", 0),
        likes=data.get("likes", 0),
        comments=data.get("comments", 0),
        shares=data.get("shares", 0),
        saves=data.get("saves", 0),
    )


@activity.defn
async def generate_daily_report(date: str) -> dict:
    """Generate end-of-day analytics report."""
    activity.logger.info("Generating daily report for %s", date)
    from sovi import db

    # Summarize the day's production
    content_stats = await db.execute_one("""
        SELECT COUNT(*) as total,
               COUNT(*) FILTER (WHERE production_status = 'complete') as completed,
               COUNT(*) FILTER (WHERE production_status = 'failed') as failed,
               COALESCE(SUM(cost_usd), 0) as total_cost
        FROM content
        WHERE created_at::date = %s::date
    """, (date,))

    distribution_stats = await db.execute_one("""
        SELECT COUNT(*) as total,
               COUNT(*) FILTER (WHERE status = 'posted') as posted,
               COUNT(*) FILTER (WHERE status = 'failed') as failed
        FROM distributions
        WHERE created_at::date = %s::date
    """, (date,))

    return {
        "date": date,
        "content": dict(content_stats) if content_stats else {},
        "distribution": dict(distribution_stats) if distribution_stats else {},
    }
