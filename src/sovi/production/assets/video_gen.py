"""Tiered video generation via fal.ai (Hailuo, Kling, etc.)."""

from __future__ import annotations

import fal_client

from sovi.config import settings
from sovi.models import GeneratedAsset, VideoTier

# Model endpoints and cost per second (verified Feb 2026)
VIDEO_MODELS: dict[VideoTier, dict] = {
    VideoTier.LOW_MID: {
        "endpoint": "fal-ai/minimax-video/video-01",  # Hailuo 02 Standard
        "cost_per_s": 0.045,
        "max_duration": 6,
        "resolution": "768p",
    },
    VideoTier.MID: {
        "endpoint": "fal-ai/minimax-video/video-01",  # Hailuo 02 Pro
        "cost_per_s": 0.08,
        "max_duration": 6,
        "resolution": "1080p",
    },
    VideoTier.PREMIUM: {
        "endpoint": "fal-ai/kling-video/v2/standard",  # Kling 3.0 Standard
        "cost_per_s": 0.168,
        "max_duration": 15,
        "resolution": "1080p",
    },
    VideoTier.CINEMATIC: {
        "endpoint": "fal-ai/kling-video/v2/pro",  # Kling 3.0 Pro
        "cost_per_s": 0.336,
        "max_duration": 15,
        "resolution": "1080p",
    },
}


def select_tier(requested_tier: VideoTier) -> dict:
    """Get model config for a tier, falling back to LOW_MID."""
    return VIDEO_MODELS.get(requested_tier, VIDEO_MODELS[VideoTier.LOW_MID])


async def generate_video(
    prompt: str,
    duration_s: float = 5.0,
    tier: VideoTier = VideoTier.LOW_MID,
    image_url: str | None = None,
    output_dir: str = "output/videos",
) -> GeneratedAsset:
    """Generate a video clip via the tiered model stack."""
    model_config = select_tier(tier)
    endpoint = model_config["endpoint"]
    max_dur = model_config["max_duration"]
    actual_duration = min(duration_s, max_dur)
    cost = model_config["cost_per_s"] * actual_duration

    arguments: dict = {
        "prompt": prompt,
        "duration": str(int(actual_duration)),
    }

    if image_url:
        arguments["image_url"] = image_url

    result = await fal_client.run_async(endpoint, arguments=arguments)

    video_url = result["video"]["url"]

    import httpx
    from pathlib import Path
    from uuid import uuid4

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    file_path = f"{output_dir}/{uuid4().hex[:12]}.mp4"

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.get(video_url)
        resp.raise_for_status()
        Path(file_path).write_bytes(resp.content)

    return GeneratedAsset(
        asset_type="video_clip",
        file_path=file_path,
        duration_s=actual_duration,
        cost_usd=cost,
        model_used=endpoint,
    )
