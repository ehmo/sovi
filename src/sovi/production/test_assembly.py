"""Test the FFmpeg assembly pipeline with synthetic data — no API keys needed.

Usage:
    python -m sovi.production.test_assembly
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path
from uuid import uuid4

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TEST_DIR = Path("output/test_assembly")
SAMPLE_WORDS = [
    {"word": "Nobody", "start": 0.0, "end": 0.35, "confidence": 0.99},
    {"word": "talks", "start": 0.35, "end": 0.65, "confidence": 0.98},
    {"word": "about", "start": 0.65, "end": 0.90, "confidence": 0.99},
    {"word": "this", "start": 0.90, "end": 1.10, "confidence": 0.98},
    {"word": "but", "start": 1.20, "end": 1.40, "confidence": 0.99},
    {"word": "AI", "start": 1.45, "end": 1.65, "confidence": 0.98},
    {"word": "is", "start": 1.65, "end": 1.80, "confidence": 0.99},
    {"word": "changing", "start": 1.80, "end": 2.20, "confidence": 0.98},
    {"word": "everything", "start": 2.25, "end": 2.80, "confidence": 0.99},
    {"word": "about", "start": 2.90, "end": 3.15, "confidence": 0.98},
    {"word": "personal", "start": 3.20, "end": 3.60, "confidence": 0.97},
    {"word": "finance.", "start": 3.60, "end": 4.10, "confidence": 0.99},
    {"word": "Here", "start": 4.30, "end": 4.55, "confidence": 0.98},
    {"word": "are", "start": 4.55, "end": 4.70, "confidence": 0.99},
    {"word": "three", "start": 4.70, "end": 4.95, "confidence": 0.98},
    {"word": "tools", "start": 4.95, "end": 5.25, "confidence": 0.99},
    {"word": "that", "start": 5.30, "end": 5.50, "confidence": 0.98},
    {"word": "can", "start": 5.50, "end": 5.70, "confidence": 0.99},
    {"word": "save", "start": 5.70, "end": 5.95, "confidence": 0.98},
    {"word": "you", "start": 5.95, "end": 6.10, "confidence": 0.99},
    {"word": "thousands", "start": 6.15, "end": 6.65, "confidence": 0.98},
    {"word": "this", "start": 6.75, "end": 6.95, "confidence": 0.99},
    {"word": "year.", "start": 6.95, "end": 7.30, "confidence": 0.98},
]


def generate_test_audio(path: str, duration_s: float = 8.0) -> None:
    """Generate a sine wave tone as placeholder audio using FFmpeg."""
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={duration_s}",
            "-c:a", "aac", "-b:a", "128k",
            path,
        ],
        check=True,
        capture_output=True,
    )


def generate_test_image(path: str, text: str, color: str = "#1a1a2e") -> None:
    """Generate a 1080x1920 test image with centered text using FFmpeg."""
    # Escape text for FFmpeg drawtext
    safe_text = text.replace("'", "\\'").replace(":", "\\:")
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"color=c={color}:s=1080x1920:d=1",
            "-vf", f"drawtext=text='{safe_text}':fontsize=60:fontcolor=white:"
                   f"x=(w-text_w)/2:y=(h-text_h)/2",
            "-frames:v", "1",
            "-update", "1",
            path,
        ],
        check=True,
        capture_output=True,
    )


async def test_full_assembly() -> None:
    """Run the complete assembly pipeline with synthetic test data."""
    from sovi.production.assets.transcription import words_to_ass
    from sovi.production.assembly import (
        assemble_faceless_narration,
        export_for_platform,
        post_process_anti_detection,
    )
    from sovi.production.quality import check_video_quality

    # Setup test directories
    for sub in ["voiceovers", "images", "captions", "assembled", "exports"]:
        (TEST_DIR / sub).mkdir(parents=True, exist_ok=True)

    # 1. Generate synthetic voiceover (sine wave)
    logger.info("Generating test audio...")
    vo_path = str(TEST_DIR / "voiceovers" / "test_vo.m4a")
    generate_test_audio(vo_path, duration_s=8.0)
    logger.info("  -> %s", vo_path)

    # 2. Generate test images
    logger.info("Generating test images...")
    img_paths = []
    for i, label in enumerate(["HOOK - AI Finance", "BODY - 3 Tools", "CTA - Follow"], 1):
        p = str(TEST_DIR / "images" / f"test_img_{i}.png")
        generate_test_image(p, label, color=["#1a1a2e", "#2e1a2e", "#1a2e2e"][i - 1])
        img_paths.append(p)
        logger.info("  -> %s", p)

    # 3. Generate ASS captions from sample words
    logger.info("Generating ASS captions...")
    ass_content = words_to_ass(SAMPLE_WORDS)
    ass_path = str(TEST_DIR / "captions" / "test_captions.ass")
    Path(ass_path).write_text(ass_content)
    logger.info("  -> %s (%d bytes)", ass_path, len(ass_content))

    # 4. Assemble video
    logger.info("Assembling faceless narration video...")
    assembled_path = await assemble_faceless_narration(
        voiceover_path=vo_path,
        image_paths=img_paths,
        music_path=None,
        caption_ass_path=ass_path,
        duration_s=8.0,
        output_dir=str(TEST_DIR / "assembled"),
    )
    size_mb = Path(assembled_path).stat().st_size / (1024 * 1024)
    logger.info("  -> %s (%.2f MB)", assembled_path, size_mb)

    # 5. Anti-detection post-processing
    logger.info("Running anti-detection post-processing...")
    processed_path = await post_process_anti_detection(
        assembled_path,
        output_path=str(TEST_DIR / "assembled" / "test_processed.mp4"),
    )
    size_mb = Path(processed_path).stat().st_size / (1024 * 1024)
    logger.info("  -> %s (%.2f MB)", processed_path, size_mb)

    # 6. Quality check
    logger.info("Running quality checks...")
    report = await check_video_quality(processed_path, "tiktok")
    logger.info("  -> Passed: %s | Score: %.3f", report.passed, report.score)
    logger.info("  -> Resolution OK: %s | Bitrate OK: %s | Audio OK: %s",
                report.resolution_ok, report.bitrate_ok, report.audio_ok)
    if report.blocking_failures:
        for f in report.blocking_failures:
            logger.warning("  -> BLOCKING: %s", f)

    # 7. Platform exports
    logger.info("Exporting for platforms...")
    for platform in ["tiktok", "instagram", "youtube_shorts", "x_twitter"]:
        export_path = await export_for_platform(
            processed_path, platform,
            output_dir=str(TEST_DIR / "exports"),
        )
        size_mb = Path(export_path).stat().st_size / (1024 * 1024)
        logger.info("  -> %s: %s (%.2f MB)", platform, export_path, size_mb)

    # Summary
    logger.info("\n=== Assembly Pipeline Test Complete ===")
    logger.info("All outputs in: %s", TEST_DIR)
    logger.info("Test PASSED" if report.passed else "Test FAILED — check blocking failures")


if __name__ == "__main__":
    asyncio.run(test_full_assembly())
