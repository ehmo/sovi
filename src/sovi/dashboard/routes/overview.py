"""Overview page and fleet stats API."""

from __future__ import annotations

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
    # Account counts by platform and state
    accounts = await execute(
        """SELECT platform, current_state, COUNT(*) as cnt
           FROM accounts WHERE deleted_at IS NULL
           GROUP BY platform, current_state
           ORDER BY platform, current_state"""
    )

    # Device counts by status
    devices = await execute(
        "SELECT status, COUNT(*) as cnt FROM devices GROUP BY status"
    )

    # Total accounts
    total_row = await execute_one(
        "SELECT COUNT(*) as total FROM accounts WHERE deleted_at IS NULL"
    )
    total_accounts = total_row["total"] if total_row else 0

    # Active devices
    active_row = await execute_one(
        "SELECT COUNT(*) as active FROM devices WHERE status = 'active'"
    )
    active_devices = active_row["active"] if active_row else 0

    # Recent events
    recent_events = await execute(
        """SELECT id, timestamp, category, severity, event_type, message
           FROM system_events
           ORDER BY timestamp DESC LIMIT 10"""
    )

    # Error count (unresolved)
    error_row = await execute_one(
        "SELECT COUNT(*) as cnt FROM system_events WHERE resolved = false AND severity IN ('error', 'critical')"
    )
    error_count = error_row["cnt"] if error_row else 0

    # Sessions today
    sessions_row = await execute_one(
        """SELECT COUNT(*) as cnt FROM system_events
           WHERE event_type = 'warming_complete'
             AND timestamp >= CURRENT_DATE"""
    )
    sessions_today = sessions_row["cnt"] if sessions_row else 0

    # Niche counts
    niches = await execute(
        "SELECT name, slug FROM niches WHERE status = 'active' ORDER BY name"
    )

    return {
        "total_accounts": total_accounts,
        "active_devices": active_devices,
        "error_count": error_count,
        "sessions_today": sessions_today,
        "accounts_by_platform": accounts,
        "devices_by_status": devices,
        "recent_events": recent_events,
        "niches": niches,
    }
