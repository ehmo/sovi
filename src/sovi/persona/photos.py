"""Face photo generation for personas using Flux via fal.ai.

Generates consistent face photos for a persona:
1. Initial reference headshot from text description
2. Subsequent photos using face reference for consistency
3. Varied poses, outfits, lighting, and settings
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import httpx

from sovi.config import settings
from sovi.db import sync_execute, sync_execute_one

logger = logging.getLogger(__name__)

FAL_BASE = "https://queue.fal.run"

# Photo variations to generate per persona
PHOTO_SPECS = [
    {"type": "headshot", "prompt_suffix": "professional headshot, white background, studio lighting, shoulders up, slight smile, high quality portrait photography"},
    {"type": "casual", "prompt_suffix": "casual selfie, natural lighting, relaxed expression, outdoor setting, candid feel"},
    {"type": "professional", "prompt_suffix": "professional photo, business casual outfit, modern office background, confident pose"},
    {"type": "lifestyle", "prompt_suffix": "lifestyle photo, coffee shop setting, natural light through windows, candid moment"},
    {"type": "casual", "prompt_suffix": "casual outdoor photo, park or urban setting, natural sunlight, genuine smile"},
    {"type": "headshot", "prompt_suffix": "close-up portrait, natural makeup, soft lighting, neutral background, warm expression"},
    {"type": "lifestyle", "prompt_suffix": "active lifestyle photo, fitness or outdoor activity, energetic pose, natural setting"},
    {"type": "professional", "prompt_suffix": "speaking at event or conference, professional attire, confident expression"},
    {"type": "casual", "prompt_suffix": "casual photo at home, cozy setting, relaxed and authentic, natural colors"},
    {"type": "lifestyle", "prompt_suffix": "social gathering, friends or event setting, happy expression, candid moment"},
]


def _build_base_prompt(persona: dict) -> str:
    """Build a text description of the persona for image generation."""
    gender = persona.get("gender", "person")
    age = persona.get("age", 28)
    occupation = persona.get("occupation", "")

    # Map gender to photo description
    gender_desc = {
        "female": "woman",
        "male": "man",
        "nonbinary": "person",
    }.get(gender, "person")

    parts = [
        f"photo of a {age} year old {gender_desc}",
    ]
    if occupation:
        parts.append(f"who works as a {occupation}")

    return ", ".join(parts)


def _fal_generate(prompt: str, image_url: str | None = None) -> tuple[bytes, str] | None:
    """Call fal.ai Flux to generate an image.

    If image_url is provided, uses it as a face reference for consistency.
    Returns (PNG bytes, image URL) or None on failure.
    """
    if not settings.fal_key:
        logger.error("FAL_KEY not configured")
        return None

    headers = {
        "Authorization": f"Key {settings.fal_key}",
        "Content-Type": "application/json",
    }

    # Use Flux Schnell for speed, or Dev for quality
    model = "fal-ai/flux/schnell"

    payload: dict[str, Any] = {
        "prompt": prompt,
        "image_size": "square_hd",
        "num_images": 1,
        "enable_safety_checker": False,
    }

    if image_url:
        # Use Flux with IP-Adapter for face consistency
        model = "fal-ai/flux-general/image-to-image"
        payload["image_url"] = image_url
        payload["strength"] = 0.65

    try:
        # Submit to queue
        resp = httpx.post(
            f"{FAL_BASE}/{model}",
            headers=headers,
            json=payload,
            timeout=30.0,
        )
        resp.raise_for_status()
        result = resp.json()

        # Check if queued (async) or direct result
        if "request_id" in result:
            # Poll for result
            request_id = result["request_id"]
            status_url = f"https://queue.fal.run/{model}/requests/{request_id}/status"
            result_url = f"https://queue.fal.run/{model}/requests/{request_id}"

            for _ in range(60):  # up to 5 minutes
                import time
                time.sleep(5)
                status_resp = httpx.get(status_url, headers=headers, timeout=15.0)
                status_data = status_resp.json()
                if status_data.get("status") == "COMPLETED":
                    result_resp = httpx.get(result_url, headers=headers, timeout=30.0)
                    result = result_resp.json()
                    break
                elif status_data.get("status") == "FAILED":
                    logger.error("fal.ai generation failed: %s", status_data)
                    return None
            else:
                logger.warning("fal.ai generation timed out after 5 minutes (request_id=%s)", request_id)
                return None

        # Extract image URL from result
        images = result.get("images", [])
        if not images:
            logger.error("No images in fal.ai response")
            return None

        image_url_result = images[0].get("url")
        if not image_url_result:
            return None

        # Download the image
        img_resp = httpx.get(image_url_result, timeout=30.0)
        img_resp.raise_for_status()
        return img_resp.content, image_url_result

    except Exception:
        logger.error("fal.ai image generation failed", exc_info=True)
        return None


def generate_persona_photos(persona: dict, count: int = 10) -> list[str]:
    """Generate consistent face photos for a persona using Flux via fal.ai.

    1. Generate initial reference headshot from text description
    2. Use face reference for subsequent photos
    3. Vary: pose, outfit, lighting, setting, expression

    Returns list of file paths.
    """
    persona_id = str(persona["id"])
    output_dir = settings.output_dir / "personas" / persona_id / "photos"
    output_dir.mkdir(parents=True, exist_ok=True)

    base_prompt = _build_base_prompt(persona)
    specs = PHOTO_SPECS[:count]
    paths: list[str] = []
    reference_url: str | None = None

    for i, spec in enumerate(specs):
        full_prompt = f"{base_prompt}, {spec['prompt_suffix']}"
        is_primary = i == 0

        logger.info("Generating photo %d/%d for %s: %s", i + 1, count, persona.get("display_name"), spec["type"])

        # First image: text-to-image. Subsequent: use reference for consistency.
        result = _fal_generate(full_prompt, image_url=reference_url)
        if not result:
            logger.warning("Failed to generate photo %d for %s", i + 1, persona.get("display_name"))
            continue
        image_bytes, generated_url = result

        # Use the first successful image as face reference for subsequent photos
        if reference_url is None:
            reference_url = generated_url

        # Save to disk
        filename = f"{spec['type']}_{i:02d}.png"
        filepath = output_dir / filename
        filepath.write_bytes(image_bytes)

        # Store in DB
        sync_execute(
            """INSERT INTO persona_photos
               (persona_id, file_path, photo_type, prompt_used, is_primary)
               VALUES (%s, %s, %s, %s, %s)""",
            (persona_id, str(filepath), spec["type"], full_prompt, is_primary),
        )

        paths.append(str(filepath))


    return paths
