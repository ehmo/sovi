"""Overview page and fleet stats API."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from sovi.dashboard.app import templates
from sovi.db import execute, execute_one

router = APIRouter(tags=["overview"])


@router.get("/", response_class=HTMLResponse)
async def overview_page(request: Request):
    stats = await _fleet_stats()
    return templates.TemplateResponse("overview.html", {"request": request, "stats": stats})


@router.get("/api/overview")
async def overview_api():
    return await _fleet_stats()


async def _fleet_stats() -> dict:
    (
        accounts,
        devices,
        total_row,
        active_row,
        recent_events,
        error_row,
        sessions_row,
        niches,
        persona_total,
        persona_with_email,
        persona_accounts,
    ) = await asyncio.gather(
        execute(
            """SELECT platform, current_state, COUNT(*) as cnt
               FROM accounts WHERE deleted_at IS NULL
               GROUP BY platform, current_state
               ORDER BY platform, current_state"""
        ),
        execute("SELECT status, COUNT(*) as cnt FROM devices GROUP BY status"),
        execute_one("SELECT COUNT(*) as total FROM accounts WHERE deleted_at IS NULL"),
        execute_one("SELECT COUNT(*) as active FROM devices WHERE status IN ('available', 'in_use')"),
        execute(
            """SELECT id, timestamp, category, severity, event_type, message
               FROM system_events
               ORDER BY timestamp DESC LIMIT 10"""
        ),
        execute_one(
            "SELECT COUNT(*) as cnt FROM system_events WHERE resolved = false AND severity IN ('error', 'critical')"
        ),
        execute_one(
            """SELECT COUNT(*) as cnt FROM system_events
               WHERE event_type = 'warming_complete'
                 AND timestamp >= CURRENT_DATE"""
        ),
        execute("SELECT name, slug FROM niches WHERE status = 'active' ORDER BY name"),
        execute_one("SELECT COUNT(*) as cnt FROM personas"),
        execute_one(
            """SELECT COUNT(DISTINCT p.id) as cnt
               FROM personas p
               JOIN email_accounts ea ON ea.persona_id = p.id"""
        ),
        execute_one(
            """SELECT COUNT(*) as cnt FROM accounts
               WHERE persona_id IS NOT NULL AND deleted_at IS NULL"""
        ),
    )

    return {
        "total_accounts": total_row["total"] if total_row else 0,
        "active_devices": active_row["active"] if active_row else 0,
        "error_count": error_row["cnt"] if error_row else 0,
        "sessions_today": sessions_row["cnt"] if sessions_row else 0,
        "accounts_by_platform": accounts,
        "devices_by_status": devices,
        "recent_events": recent_events,
        "niches": niches,
        "total_personas": persona_total["cnt"] if persona_total else 0,
        "personas_with_email": persona_with_email["cnt"] if persona_with_email else 0,
        "total_persona_accounts": persona_accounts["cnt"] if persona_accounts else 0,
    }
