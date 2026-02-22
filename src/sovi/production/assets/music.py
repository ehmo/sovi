"""Background music management — stock library + selection logic.

Uses a pre-downloaded library of royalty-free background tracks categorized by mood.
Music is mixed at ~8% volume behind voiceover during assembly.
"""

from __future__ import annotations

import random
from pathlib import Path

from sovi.config import settings

# Music library directory structure:
#   assets/music/{mood}/{track_name}.mp3
MUSIC_DIR = Path(settings.output_dir).parent / "assets" / "music"

# Mood categories matching hook emotional tones
MOOD_CATEGORIES = [
    "upbeat",       # energetic, motivational
    "calm",         # relaxed, ambient
    "dramatic",     # tension, suspense
    "inspiring",    # hopeful, cinematic
    "mysterious",   # curiosity, intrigue
    "corporate",    # professional, clean
]

# Map hook emotional tones to music moods
TONE_TO_MOOD = {
    "curiosity": "mysterious",
    "contrarian": "dramatic",
    "empathy": "calm",
    "inspiration": "inspiring",
    "authority": "corporate",
    "urgency": "dramatic",
    "practical": "upbeat",
    "vulnerability": "calm",
    "excitement": "upbeat",
    "connection": "inspiring",
    # Defaults
    "engaging": "upbeat",
    "neutral": "calm",
}


def select_background_music(
    emotional_tone: str | None = None,
    mood: str | None = None,
) -> str | None:
    """Select a random background track matching the emotional tone.

    Returns path to music file, or None if no library exists.
    """
    target_mood = mood or TONE_TO_MOOD.get(emotional_tone or "", "calm")

    # Try mood-specific directory first
    mood_dir = MUSIC_DIR / target_mood
    if mood_dir.exists():
        tracks = list(mood_dir.glob("*.mp3")) + list(mood_dir.glob("*.m4a"))
        if tracks:
            return str(random.choice(tracks))

    # Fall back to any available track
    if MUSIC_DIR.exists():
        all_tracks = list(MUSIC_DIR.rglob("*.mp3")) + list(MUSIC_DIR.rglob("*.m4a"))
        if all_tracks:
            return str(random.choice(all_tracks))

    return None


def init_music_library() -> None:
    """Create the music library directory structure."""
    for mood in MOOD_CATEGORIES:
        (MUSIC_DIR / mood).mkdir(parents=True, exist_ok=True)
