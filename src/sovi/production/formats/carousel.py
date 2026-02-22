"""Carousel / Slideshow format factory — multi-slide images for IG and TikTok photo mode."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont

from sovi.models import ContentFormat


async def produce_carousel(
    slides: list[dict],
    output_dir: str = "output/carousels",
) -> dict:
    """Produce a carousel set from slide definitions.

    Args:
        slides: List of dicts with keys: text, subtitle (optional), background_color
        output_dir: Output directory

    Returns dict with paths to all produced slide images.
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    set_id = uuid4().hex[:12]
    slide_paths = []

    for i, slide in enumerate(slides):
        img = _render_slide(
            text=slide["text"],
            subtitle=slide.get("subtitle", ""),
            bg_color=slide.get("background_color", "#1a1a2e"),
            text_color=slide.get("text_color", "#ffffff"),
            slide_number=i + 1,
            total_slides=len(slides),
        )
        path = f"{output_dir}/{set_id}_slide_{i+1:02d}.png"
        img.save(path, "PNG", quality=95)
        slide_paths.append(path)

    return {
        "format": ContentFormat.CAROUSEL.value,
        "slide_paths": slide_paths,
        "slide_count": len(slides),
        "total_cost_usd": 0.003,  # Script cost only, rendering is free
    }


def _render_slide(
    text: str,
    subtitle: str = "",
    bg_color: str = "#1a1a2e",
    text_color: str = "#ffffff",
    slide_number: int = 1,
    total_slides: int = 1,
    width: int = 1080,
    height: int = 1080,
) -> Image.Image:
    """Render a single carousel slide using Pillow."""
    img = Image.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(img)

    # Use default font (production: download and use Montserrat)
    try:
        title_font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 56)
        subtitle_font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 32)
        counter_font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 24)
    except OSError:
        title_font = ImageFont.load_default()
        subtitle_font = title_font
        counter_font = title_font

    # Draw main text (centered)
    text_bbox = draw.textbbox((0, 0), text, font=title_font)
    text_w = text_bbox[2] - text_bbox[0]
    # Word wrap if too wide
    max_w = width - 120
    if text_w > max_w:
        words = text.split()
        lines = []
        current = ""
        for word in words:
            test = f"{current} {word}".strip()
            tw = draw.textbbox((0, 0), test, font=title_font)[2]
            if tw > max_w and current:
                lines.append(current)
                current = word
            else:
                current = test
        if current:
            lines.append(current)
        text = "\n".join(lines)

    draw.multiline_text(
        (width // 2, height // 2 - 60),
        text,
        fill=text_color,
        font=title_font,
        anchor="mm",
        align="center",
    )

    # Draw subtitle
    if subtitle:
        draw.text(
            (width // 2, height // 2 + 80),
            subtitle,
            fill="#aaaaaa",
            font=subtitle_font,
            anchor="mm",
        )

    # Slide counter
    draw.text(
        (width - 60, height - 40),
        f"{slide_number}/{total_slides}",
        fill="#666666",
        font=counter_font,
        anchor="mm",
    )

    return img
