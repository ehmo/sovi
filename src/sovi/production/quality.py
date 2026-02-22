"""Automated quality gate — checks videos before distribution."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from sovi.models import QualityReport


async def check_video_quality(video_path: str, platform: str) -> QualityReport:
    """Run all quality checks on an assembled video."""
    blocking_failures: list[str] = []
    scores: dict[str, float] = {}

    # Get video metadata via ffprobe
    probe = await _ffprobe(video_path)
    if not probe:
        return QualityReport(
            passed=False, score=0.0,
            blocking_failures=["ffprobe failed — invalid video file"],
        )

    video_stream = next((s for s in probe.get("streams", []) if s["codec_type"] == "video"), None)
    audio_stream = next((s for s in probe.get("streams", []) if s["codec_type"] == "audio"), None)

    # 1. Resolution check (weight 0.10, blocking)
    if video_stream:
        w = int(video_stream.get("width", 0))
        h = int(video_stream.get("height", 0))
        resolution_ok = w >= 1080 and h >= 1920
        scores["resolution"] = 1.0 if resolution_ok else 0.0
        if not resolution_ok:
            blocking_failures.append(f"Resolution {w}x{h} below 1080x1920")
    else:
        scores["resolution"] = 0.0
        blocking_failures.append("No video stream found")
        resolution_ok = False

    # 2. Bitrate check (weight 0.05, non-blocking)
    bitrate = int(video_stream.get("bit_rate", 0)) if video_stream else 0
    bitrate_ok = bitrate >= 5_000_000  # 5 Mbps
    scores["bitrate"] = 1.0 if bitrate_ok else max(0.0, bitrate / 5_000_000)

    # 3. Audio check (weight 0.15, blocking)
    audio_ok = audio_stream is not None
    if not audio_ok:
        blocking_failures.append("No audio stream")
    scores["audio"] = 1.0 if audio_ok else 0.0

    # 4. File size check (platform-specific)
    file_size_mb = Path(video_path).stat().st_size / (1024 * 1024)
    max_sizes = {"tiktok": 500, "instagram": 4000, "youtube_shorts": 60, "reddit": 1000, "x_twitter": 512}
    max_mb = max_sizes.get(platform, 500)
    if file_size_mb > max_mb:
        blocking_failures.append(f"File size {file_size_mb:.1f}MB exceeds {platform} limit {max_mb}MB")

    # 5. Duration check
    duration = float(probe.get("format", {}).get("duration", 0))
    duration_ok = 5.0 <= duration <= 180.0
    scores["duration"] = 1.0 if duration_ok else 0.5

    # Compute weighted score
    weights = {"resolution": 0.15, "bitrate": 0.10, "audio": 0.20, "duration": 0.15}
    # Remaining weight (0.40) reserved for caption accuracy + content policy + engagement prediction
    # which are checked separately
    base_score = sum(scores.get(k, 0) * v for k, v in weights.items()) / sum(weights.values())

    passed = len(blocking_failures) == 0 and base_score >= 0.6

    return QualityReport(
        passed=passed,
        score=round(base_score, 3),
        resolution_ok=resolution_ok,
        bitrate_ok=bitrate_ok,
        audio_ok=audio_ok,
        safe_zone_ok=True,  # TODO: implement safe zone detection
        content_policy_ok=True,  # TODO: implement content policy scan
        blocking_failures=blocking_failures,
    )


async def _ffprobe(path: str) -> dict | None:
    """Run ffprobe and return parsed JSON."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        path,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        return json.loads(stdout.decode())
    except Exception:
        return None
