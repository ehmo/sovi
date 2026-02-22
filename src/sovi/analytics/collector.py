"""Metrics collection from platforms and storage in TimescaleDB."""

from __future__ import annotations

from uuid import UUID

from sovi import db
from sovi.models import EngagementSnapshot


async def store_engagement_snapshot(snapshot: EngagementSnapshot) -> None:
    """Insert a metric snapshot into the metric_snapshots hypertable."""
    await db.execute("""
        INSERT INTO metric_snapshots
            (time, distribution_id, views, likes, comments, shares, saves,
             completion_rate, engagement_rate)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        snapshot.collected_at,
        str(snapshot.distribution_id),
        snapshot.views,
        snapshot.likes,
        snapshot.comments,
        snapshot.shares,
        snapshot.saves,
        snapshot.completion_rate,
        snapshot.engagement_rate,
    ))


async def get_content_performance(content_id: UUID, days: int = 7) -> list[dict]:
    """Get engagement metrics for a piece of content over time."""
    return await db.execute("""
        SELECT ms.time, ms.views, ms.likes, ms.comments, ms.shares, ms.saves,
               ms.completion_rate, ms.engagement_rate,
               d.platform, a.username
        FROM metric_snapshots ms
        JOIN distributions d ON ms.distribution_id = d.id
        JOIN accounts a ON d.account_id = a.id
        WHERE d.content_id = %s
          AND ms.time >= NOW() - make_interval(days => %s)
        ORDER BY ms.time ASC
    """, (str(content_id), days))


async def calculate_overperformance(content_id: UUID) -> float | None:
    """Calculate overperformance ratio: content engagement vs niche baseline."""
    result = await db.execute_one("""
        WITH content_engagement AS (
            SELECT AVG(ms.views) as avg_views,
                   AVG(CASE WHEN ms.views > 0
                        THEN (ms.likes + ms.comments + ms.shares)::float / ms.views
                        ELSE 0 END) as engagement_rate
            FROM metric_snapshots ms
            JOIN distributions d ON ms.distribution_id = d.id
            WHERE d.content_id = %s
              AND ms.time >= NOW() - interval '7 days'
        ),
        niche_baseline AS (
            SELECT AVG(CASE WHEN ms.views > 0
                        THEN (ms.likes + ms.comments + ms.shares)::float / ms.views
                        ELSE 0 END) as baseline_engagement
            FROM metric_snapshots ms
            JOIN distributions d ON ms.distribution_id = d.id
            JOIN content c ON d.content_id = c.id
            JOIN content target ON target.id = %s AND target.niche_id = c.niche_id
            WHERE ms.time >= NOW() - interval '30 days'
        )
        SELECT CASE WHEN nb.baseline_engagement > 0
               THEN ce.engagement_rate / nb.baseline_engagement
               ELSE NULL END as overperformance
        FROM content_engagement ce, niche_baseline nb
    """, (str(content_id), str(content_id)))

    return result["overperformance"] if result else None


async def detect_shadowban(account_id: UUID) -> bool:
    """Detect potential shadowban via >50% reach drop vs 14-day average."""
    result = await db.execute_one("""
        WITH recent AS (
            SELECT AVG(ms.views) as recent_avg
            FROM metric_snapshots ms
            JOIN distributions d ON ms.distribution_id = d.id
            WHERE d.account_id = %s
              AND ms.time >= NOW() - interval '3 days'
        ),
        baseline AS (
            SELECT AVG(ms.views) as baseline_avg
            FROM metric_snapshots ms
            JOIN distributions d ON ms.distribution_id = d.id
            WHERE d.account_id = %s
              AND ms.time >= NOW() - interval '14 days'
              AND ms.time < NOW() - interval '3 days'
        )
        SELECT CASE WHEN b.baseline_avg > 0 AND r.recent_avg < b.baseline_avg * 0.5
               THEN true ELSE false END as is_shadowbanned
        FROM recent r, baseline b
    """, (str(account_id), str(account_id)))

    return result["is_shadowbanned"] if result else False
