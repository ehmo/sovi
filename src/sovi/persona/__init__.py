"""Persona-first account creation pipeline.

Orchestrates persona generation, photo creation, email account setup,
and platform account creation for each persona.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sovi.db import sync_execute, sync_execute_one
from sovi.persona.generator import generate_personas
from sovi.persona.photos import generate_persona_photos

logger = logging.getLogger(__name__)


def create_persona_batch(niche_slug: str, count: int = 10) -> list[str]:
    """Full pipeline: generate personas and store in DB.

    Photos are generated separately (can be slow/expensive).
    Returns list of persona IDs.
    """
    niche = sync_execute_one(
        "SELECT id, slug, name FROM niches WHERE slug = %s AND status = 'active'",
        (niche_slug,),
    )
    if not niche:
        raise ValueError(f"Niche not found or inactive: {niche_slug}")

    niche_id = str(niche["id"])

    # Check how many personas already exist for this niche
    existing = sync_execute_one(
        "SELECT COUNT(*) as cnt FROM personas WHERE niche_id = %s",
        (niche_id,),
    )
    existing_count = existing["cnt"] if existing else 0
    logger.info("Niche %s has %d existing personas, generating %d more", niche_slug, existing_count, count)

    # Generate persona data via LLM
    persona_dicts = generate_personas(niche_id, niche_slug, count)
    if not persona_dicts:
        logger.error("No personas generated for %s", niche_slug)
        return []

    persona_ids: list[str] = []
    for p in persona_dicts:
        row = sync_execute_one(
            """INSERT INTO personas
               (niche_id, first_name, last_name, display_name, username_base,
                gender, date_of_birth, age, country, state, city,
                occupation, bio_short, bio_long, interests, personality, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'ready')
               RETURNING id""",
            (
                niche_id,
                p["first_name"], p["last_name"], p["display_name"], p["username_base"],
                p["gender"], p["date_of_birth"], p["age"],
                p.get("country", "US"), p.get("state"), p.get("city"),
                p.get("occupation"), p["bio_short"], p.get("bio_long"),
                p.get("interests", []), p.get("personality", "{}"),
            ),
        )
        if row:
            persona_ids.append(str(row["id"]))
            logger.info("Created persona: %s (%s)", p["display_name"], row["id"])

    logger.info("Created %d personas for niche %s", len(persona_ids), niche_slug)
    return persona_ids


def generate_photos_for_pending(limit: int = 10) -> int:
    """Generate photos for personas that don't have them yet.

    Returns count of personas processed.
    """
    rows = sync_execute(
        """SELECT id, first_name, last_name, display_name, gender, age,
                  occupation, bio_short, interests, photo_style
           FROM personas
           WHERE photos_generated = false AND status IN ('ready', 'active')
           ORDER BY created_at ASC
           LIMIT %s""",
        (limit,),
    )

    count = 0
    for persona in rows:
        persona_id = str(persona["id"])
        try:
            paths = generate_persona_photos(persona)
            if paths:
                sync_execute(
                    "UPDATE personas SET photos_generated = true, updated_at = now() WHERE id = %s",
                    (persona_id,),
                )
                count += 1
                logger.info("Generated %d photos for %s", len(paths), persona["display_name"])
        except Exception:
            logger.error("Photo generation failed for %s", persona["display_name"], exc_info=True)

    return count
