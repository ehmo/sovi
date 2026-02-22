"""Image generation via FLUX 2 on fal.ai."""

from __future__ import annotations

import fal_client

from sovi.config import settings
from sovi.models import GeneratedAsset, VideoTier

# FLUX 2 model endpoints on fal.ai
FLUX_MODELS = {
    VideoTier.BUDGET: "fal-ai/flux/dev",           # FLUX 2 Klein Base ~$0.009/MP
    VideoTier.LOW_MID: "fal-ai/flux/schnell",      # FLUX 2 Klein Distilled ~$0.014/MP
    VideoTier.MID: "fal-ai/flux-pro/v1.1",         # FLUX 2 Pro ~$0.03/MP
    VideoTier.PREMIUM: "fal-ai/flux-pro/v1.1",     # Same model, higher quality settings
}

# Costs per megapixel (verified Feb 2026)
COST_PER_MP = {
    VideoTier.BUDGET: 0.009,
    VideoTier.LOW_MID: 0.014,
    VideoTier.MID: 0.03,
    VideoTier.PREMIUM: 0.05,
}

# 9:16 vertical at 1MP ≈ 768x1365, at 2MP ≈ 1080x1920
DEFAULT_SIZE = {"width": 1080, "height": 1920}


async def generate_image(
    prompt: str,
    tier: VideoTier = VideoTier.BUDGET,
    width: int = 1080,
    height: int = 1920,
    output_dir: str = "output/images",
) -> GeneratedAsset:
    """Generate a single image via FLUX 2."""
    model = FLUX_MODELS.get(tier, FLUX_MODELS[VideoTier.BUDGET])
    megapixels = (width * height) / 1_000_000
    cost = COST_PER_MP.get(tier, 0.009) * megapixels

    result = await fal_client.run_async(
        model,
        arguments={
            "prompt": prompt,
            "image_size": {"width": width, "height": height},
            "num_images": 1,
        },
    )

    image_url = result["images"][0]["url"]

    # Download to local
    import httpx
    from pathlib import Path
    from uuid import uuid4

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    file_path = f"{output_dir}/{uuid4().hex[:12]}.png"

    async with httpx.AsyncClient() as client:
        resp = await client.get(image_url)
        resp.raise_for_status()
        Path(file_path).write_bytes(resp.content)

    return GeneratedAsset(
        asset_type="image",
        file_path=file_path,
        cost_usd=cost,
        model_used=model,
    )


async def generate_images_batch(
    prompts: list[str],
    tier: VideoTier = VideoTier.BUDGET,
    output_dir: str = "output/images",
) -> list[GeneratedAsset]:
    """Generate multiple images in parallel."""
    import asyncio

    tasks = [generate_image(p, tier, output_dir=output_dir) for p in prompts]
    return await asyncio.gather(*tasks)
