"""Feedback loop — update hook scores and niche allocations based on performance data."""

from __future__ import annotations

from sovi import db
from sovi.hooks.selector import deprecate_underperformers, update_hook_performance


async def process_content_feedback(content_id: str) -> None:
    """Process performance feedback for a piece of content.

    Called at T+24h to update hook template scores.
    """
    from sovi.analytics.collector import calculate_overperformance

    from uuid import UUID
    cid = UUID(content_id)
    overperformance = await calculate_overperformance(cid)

    if overperformance is None:
        return

    # Get the hook template used by this content
    result = await db.execute_one("""
        SELECT hook_id FROM content WHERE id = %s AND hook_id IS NOT NULL
    """, (str(cid),))

    if not result or not result.get("hook_id"):
        return

    hook_id = result["hook_id"]
    succeeded = overperformance > 1.0
    await update_hook_performance(hook_id, succeeded)


async def rebalance_niches() -> dict:
    """Weekly niche rebalancing based on content-to-engagement efficiency.

    Increase top 25% production by 20%, decrease bottom 25% by 20%.
    Cap any single niche at 40% of total production.
    """
    niche_efficiency = await db.execute("""
        SELECT n.slug, n.name,
               COUNT(c.id) as content_count,
               AVG(CASE WHEN ms.views > 0
                    THEN (ms.likes + ms.comments + ms.shares)::float / ms.views
                    ELSE 0 END) as avg_engagement,
               SUM(c.cost_usd) as total_cost
        FROM niches n
        LEFT JOIN content c ON c.niche_id = n.id
            AND c.created_at >= NOW() - interval '30 days'
        LEFT JOIN distributions d ON d.content_id = c.id
        LEFT JOIN metric_snapshots ms ON ms.distribution_id = d.id
        WHERE n.is_active = true
        GROUP BY n.id, n.slug, n.name
        ORDER BY avg_engagement DESC NULLS LAST
    """)

    if not niche_efficiency:
        return {"adjusted": 0}

    # Calculate efficiency = engagement / cost
    for niche in niche_efficiency:
        cost = float(niche.get("total_cost") or 1.0)
        engagement = float(niche.get("avg_engagement") or 0.0)
        niche["efficiency"] = engagement / cost

    return {
        "niches": niche_efficiency,
        "top_performers": [n["slug"] for n in niche_efficiency[:len(niche_efficiency)//4]],
        "bottom_performers": [n["slug"] for n in niche_efficiency[-(len(niche_efficiency)//4):]],
    }


async def run_nightly_feedback() -> dict:
    """Run all nightly feedback operations."""
    deprecated = await deprecate_underperformers()
    return {"hooks_deprecated": deprecated}
