"""Distribution orchestrator — end-to-end flow from content to scheduled posts.

Given a content_id, selects accounts per platform, creates platform exports,
schedules staggered distribution, persists to the distributions table, and
kicks off posting via Late API.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from uuid import UUID, uuid4

from sovi import db
from sovi.distribution.accounts import get_account_for_posting
from sovi.distribution.poster import post_via_late
from sovi.distribution.scheduler import schedule_distribution
from sovi.models import DistributionRequest, Platform
from sovi.production.assembly import export_for_platform

logger = logging.getLogger(__name__)


async def distribute_content(
    content_id: UUID,
    platforms: list[Platform] | None = None,
    base_time: datetime | None = None,
) -> dict:
    """Full distribution flow for a piece of content.

    1. Load content + its niche
    2. Select best account per platform
    3. Generate platform exports
    4. Create staggered schedule
    5. Persist distribution records
    6. Optionally post immediately for first platform

    Returns summary with distribution IDs and schedule.
    """
    # 1. Load content record
    content = await db.execute_one("""
        SELECT c.id, c.topic, c.content_format, c.file_paths,
               c.duration_seconds, n.slug AS niche_slug
        FROM content c
        JOIN niches n ON c.niche_id = n.id
        WHERE c.id = %s AND c.production_status = 'complete'
    """, (str(content_id),))

    if not content:
        return {"error": f"Content {content_id} not found or not complete"}

    file_paths = content["file_paths"]
    if isinstance(file_paths, str):
        file_paths = json.loads(file_paths)

    video_path = file_paths.get("video", "")
    if not video_path:
        return {"error": "No video path in content file_paths"}

    niche_slug = content["niche_slug"]
    target_platforms = platforms or [
        Platform.TIKTOK, Platform.INSTAGRAM, Platform.YOUTUBE,
        Platform.TWITTER, Platform.REDDIT,
    ]

    # 2. Select best account per platform
    account_ids: dict[Platform, UUID] = {}
    for platform in target_platforms:
        account = await get_account_for_posting(platform, niche_slug)
        if account:
            account_ids[platform] = UUID(str(account["id"]))
            logger.info("Selected account %s for %s/%s", account["username"], platform, niche_slug)
        else:
            logger.warning("No available account for %s/%s", platform, niche_slug)

    if not account_ids:
        return {"error": "No available accounts for any target platform"}

    # 3. Generate platform-specific exports
    export_paths: dict[Platform, str] = {}
    existing_exports = file_paths.get("exports", {})

    for platform in account_ids:
        # Reuse existing export if available
        if platform.value in existing_exports:
            export_paths[platform] = existing_exports[platform.value]
            continue

        try:
            exported = await export_for_platform(video_path, platform.value)
            export_paths[platform] = exported
        except Exception:
            logger.error("Failed to export for %s", platform, exc_info=True)

    # 4. Generate caption per platform
    topic = content.get("topic", "")
    caption = topic  # Base caption — in production, Claude API rewrites per platform

    # 5. Create staggered schedule
    requests = schedule_distribution(
        content_id=content_id,
        account_ids=account_ids,
        export_paths={p: path for p, path in export_paths.items()},
        caption=caption,
        hashtags={},  # TODO: generate platform-specific hashtags
        base_time=base_time,
    )

    # 6. Persist distribution records
    distribution_ids = []
    for req in requests:
        dist_id = uuid4()
        await db.execute("""
            INSERT INTO distributions
                (id, content_id, account_id, platform, scheduled_for, status)
            VALUES (%s, %s, %s, %s::platform_type, %s, 'queued'::distribution_status)
        """, (
            str(dist_id),
            str(content_id),
            str(req.account_id),
            req.platform.value,
            req.scheduled_at,
        ))
        distribution_ids.append({
            "id": str(dist_id),
            "platform": req.platform.value,
            "scheduled_for": req.scheduled_at.isoformat() if req.scheduled_at else None,
        })
        logger.info(
            "Scheduled distribution %s: %s at %s",
            dist_id, req.platform.value, req.scheduled_at,
        )

    return {
        "content_id": str(content_id),
        "distributions": distribution_ids,
        "platforms": [p.value for p in account_ids],
        "exports": {p.value: path for p, path in export_paths.items()},
    }


async def execute_pending_distributions() -> dict:
    """Find and execute distributions that are due for posting.

    Called by a scheduled job (e.g., every 5 minutes).
    """
    pending = await db.execute("""
        SELECT d.id, d.content_id, d.account_id, d.platform,
               d.scheduled_for, d.retry_count,
               c.file_paths, c.topic
        FROM distributions d
        JOIN content c ON d.content_id = c.id
        WHERE d.status = 'queued'
          AND (d.scheduled_for IS NULL OR d.scheduled_for <= NOW())
          AND d.retry_count < 3
        ORDER BY d.scheduled_for ASC NULLS FIRST
        LIMIT 10
    """)

    results = {"posted": 0, "failed": 0, "skipped": 0}

    for dist in pending:
        dist_id = str(dist["id"])
        platform = dist["platform"]

        file_paths = dist["file_paths"]
        if isinstance(file_paths, str):
            file_paths = json.loads(file_paths)

        exports = file_paths.get("exports", {})
        export_path = exports.get(platform, file_paths.get("video", ""))

        if not export_path:
            logger.warning("No export path for distribution %s", dist_id)
            results["skipped"] += 1
            continue

        # Mark as in-progress
        await db.execute(
            "UPDATE distributions SET status = 'posting' WHERE id = %s",
            (dist_id,),
        )

        try:
            request = DistributionRequest(
                content_id=UUID(str(dist["content_id"])),
                account_id=UUID(str(dist["account_id"])),
                platform=Platform(platform),
                export_path=export_path,
                caption=dist.get("topic", ""),
            )
            resp = await post_via_late(request)

            # Update with success
            await db.execute("""
                UPDATE distributions
                SET status = 'posted',
                    posted_at = NOW(),
                    post_id_on_platform = %s,
                    post_url = %s
                WHERE id = %s
            """, (
                resp.get("id", ""),
                resp.get("url", ""),
                dist_id,
            ))
            results["posted"] += 1
            logger.info("Posted distribution %s to %s", dist_id, platform)

        except Exception as e:
            await db.execute("""
                UPDATE distributions
                SET status = 'queued',
                    retry_count = retry_count + 1,
                    error_message = %s
                WHERE id = %s
            """, (str(e)[:500], dist_id))
            results["failed"] += 1
            logger.error("Failed distribution %s: %s", dist_id, e)

    return results
