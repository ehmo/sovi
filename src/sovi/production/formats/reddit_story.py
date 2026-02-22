"""Reddit Story format factory — scrape stories, TTS, gameplay background overlay."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from sovi.models import ContentFormat, GeneratedScript, VideoTier


async def produce_reddit_story(
    story: dict,
    output_dir: str = "output",
) -> dict:
    """Produce a Reddit story video.

    Args:
        story: Dict from reddit scraper with title, selftext, score, etc.
        output_dir: Output directory for produced assets.

    Returns dict with paths to all produced assets.
    """
    from sovi.production.assets.voice_gen import generate_voiceover
    from sovi.production.assets.transcription import transcribe, words_to_ass
    from sovi.production.assembly import assemble_faceless_narration, post_process_anti_detection

    # 1. Adapt story text for TTS (clean up Reddit formatting)
    import anthropic
    from sovi.config import settings

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    response = await client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"""Adapt this Reddit story for a voiceover narration. Keep it engaging,
add a hook at the start, clean up Reddit-specific formatting (edits, TLDRs, etc.),
and keep it under 200 words for a 60-90 second video.

Title: {story['title']}

Story: {story['selftext'][:3000]}""",
        }],
    )
    adapted_text = response.content[0].text

    # 2. Generate TTS (use OpenAI for bulk Reddit stories)
    voiceover = await generate_voiceover(
        adapted_text,
        use_elevenlabs=False,
        output_dir=f"{output_dir}/voiceovers",
    )

    # 3. Transcribe for captions
    transcript = await transcribe(voiceover.file_path)

    # 4. Generate ASS captions
    ass_content = words_to_ass(transcript["words"])
    ass_path = f"{output_dir}/captions/{uuid4().hex[:12]}.ass"
    Path(ass_path).parent.mkdir(parents=True, exist_ok=True)
    Path(ass_path).write_text(ass_content)

    # 5. Assemble with background video (gameplay/satisfying footage)
    # For now, use placeholder images. TODO: integrate stock footage library
    video_path = await assemble_faceless_narration(
        voiceover_path=voiceover.file_path,
        image_paths=[],  # TODO: use background video loop instead
        music_path=None,
        caption_ass_path=ass_path,
        duration_s=transcript["duration_s"],
        output_dir=f"{output_dir}/assembled",
    )

    # 6. Anti-detection post-processing
    final_path = await post_process_anti_detection(video_path)

    return {
        "format": ContentFormat.REDDIT_STORY.value,
        "video_path": final_path,
        "source_story": {
            "title": story["title"],
            "subreddit": story.get("subreddit"),
            "score": story.get("score"),
            "permalink": story.get("permalink"),
        },
        "adapted_text": adapted_text,
        "total_cost_usd": voiceover.cost_usd + 0.003,  # script adaptation
        "duration_s": transcript["duration_s"],
    }
