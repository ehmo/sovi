"""Cross-platform posting scheduler with staggered timing and account rotation."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from sovi.models import DistributionRequest, Platform

# Optimal posting times (hour in local timezone) and day offsets from primary post
PLATFORM_SCHEDULE = {
    Platform.TIKTOK: {"hour": 19, "day_offset": 0},     # Day 0, 7 PM — first
    Platform.INSTAGRAM: {"hour": 11, "day_offset": 1},   # Day 1, 11 AM
    Platform.YOUTUBE: {"hour": 15, "day_offset": 1},     # Day 1, 3 PM
    Platform.TWITTER: {"hour": 10, "day_offset": 2},     # Day 2, 10 AM
    Platform.REDDIT: {"hour": 7, "day_offset": 3},       # Day 3, 7 AM weekday
}

# Max posts per day per account before diminishing returns
DAILY_LIMITS = {
    Platform.TIKTOK: 2,
    Platform.INSTAGRAM: 2,
    Platform.YOUTUBE: 1,
    Platform.REDDIT: 2,
    Platform.TWITTER: 5,
}


def schedule_distribution(
    content_id: UUID,
    account_ids: dict[Platform, UUID],
    export_paths: dict[Platform, str],
    caption: str,
    hashtags: dict[Platform, list[str]],
    base_time: datetime | None = None,
) -> list[DistributionRequest]:
    """Generate staggered distribution requests across platforms."""
    if base_time is None:
        base_time = datetime.utcnow()

    requests = []
    for platform, account_id in account_ids.items():
        if platform not in export_paths:
            continue

        schedule = PLATFORM_SCHEDULE.get(platform)
        if not schedule:
            continue

        # Calculate scheduled time
        scheduled = base_time.replace(
            hour=schedule["hour"], minute=0, second=0, microsecond=0
        ) + timedelta(days=schedule["day_offset"])

        # If scheduled time is in the past, push to next day
        if scheduled <= datetime.utcnow():
            scheduled += timedelta(days=1)

        requests.append(DistributionRequest(
            content_id=content_id,
            account_id=account_id,
            platform=platform,
            export_path=export_paths[platform],
            caption=caption,
            hashtags=hashtags.get(platform, []),
            scheduled_at=scheduled,
        ))

    return sorted(requests, key=lambda r: r.scheduled_at or datetime.max)
