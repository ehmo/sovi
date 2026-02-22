"""Account management API + HTML page."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from sovi.dashboard.app import templates
from sovi.db import execute, execute_one

router = APIRouter(tags=["accounts"])


class AccountCreate(BaseModel):
    platform: str
    username: str
    niche_slug: str
    email: str | None = None
    password: str | None = None


class AccountUpdate(BaseModel):
    current_state: str | None = None
    username: str | None = None
    niche_id: str | None = None


# --- HTML ---

@router.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request):
    accounts = await _list_accounts()
    return templates.TemplateResponse("accounts.html", {"request": request, "accounts": accounts})


# --- API ---

@router.get("/api/accounts")
async def list_accounts(
    platform: str | None = Query(None),
    status: str | None = Query(None),
    niche: str | None = Query(None),
    limit: int = Query(100, le=500),
):
    return await _list_accounts(platform=platform, status=status, niche=niche, limit=limit)


@router.get("/api/accounts/{account_id}")
async def get_account(account_id: str):
    account = await execute_one(
        """SELECT a.*, n.name as niche_name, n.slug as niche_slug,
                  d.name as device_name
           FROM accounts a
           LEFT JOIN niches n ON a.niche_id = n.id
           LEFT JOIN devices d ON a.device_id = d.id
           WHERE a.id = %s""",
        (account_id,),
    )
    if not account:
        return {"error": "Account not found"}, 404

    # Get recent events for this account
    events = await execute(
        """SELECT id, timestamp, category, severity, event_type, message, context
           FROM system_events
           WHERE account_id = %s
           ORDER BY timestamp DESC LIMIT 20""",
        (account_id,),
    )

    return {**account, "recent_events": events}


@router.post("/api/accounts")
async def create_account(body: AccountCreate):
    # Look up niche ID
    niche = await execute_one(
        "SELECT id FROM niches WHERE slug = %s", (body.niche_slug,)
    )
    if not niche:
        return {"error": f"Niche not found: {body.niche_slug}"}, 400

    rows = await execute(
        """INSERT INTO accounts (platform, username, niche_id, current_state)
           VALUES (%s, %s, %s, 'created')
           RETURNING id, platform, username, current_state""",
        (body.platform, body.username, str(niche["id"])),
    )
    return rows[0] if rows else {"error": "Failed to create account"}


@router.patch("/api/accounts/{account_id}")
async def update_account(account_id: str, body: AccountUpdate):
    sets: list[str] = []
    params: list[Any] = []

    if body.current_state is not None:
        sets.append("current_state = %s")
        params.append(body.current_state)
    if body.username is not None:
        sets.append("username = %s")
        params.append(body.username)
    if body.niche_id is not None:
        sets.append("niche_id = %s")
        params.append(body.niche_id)

    if not sets:
        return {"error": "No fields to update"}

    sets.append("updated_at = now()")
    params.append(account_id)

    await execute(
        f"UPDATE accounts SET {', '.join(sets)} WHERE id = %s",
        tuple(params),
    )
    return {"ok": True}


@router.post("/api/accounts/{account_id}/retry-login")
async def retry_login(account_id: str):
    """Queue a login retry for an account.

    This emits an event that the scheduler can pick up,
    or an LLM agent can act on.
    """
    from sovi.events import async_emit

    account = await execute_one(
        "SELECT id, platform, username FROM accounts WHERE id = %s",
        (account_id,),
    )
    if not account:
        return {"error": "Account not found"}, 404

    await async_emit(
        "account", "info", "login_retry_requested",
        f"Login retry requested for {account['platform']}/{account['username']}",
        account_id=account_id,
        context={"platform": account["platform"], "username": account["username"]},
    )

    return {"ok": True, "message": "Login retry queued"}


async def _list_accounts(
    platform: str | None = None,
    status: str | None = None,
    niche: str | None = None,
    limit: int = 100,
) -> list[dict]:
    conditions: list[str] = ["a.deleted_at IS NULL"]
    params: list[Any] = []

    if platform:
        conditions.append("a.platform = %s")
        params.append(platform)
    if status:
        conditions.append("a.current_state = %s")
        params.append(status)
    if niche:
        conditions.append("n.slug = %s")
        params.append(niche)

    where = " AND ".join(conditions)
    params.append(limit)

    return await execute(
        f"""SELECT a.id, a.platform, a.username, a.current_state,
                   a.warming_day_count, a.followers, a.last_warmed_at,
                   a.last_activity_at, a.created_at,
                   n.name as niche_name, n.slug as niche_slug,
                   d.name as device_name
            FROM accounts a
            LEFT JOIN niches n ON a.niche_id = n.id
            LEFT JOIN devices d ON a.device_id = d.id
            WHERE {where}
            ORDER BY a.created_at DESC
            LIMIT %s""",
        tuple(params),
    )
