"""Faceless Narration format factory — VO + stock/AI visuals + animated captions."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from sovi.models import (
    ContentFormat,
    GeneratedAsset,
    GeneratedScript,
    Platform,
    VideoTier,
)
from sovi.production.assets.transcription import transcribe, words_to_ass


async def produce_faceless_narration(
    script: GeneratedScript,
    tier: VideoTier = VideoTier.BUDGET,
    use_elevenlabs: bool = True,
    voice_id: str | None = None,
    output_dir: str = "output",
) -> dict:
    """Full faceless narration production pipeline.

    Returns dict with paths to all produced assets and the assembled video.
    """
    from sovi.production.assets.voice_gen import generate_voiceover
    from sovi.production.assets.image_gen import generate_images_batch
    from sovi.production.assembly import assemble_faceless_narration, post_process_anti_detection

    # 1. Generate voiceover
    voiceover = await generate_voiceover(
        script.full_text,
        voice_id=voice_id,
        use_elevenlabs=use_elevenlabs,
        output_dir=f"{output_dir}/voiceovers",
    )

    # 2. Generate visuals based on tier
    image_prompts = [
        f"Cinematic, dramatic visual representing: {script.hook_text}. 9:16 vertical, moody lighting.",
        f"Cinematic visual representing: {script.body_text[:200]}. 9:16 vertical, high quality.",
    ]
    images = await generate_images_batch(
        image_prompts,
        tier=tier,
        output_dir=f"{output_dir}/images",
    )

    # 3. Transcribe for word-level captions
    transcript = await transcribe(voiceover.file_path)

    # 4. Generate ASS captions
    ass_content = words_to_ass(transcript["words"])
    ass_path = f"{output_dir}/captions/{uuid4().hex[:12]}.ass"
    Path(ass_path).parent.mkdir(parents=True, exist_ok=True)
    Path(ass_path).write_text(ass_content)

    # 5. Select background music
    from sovi.production.assets.music import select_background_music
    music_path = select_background_music(emotional_tone="engaging")

    # 6. Assemble video
    video_path = await assemble_faceless_narration(
        voiceover_path=voiceover.file_path,
        image_paths=[img.file_path for img in images],
        music_path=music_path,
        caption_ass_path=ass_path,
        duration_s=transcript["duration_s"],
        output_dir=f"{output_dir}/assembled",
    )

    # 7. Anti-detection post-processing
    final_path = await post_process_anti_detection(video_path)

    total_cost = voiceover.cost_usd + sum(img.cost_usd for img in images)

    return {
        "format": ContentFormat.FACELESS.value,
        "video_path": final_path,
        "voiceover": voiceover,
        "images": images,
        "transcript": transcript,
        "caption_path": ass_path,
        "total_cost_usd": total_cost,
        "duration_s": transcript["duration_s"],
    }
