"""Account registry and rotation logic."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from sovi import db
from sovi.models import AccountState, Platform


async def get_available_accounts(
    platform: Platform,
    niche_slug: str,
    exclude_ids: list[UUID] | None = None,
) -> list[dict]:
    """Get active, non-resting accounts for a platform/niche, sorted by health score."""
    exclude = exclude_ids or []
    exclude_clause = ""
    params: list = [platform.value, niche_slug]
    if exclude:
        placeholders = ",".join(["%s"] * len(exclude))
        exclude_clause = f"AND a.id NOT IN ({placeholders})"
        params.extend(str(eid) for eid in exclude)

    query = f"""
        SELECT a.id, a.username, a.current_state, a.followers,
               a.last_post_at, a.warming_day_count
        FROM accounts a
        JOIN niches n ON a.niche_id = n.id
        WHERE a.platform = %s
          AND n.slug = %s
          AND a.current_state = 'active'
          {exclude_clause}
        ORDER BY a.followers DESC
    """
    return await db.execute(query, tuple(params))


async def get_account_for_posting(platform: Platform, niche_slug: str) -> dict | None:
    """Select the best account for posting, respecting cooldowns and limits."""
    accounts = await get_available_accounts(platform, niche_slug)

    cutoff = datetime.utcnow() - timedelta(hours=12)
    for account in accounts:
        # Skip if posted too recently (12h minimum gap for most platforms)
        if account.get("last_post_at") and account["last_post_at"] > cutoff:
            continue
        return account

    return None


async def record_post(account_id: UUID, platform: str) -> None:
    """Update account's last_post_at after a successful post."""
    await db.execute(
        "UPDATE accounts SET last_post_at = NOW() WHERE id = %s",
        (str(account_id),),
    )


async def set_account_state(account_id: UUID, state: AccountState) -> None:
    """Transition an account to a new state."""
    await db.execute(
        "UPDATE accounts SET current_state = %s, updated_at = NOW() WHERE id = %s",
        (state.value, str(account_id)),
    )


async def get_warming_accounts() -> list[dict]:
    """Get all accounts currently in warming phases."""
    return await db.execute("""
        SELECT a.id, a.platform, a.username, a.current_state, a.warming_day_count,
               a.device_id, n.slug as niche_slug
        FROM accounts a
        JOIN niches n ON a.niche_id = n.id
        WHERE a.current_state IN ('warming_p1', 'warming_p2', 'warming_p3')
        ORDER BY a.warming_day_count ASC
    """)
