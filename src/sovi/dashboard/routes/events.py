"""Events API + SSE stream + HTML page."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from starlette.responses import StreamingResponse

from sovi.dashboard.app import templates
from sovi.db import execute
from sovi.events import async_get_events, async_get_unresolved, async_resolve

router = APIRouter(tags=["events"])


# --- HTML ---

@router.get("/events", response_class=HTMLResponse)
async def events_page(request: Request):
    events = await async_get_events(limit=50)
    return templates.TemplateResponse("events.html", {"request": request, "events": events})


# --- API ---

@router.get("/api/events")
async def list_events(
    severity: str | None = Query(None),
    category: str | None = Query(None),
    event_type: str | None = Query(None),
    device_id: str | None = Query(None),
    account_id: str | None = Query(None),
    resolved: bool | None = Query(None),
    limit: int = Query(100, le=500),
):
    return await async_get_events(
        severity=severity,
        category=category,
        event_type=event_type,
        device_id=device_id,
        account_id=account_id,
        resolved=resolved,
        limit=limit,
    )


@router.get("/api/events/unresolved")
async def unresolved_events(
    severity: str | None = Query(None),
    category: str | None = Query(None),
    limit: int = Query(50),
):
    return await async_get_unresolved(severity=severity, category=category, limit=limit)


@router.post("/api/events/{event_id}/resolve")
async def resolve_event(event_id: int, resolved_by: str = "human"):
    ok = await async_resolve(event_id, resolved_by=resolved_by)
    return {"ok": ok}


def _json_serial(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


@router.get("/api/logs/stream")
async def stream_events():
    """SSE endpoint — real-time event stream."""

    async def event_generator():
        last_id = 0
        while True:
            rows = await execute(
                """SELECT id, timestamp, category, severity, event_type,
                          device_id, account_id, message, context
                   FROM system_events
                   WHERE id > %s
                   ORDER BY id
                   LIMIT 20""",
                (last_id,),
            )
            for row in rows:
                last_id = row["id"]
                data = json.dumps(row, default=_json_serial)
                yield f"data: {data}\n\n"

            await asyncio.sleep(2)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
