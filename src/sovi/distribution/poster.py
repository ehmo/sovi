"""Content distribution via Late API (primary) and Upload-Post (backup)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import httpx

from sovi.config import settings
from sovi.models import DistributionRequest, Platform

LATE_BASE_URL = "https://api.getlate.dev/v1"

# Platform IDs in Late API
# Map our Platform enum to Late API platform names
LATE_PLATFORM_MAP = {
    Platform.TIKTOK: "tiktok",
    Platform.INSTAGRAM: "instagram",
    Platform.YOUTUBE: "youtube",     # Late API uses "youtube" not "youtube_shorts"
    Platform.REDDIT: "reddit",
    Platform.TWITTER: "twitter",     # Late API uses "twitter" not "x_twitter"
}


async def post_via_late(request: DistributionRequest) -> dict:
    """Post content to a platform via Late API (Accelerate tier)."""
    platform = LATE_PLATFORM_MAP.get(request.platform)
    if not platform:
        raise ValueError(f"Unsupported platform: {request.platform}")

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Upload media first
        with open(request.export_path, "rb") as f:
            upload_resp = await client.post(
                f"{LATE_BASE_URL}/media/upload",
                headers={"Authorization": f"Bearer {settings.late_api_key}"},
                files={"file": ("video.mp4", f, "video/mp4")},
            )
            upload_resp.raise_for_status()
            media_id = upload_resp.json()["id"]

        # Build caption with hashtags
        caption = request.caption
        if request.hashtags:
            caption += "\n\n" + " ".join(f"#{tag}" for tag in request.hashtags)

        # Create post
        payload: dict = {
            "platform": platform,
            "account_id": str(request.account_id),
            "media_ids": [media_id],
            "caption": caption,
        }

        if request.scheduled_at:
            payload["scheduled_at"] = request.scheduled_at.isoformat()

        post_resp = await client.post(
            f"{LATE_BASE_URL}/posts",
            headers={
                "Authorization": f"Bearer {settings.late_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        post_resp.raise_for_status()
        return post_resp.json()


async def get_post_analytics(post_id: str) -> dict:
    """Fetch analytics for a posted piece of content via Late API."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{LATE_BASE_URL}/posts/{post_id}/analytics",
            headers={"Authorization": f"Bearer {settings.late_api_key}"},
        )
        resp.raise_for_status()
        return resp.json()
