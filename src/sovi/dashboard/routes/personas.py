"""Persona management dashboard routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from sovi.dashboard.app import templates
from sovi.db import execute, execute_one

router = APIRouter(tags=["personas"])


# --- HTML ---

@router.get("/personas", response_class=HTMLResponse)
async def personas_page(request: Request):
    personas = await _list_personas()
    pipeline = await _pipeline_stats()
    return templates.TemplateResponse(
        "personas.html",
        {"request": request, "personas": personas, "pipeline": pipeline},
    )


@router.get("/personas/{persona_id}", response_class=HTMLResponse)
async def persona_detail_page(request: Request, persona_id: str):
    persona = await _get_persona(persona_id)
    if not persona:
        return templates.TemplateResponse(
            "personas.html",
            {"request": request, "personas": [], "pipeline": {}, "error": "Persona not found"},
        )
    return templates.TemplateResponse(
        "persona_detail.html",
        {"request": request, "persona": persona},
    )


# --- API ---

@router.get("/api/personas")
async def list_personas_api(
    niche: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(100, le=500),
):
    return await _list_personas(niche=niche, status=status, limit=limit)


@router.get("/api/personas/{persona_id}")
async def get_persona_api(persona_id: str):
    persona = await _get_persona(persona_id)
    if not persona:
        raise HTTPException(status_code=404, detail="Persona not found")
    return persona


@router.get("/api/personas/pipeline/stats")
async def pipeline_stats_api():
    return await _pipeline_stats()


# --- Helpers ---

async def _list_personas(
    niche: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict]:
    conditions: list[str] = []
    params: list[Any] = []

    if niche:
        conditions.append("n.slug = %s")
        params.append(niche)
    if status:
        conditions.append("p.status = %s")
        params.append(status)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)

    personas = await execute(
        f"""SELECT p.id, p.first_name, p.last_name, p.display_name,
                   p.username_base, p.gender, p.age, p.occupation,
                   p.bio_short, p.status, p.photos_generated,
                   p.created_at,
                   n.name as niche_name, n.slug as niche_slug
            FROM personas p
            JOIN niches n ON p.niche_id = n.id
            {where}
            ORDER BY p.created_at DESC
            LIMIT %s""",
        tuple(params),
    )

    # Get account status matrix for each persona
    for persona in personas:
        pid = str(persona["id"])
        accounts = await execute(
            """SELECT platform, current_state, warming_day_count
               FROM accounts
               WHERE persona_id = %s AND deleted_at IS NULL""",
            (pid,),
        )
        persona["accounts"] = {a["platform"]: a for a in accounts}

        # Check email status
        email = await execute_one(
            "SELECT id, status FROM email_accounts WHERE persona_id = %s LIMIT 1",
            (pid,),
        )
        persona["has_email"] = email is not None

    return personas


async def _get_persona(persona_id: str) -> dict | None:
    persona = await execute_one(
        """SELECT p.*, n.name as niche_name, n.slug as niche_slug
           FROM personas p
           JOIN niches n ON p.niche_id = n.id
           WHERE p.id = %s""",
        (persona_id,),
    )
    if not persona:
        return None

    # Get photos
    photos = await execute(
        """SELECT id, file_path, photo_type, is_primary, created_at
           FROM persona_photos
           WHERE persona_id = %s
           ORDER BY is_primary DESC, created_at ASC""",
        (persona_id,),
    )
    persona["photos"] = photos

    # Get email accounts
    emails = await execute(
        """SELECT id, provider, domain, status, created_at
           FROM email_accounts
           WHERE persona_id = %s
           ORDER BY created_at DESC""",
        (persona_id,),
    )
    persona["emails"] = emails

    # Get platform accounts
    accounts = await execute(
        """SELECT id, platform, username, current_state, warming_day_count,
                  followers, last_warmed_at, created_at
           FROM accounts
           WHERE persona_id = %s AND deleted_at IS NULL
           ORDER BY platform""",
        (persona_id,),
    )
    persona["accounts"] = accounts

    return persona


async def _pipeline_stats() -> dict:
    """Get pipeline progress statistics."""
    total_personas = await execute_one(
        "SELECT COUNT(*) as cnt FROM personas"
    )
    ready_personas = await execute_one(
        "SELECT COUNT(*) as cnt FROM personas WHERE status = 'ready'"
    )
    with_email = await execute_one(
        """SELECT COUNT(DISTINCT p.id) as cnt
           FROM personas p
           JOIN email_accounts ea ON ea.persona_id = p.id"""
    )
    with_photos = await execute_one(
        "SELECT COUNT(*) as cnt FROM personas WHERE photos_generated = true"
    )

    # Accounts per platform
    platform_counts = await execute(
        """SELECT a.platform, COUNT(*) as cnt
           FROM accounts a
           JOIN personas p ON a.persona_id = p.id
           WHERE a.deleted_at IS NULL
           GROUP BY a.platform
           ORDER BY a.platform"""
    )

    total = total_personas["cnt"] if total_personas else 0
    emails = with_email["cnt"] if with_email else 0

    # Total possible accounts = personas * 6 platforms
    total_possible_accounts = total * 6
    total_accounts = sum(p["cnt"] for p in platform_counts)

    return {
        "total_personas": total,
        "ready_personas": ready_personas["cnt"] if ready_personas else 0,
        "with_email": emails,
        "with_photos": with_photos["cnt"] if with_photos else 0,
        "email_pct": round(emails / total * 100) if total else 0,
        "accounts_by_platform": {p["platform"]: p["cnt"] for p in platform_counts},
        "total_accounts": total_accounts,
        "total_possible_accounts": total_possible_accounts,
        "accounts_pct": round(total_accounts / total_possible_accounts * 100) if total_possible_accounts else 0,
    }
