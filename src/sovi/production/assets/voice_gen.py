"""Voice generation via ElevenLabs (hero) and OpenAI TTS (bulk)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from sovi.config import settings
from sovi.models import GeneratedAsset


async def generate_voiceover_elevenlabs(
    text: str,
    voice_id: str = "21m00Tcm4TlvDq8ikWAM",  # Default Rachel voice
    output_dir: str = "output/voiceovers",
) -> GeneratedAsset:
    """Generate voiceover via ElevenLabs API."""
    from elevenlabs.client import AsyncElevenLabs

    client = AsyncElevenLabs(api_key=settings.elevenlabs_api_key)

    audio_generator = await client.text_to_speech.convert(
        voice_id=voice_id,
        text=text,
        model_id="eleven_turbo_v2_5",
        output_format="mp3_44100_128",
    )

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    file_path = f"{output_dir}/{uuid4().hex[:12]}.mp3"

    # Collect async generator into bytes
    audio_bytes = b""
    async for chunk in audio_generator:
        audio_bytes += chunk

    Path(file_path).write_bytes(audio_bytes)

    # Estimate cost: ~$0.30/1K chars at Pro tier
    cost = len(text) / 1000 * 0.30

    return GeneratedAsset(
        asset_type="voiceover",
        file_path=file_path,
        cost_usd=cost,
        model_used="elevenlabs_turbo_v2_5",
    )


async def generate_voiceover_openai(
    text: str,
    voice: str = "alloy",
    output_dir: str = "output/voiceovers",
) -> GeneratedAsset:
    """Generate voiceover via OpenAI TTS (cheaper bulk option)."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=settings.openai_api_key)

    response = await client.audio.speech.create(
        model="tts-1",
        voice=voice,
        input=text,
        response_format="mp3",
    )

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    file_path = f"{output_dir}/{uuid4().hex[:12]}.mp3"
    response.stream_to_file(file_path)

    # OpenAI TTS: ~$15/1M chars
    cost = len(text) / 1_000_000 * 15.0

    return GeneratedAsset(
        asset_type="voiceover",
        file_path=file_path,
        cost_usd=cost,
        model_used="openai_tts-1",
    )


async def generate_voiceover(
    text: str,
    voice_id: str | None = None,
    use_elevenlabs: bool = True,
    output_dir: str = "output/voiceovers",
) -> GeneratedAsset:
    """Route to ElevenLabs (hero) or OpenAI (bulk) based on flag."""
    if use_elevenlabs:
        return await generate_voiceover_elevenlabs(text, voice_id or "21m00Tcm4TlvDq8ikWAM", output_dir)
    return await generate_voiceover_openai(text, voice_id or "alloy", output_dir)
