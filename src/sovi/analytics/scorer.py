"""Content, hook, and niche scoring — powers the feedback loop."""

from __future__ import annotations

from uuid import UUID

from sovi import db


async def score_content(content_id: UUID) -> dict:
    """Score a piece of content based on engagement metrics.

    Returns dict with engagement_rate, overperformance, completion_rate, lifecycle_class.
    """
    result = await db.execute_one("""
        SELECT
            c.id,
            c.topic,
            c.content_format,
            c.quality_score,
            c.cost_usd,
            COALESCE(SUM(ms.views), 0) AS total_views,
            COALESCE(SUM(ms.likes), 0) AS total_likes,
            COALESCE(SUM(ms.comments), 0) AS total_comments,
            COALESCE(SUM(ms.shares), 0) AS total_shares,
            COALESCE(SUM(ms.saves), 0) AS total_saves,
            AVG(ms.completion_rate) AS avg_completion
        FROM content c
        LEFT JOIN distributions d ON d.content_id = c.id
        LEFT JOIN metric_snapshots ms ON ms.distribution_id = d.id
        WHERE c.id = %s
        GROUP BY c.id
    """, (str(content_id),))

    if not result:
        return {"content_id": str(content_id), "error": "not_found"}

    views = int(result["total_views"] or 0)
    likes = int(result["total_likes"] or 0)
    comments = int(result["total_comments"] or 0)
    shares = int(result["total_shares"] or 0)
    saves = int(result["total_saves"] or 0)

    engagement_rate = 0.0
    if views > 0:
        engagement_rate = (likes + comments + shares + saves) / views

    # Virality signal: share-to-view ratio
    share_ratio = shares / views if views > 0 else 0.0

    # Evergreen signal: save-to-view ratio
    save_ratio = saves / views if views > 0 else 0.0

    return {
        "content_id": str(content_id),
        "total_views": views,
        "engagement_rate": round(engagement_rate, 6),
        "share_ratio": round(share_ratio, 6),
        "save_ratio": round(save_ratio, 6),
        "avg_completion": float(result["avg_completion"] or 0),
        "cost_usd": float(result["cost_usd"] or 0),
        "cost_per_view": round(float(result["cost_usd"] or 0) / max(views, 1), 6),
    }


async def score_hooks(niche_slug: str | None = None, min_uses: int = 5) -> list[dict]:
    """Rank hook templates by performance.

    Returns list sorted by Thompson Sampling expected value (alpha / (alpha + beta)).
    """
    conditions = ["h.is_active = true", f"h.times_used >= {min_uses}"]
    params: list = []

    if niche_slug:
        conditions.append("(n.slug = %s OR h.niche_id IS NULL)")
        params.append(niche_slug)

    where = " AND ".join(conditions)
    hooks = await db.execute(f"""
        SELECT h.id, h.template_text, h.hook_category, h.emotional_tone,
               h.thompson_alpha, h.thompson_beta, h.times_used,
               h.performance_score,
               (h.thompson_alpha) / (h.thompson_alpha + h.thompson_beta) AS expected_value
        FROM hooks h
        LEFT JOIN niches n ON h.niche_id = n.id
        WHERE {where}
        ORDER BY (h.thompson_alpha) / (h.thompson_alpha + h.thompson_beta) DESC
    """, tuple(params))

    return [
        {
            "id": str(h["id"]),
            "template": h["template_text"][:80],
            "category": h["hook_category"],
            "expected_value": round(float(h["expected_value"]), 4),
            "times_used": h["times_used"],
            "alpha": float(h["thompson_alpha"]),
            "beta": float(h["thompson_beta"]),
        }
        for h in hooks
    ]


