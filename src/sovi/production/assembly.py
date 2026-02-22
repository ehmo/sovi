"""Video assembly via FFmpeg — combines assets into final videos."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from uuid import uuid4

from sovi.models import GeneratedAsset, Platform

# Platform export specs
PLATFORM_SPECS: dict[str, dict] = {
    "tiktok": {
        "width": 1080, "height": 1920, "fps": 30,
        "max_size_mb": 500, "codec": "libx264", "crf": 20,
        "audio_codec": "aac", "audio_bitrate": "192k",
    },
    "instagram": {
        "width": 1080, "height": 1920, "fps": 30,
        "max_size_mb": 4000, "codec": "libx264", "crf": 18,
        "audio_codec": "aac", "audio_bitrate": "256k",
    },
    "youtube_shorts": {
        "width": 1080, "height": 1920, "fps": 30,
        "max_size_mb": 60, "codec": "libx264", "crf": 23,
        "audio_codec": "aac", "audio_bitrate": "128k",
    },
    "reddit": {
        "width": 1080, "height": 1920, "fps": 30,
        "max_size_mb": 1000, "codec": "libx264", "crf": 20,
        "audio_codec": "aac", "audio_bitrate": "192k",
    },
    "x_twitter": {
        "width": 1080, "height": 1920, "fps": 30,
        "max_size_mb": 512, "codec": "libx264", "crf": 20,
        "audio_codec": "aac", "audio_bitrate": "192k",
    },
}


async def assemble_faceless_narration(
    voiceover_path: str,
    image_paths: list[str],
    music_path: str | None,
    caption_ass_path: str | None,
    duration_s: float,
    output_dir: str = "output/assembled",
) -> str:
    """Assemble a faceless narration video: images with Ken Burns + VO + captions + music."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    output_path = f"{output_dir}/{uuid4().hex[:12]}.mp4"

    # Calculate per-image duration
    n_images = max(len(image_paths), 1)
    img_duration = duration_s / n_images

    # Build FFmpeg filter for Ken Burns zoom/pan on each image
    inputs = []
    filter_parts = []
    for i, img_path in enumerate(image_paths):
        inputs.extend(["-loop", "1", "-t", str(img_duration), "-i", img_path])
        # Zoom from 1.0 to 1.15 over duration (Ken Burns effect)
        filter_parts.append(
            f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,"
            f"zoompan=z='min(zoom+0.0015,1.15)':d={int(img_duration * 30)}:"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920:fps=30"
            f"[v{i}]"
        )

    # Concatenate image segments
    concat_inputs = "".join(f"[v{i}]" for i in range(n_images))
    filter_parts.append(f"{concat_inputs}concat=n={n_images}:v=1:a=0[video]")

    # Add voiceover input
    inputs.extend(["-i", voiceover_path])
    vo_idx = n_images

    # Mix VO + music if provided
    if music_path:
        inputs.extend(["-i", music_path])
        music_idx = vo_idx + 1
        # VO at 100%, music at 8%
        filter_parts.append(
            f"[{music_idx}:a]volume=0.08[bgm];"
            f"[{vo_idx}:a][bgm]amix=inputs=2:duration=first[audio]"
        )
    else:
        filter_parts.append(f"[{vo_idx}:a]acopy[audio]")

    # Burn captions if ASS file provided
    if caption_ass_path:
        filter_parts.append(f"[video]ass={caption_ass_path}[final]")
        map_video = "[final]"
    else:
        map_video = "[video]"

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", map_video,
        "-map", "[audio]",
        "-c:v", "libx264", "-preset", "slow", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-t", str(duration_s),
        "-shortest",
        output_path,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg assembly failed: {stderr.decode()[-500:]}")

    return output_path


async def post_process_anti_detection(input_path: str, output_path: str | None = None) -> str:
    """Apply anti-AI-detection post-processing: noise, color shift, metadata strip."""
    if output_path is None:
        p = Path(input_path)
        output_path = str(p.with_stem(p.stem + "_processed"))

    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-vf", "noise=alls=5:allf=t,eq=contrast=1.03:brightness=0.01:saturation=1.05,unsharp=3:3:0.5",
        "-map_metadata", "-1",
        "-c:v", "libx264", "-crf", "20", "-preset", "slow",
        "-c:a", "aac", "-b:a", "192k",
        output_path,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg post-processing failed: {stderr.decode()[-500:]}")

    return output_path


async def export_for_platform(
    input_path: str,
    platform: str,
    output_dir: str = "output/exports",
) -> str:
    """Re-encode video to meet platform-specific requirements."""
    specs = PLATFORM_SPECS.get(platform, PLATFORM_SPECS["tiktok"])
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    output_path = f"{output_dir}/{platform}_{uuid4().hex[:8]}.mp4"

    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-vf", f"scale={specs['width']}:{specs['height']}:force_original_aspect_ratio=decrease,"
               f"pad={specs['width']}:{specs['height']}:(ow-iw)/2:(oh-ih)/2",
        "-r", str(specs["fps"]),
        "-c:v", specs["codec"], "-crf", str(specs["crf"]), "-preset", "medium",
        "-c:a", specs["audio_codec"], "-b:a", specs["audio_bitrate"],
        "-map_metadata", "-1",
        "-movflags", "+faststart",
        output_path,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg export failed for {platform}: {stderr.decode()[-500:]}")

    return output_path
