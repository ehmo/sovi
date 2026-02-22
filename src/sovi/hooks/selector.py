"""Hook template selection using Thompson Sampling (Beta-Bernoulli bandit)."""

from __future__ import annotations

import random
from uuid import UUID

from sovi import db


async def select_hook_template(
    niche_slug: str,
    platform: str | None = None,
    category: str | None = None,
) -> dict | None:
    """Select a hook template using Thompson Sampling.

    Draws a sample from each template's Beta(alpha, beta) posterior and
    picks the template with the highest sample. Naturally balances
    exploration (undersampled hooks) with exploitation (proven hooks).
    """
    conditions = ["h.is_active = true"]
    params: list = []
    idx = 1

    # Filter by niche if specified
    if niche_slug:
        conditions.append(f"(n.slug = %s OR h.niche_id IS NULL)")
        params.append(niche_slug)

    # Filter by platform if specified
    if platform:
        conditions.append(f"(h.platform = %s OR h.platform IS NULL)")
        params.append(platform)

    if category:
        conditions.append(f"h.hook_category = %s")
        params.append(category)

    where = " AND ".join(conditions)
    query = f"""
        SELECT h.id, h.hook_text, h.template_text, h.hook_category,
               h.emotional_tone, h.thompson_alpha, h.thompson_beta,
               h.times_used, h.performance_score
        FROM hooks h
        LEFT JOIN niches n ON h.niche_id = n.id
        WHERE {where}
    """
    templates = await db.execute(query, tuple(params))

    if not templates:
        return None

    # Thompson Sampling: draw from Beta distribution for each template
    best_template = None
    best_sample = -1.0
    for t in templates:
        alpha = float(t.get("thompson_alpha", 1.0))
        beta_val = float(t.get("thompson_beta", 1.0))
        sample = random.betavariate(alpha, beta_val)
        if sample > best_sample:
            best_sample = sample
            best_template = t

    return best_template


async def update_hook_performance(hook_id: UUID, succeeded: bool) -> None:
    """Update Thompson Sampling parameters after observing content performance.

    Success = content overperformance ratio > 1.0 at T+24h.
    """
    if succeeded:
        await db.execute(
            "UPDATE hooks SET thompson_alpha = thompson_alpha + 1, "
            "times_used = times_used + 1, updated_at = NOW() WHERE id = %s",
            (str(hook_id),),
        )
    else:
        await db.execute(
            "UPDATE hooks SET thompson_beta = thompson_beta + 1, "
            "times_used = times_used + 1, updated_at = NOW() WHERE id = %s",
            (str(hook_id),),
        )


async def deprecate_underperformers(min_trials: int = 20, min_success_rate: float = 0.2) -> int:
    """Auto-deprecate hook templates with <20% success rate after 20+ trials."""
    result = await db.execute("""
        UPDATE hooks
        SET is_active = false, updated_at = NOW()
        WHERE is_active = true
          AND (thompson_alpha + thompson_beta - 2) >= %s
          AND (thompson_alpha - 1.0) / (thompson_alpha + thompson_beta - 2.0) < %s
        RETURNING id
    """, (min_trials, min_success_rate))
    return len(result)