async def score_niches() -> list[dict]:
    """Score niches by content-to-engagement efficiency.

    Used by the weekly rebalancing in feedback.py.
    """
    niches = await db.execute("""
        SELECT n.slug, n.name,
               COUNT(c.id) AS content_count,
               COALESCE(SUM(c.cost_usd), 0) AS total_cost,
               COALESCE(SUM(ms.views), 0) AS total_views,
               COALESCE(SUM(ms.likes + ms.comments + ms.shares), 0) AS total_engagement,
               CASE WHEN SUM(ms.views) > 0
                    THEN SUM(ms.likes + ms.comments + ms.shares)::float / SUM(ms.views)
                    ELSE 0 END AS engagement_rate,
               CASE WHEN SUM(c.cost_usd) > 0
                    THEN SUM(ms.views)::float / SUM(c.cost_usd)
                    ELSE 0 END AS views_per_dollar
        FROM niches n
        LEFT JOIN content c ON c.niche_id = n.id
            AND c.created_at >= NOW() - interval '30 days'
        LEFT JOIN distributions d ON d.content_id = c.id
        LEFT JOIN metric_snapshots ms ON ms.distribution_id = d.id
        WHERE n.is_active = true
        GROUP BY n.id, n.slug, n.name
        ORDER BY engagement_rate DESC NULLS LAST
    """)

    return [
        {
            "slug": n["slug"],
            "name": n["name"],
            "content_count": n["content_count"],
            "total_cost": float(n["total_cost"]),
            "total_views": int(n["total_views"]),
            "engagement_rate": round(float(n["engagement_rate"]), 6),
            "views_per_dollar": round(float(n["views_per_dollar"]), 2),
        }
        for n in niches
    ]


async def get_top_content(
    niche_slug: str | None = None,
    days: int = 7,
    limit: int = 10,
) -> list[dict]:
    """Get top-performing content by engagement rate."""
    conditions = [
        "c.created_at >= NOW() - make_interval(days => %s)",
    ]
    params: list = [days]

    if niche_slug:
        conditions.append("n.slug = %s")
        params.append(niche_slug)

    where = " AND ".join(conditions)
    params.append(limit)

    rows = await db.execute(f"""
        SELECT c.id, c.topic, c.content_format, c.quality_score,
               c.cost_usd, n.slug AS niche_slug,
               COALESCE(SUM(ms.views), 0) AS total_views,
               CASE WHEN SUM(ms.views) > 0
                    THEN (SUM(ms.likes + ms.comments + ms.shares))::float / SUM(ms.views)
                    ELSE 0 END AS engagement_rate
        FROM content c
        JOIN niches n ON c.niche_id = n.id
        LEFT JOIN distributions d ON d.content_id = c.id
        LEFT JOIN metric_snapshots ms ON ms.distribution_id = d.id
        WHERE {where}
        GROUP BY c.id, c.topic, c.content_format, c.quality_score,
                 c.cost_usd, n.slug
        HAVING SUM(ms.views) > 0
        ORDER BY engagement_rate DESC
        LIMIT %s
    """, tuple(params))

    return [
        {
            "id": str(r["id"]),
            "topic": r["topic"],
            "format": r["content_format"],
            "niche": r["niche_slug"],
            "views": int(r["total_views"]),
            "engagement_rate": round(float(r["engagement_rate"]), 6),
            "quality_score": float(r["quality_score"] or 0),
            "cost_usd": float(r["cost_usd"] or 0),
        }
        for r in rows
    ]


async def get_production_summary(days: int = 7) -> dict:
    """Summary of production activity over the last N days."""
    result = await db.execute_one("""
        SELECT
            COUNT(*) AS total_content,
            COUNT(*) FILTER (WHERE production_status = 'complete') AS completed,
            COUNT(*) FILTER (WHERE production_status = 'failed') AS failed,
            COALESCE(SUM(cost_usd), 0) AS total_cost,
            AVG(quality_score) AS avg_quality,
            AVG(duration_seconds) AS avg_duration
        FROM content
        WHERE created_at >= NOW() - make_interval(days => %s)
    """, (days,))

    dist_result = await db.execute_one("""
        SELECT
            COUNT(*) AS total_distributions,
            COUNT(*) FILTER (WHERE status = 'posted') AS posted,
            COUNT(*) FILTER (WHERE status = 'failed') AS failed
        FROM distributions
        WHERE created_at >= NOW() - make_interval(days => %s)
    """, (days,))

    return {
        "period_days": days,
        "content": {
            "total": result["total_content"] if result else 0,
            "completed": result["completed"] if result else 0,
            "failed": result["failed"] if result else 0,
            "total_cost": float(result["total_cost"]) if result else 0,
            "avg_quality": round(float(result["avg_quality"] or 0), 2),
            "avg_duration": round(float(result["avg_duration"] or 0), 1),
        },
        "distribution": {
            "total": dist_result["total_distributions"] if dist_result else 0,
            "posted": dist_result["posted"] if dist_result else 0,
            "failed": dist_result["failed"] if dist_result else 0,
        },
    }
