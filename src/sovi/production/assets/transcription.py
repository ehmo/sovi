"""Audio transcription via Deepgram Nova-3 with word-level timestamps."""

from __future__ import annotations

from pathlib import Path

from sovi.config import settings


async def transcribe(audio_path: str) -> dict:
    """Transcribe audio file, returning word-level timestamps for caption generation.

    Returns:
        {
            "transcript": "full text...",
            "words": [
                {"word": "hello", "start": 0.0, "end": 0.45, "confidence": 0.99},
                ...
            ],
            "duration_s": 45.2,
        }
    """
    from deepgram import DeepgramClient, PrerecordedOptions, FileSource

    client = DeepgramClient(settings.deepgram_api_key)

    with open(audio_path, "rb") as f:
        payload: FileSource = {"buffer": f.read()}

    options = PrerecordedOptions(
        model="nova-3",
        smart_format=True,
        punctuate=True,
        diarize=False,
        language="en",
    )

    response = await client.listen.asyncrest.v("1").transcribe_file(payload, options)

    result = response.to_dict()
    channel = result["results"]["channels"][0]
    alt = channel["alternatives"][0]

    words = []
    for w in alt.get("words", []):
        words.append({
            "word": w["word"],
            "start": w["start"],
            "end": w["end"],
            "confidence": w.get("confidence", 0.0),
        })

    return {
        "transcript": alt["transcript"],
        "words": words,
        "duration_s": result.get("metadata", {}).get("duration", 0.0),
    }


def words_to_ass(words: list[dict], video_width: int = 1080, video_height: int = 1920) -> str:
    """Convert word-level timestamps to ASS subtitle format for animated captions.

    Produces word-by-word highlighting style popular on TikTok/Reels.
    """
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_width}
PlayResY: {video_height}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Montserrat,72,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,0,2,40,40,180,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    # Group words into ~3-4 word chunks for readability
    chunk_size = 3
    for i in range(0, len(words), chunk_size):
        chunk = words[i : i + chunk_size]
        start = chunk[0]["start"]
        end = chunk[-1]["end"]
        text = " ".join(w["word"] for w in chunk)

        start_ts = _seconds_to_ass_time(start)
        end_ts = _seconds_to_ass_time(end)
        lines.append(f"Dialogue: 0,{start_ts},{end_ts},Default,,0,0,0,,{text}")

    return header + "\n".join(lines) + "\n"


def _seconds_to_ass_time(s: float) -> str:
    """Convert seconds to ASS timestamp format H:MM:SS.CC."""
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = int(s % 60)
    cs = int((s % 1) * 100)
    return f"{h}:{m:02d}:{sec:02d}.{cs:02d}"
