"""Structured event logger for the scheduler and all subsystems.

Every scheduler operation, auth flow, and device action calls emit().
The dashboard and LLM agent consume these events via REST API + SSE.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sovi.db import execute, sync_execute

logger = logging.getLogger(__name__)


def emit(
    category: str,
    severity: str,
    event_type: str,
    message: str,
    *,
    device_id: UUID | str | None = None,
    account_id: UUID | str | None = None,
    context: dict[str, Any] | None = None,
) -> int | None:
    """Insert a structured event into system_events (sync).

    Returns the event ID if successful, None on failure.
    """
    try:
        rows = sync_execute(
            """INSERT INTO system_events
               (category, severity, event_type, message, device_id, account_id, context)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (
                category,
                severity,
                event_type,
                message,
                str(device_id) if device_id else None,
                str(account_id) if account_id else None,
                json.dumps(context or {}),
            ),
        )
        event_id = rows[0]["id"] if rows else None
        logger.info("[event] %s/%s: %s", category, event_type, message)
        return event_id
    except Exception:
        logger.warning("Failed to emit event: %s/%s: %s", category, event_type, message, exc_info=True)
        return None


async def async_emit(
    category: str,
    severity: str,
    event_type: str,
    message: str,
    *,
    device_id: UUID | str | None = None,
    account_id: UUID | str | None = None,
    context: dict[str, Any] | None = None,
) -> int | None:
    """Async version of emit() for use in the dashboard."""
    try:
        rows = await execute(
            """INSERT INTO system_events
               (category, severity, event_type, message, device_id, account_id, context)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (
                category,
                severity,
                event_type,
                message,
                str(device_id) if device_id else None,
                str(account_id) if account_id else None,
                json.dumps(context or {}),
            ),
        )
        return rows[0]["id"] if rows else None
    except Exception:
        logger.warning("Failed to async emit event: %s/%s", category, event_type, exc_info=True)
        return None


def get_unresolved(
    severity: str | None = None,
    category: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Get unresolved events (sync)."""
    conditions = ["resolved = false"]
    params: list[Any] = []

    if severity:
        conditions.append("severity = %s")
        params.append(severity)
    if category:
        conditions.append("category = %s")
        params.append(category)

    where = " AND ".join(conditions)
    params.append(limit)

    return sync_execute(
        f"""SELECT id, timestamp, category, severity, event_type,
                   device_id, account_id, message, context
            FROM system_events
            WHERE {where}
            ORDER BY timestamp DESC
            LIMIT %s""",
        tuple(params),
    )


async def async_get_unresolved(
    severity: str | None = None,
    category: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Get unresolved events (async)."""
    conditions = ["resolved = false"]
    params: list[Any] = []

    if severity:
        conditions.append("severity = %s")
        params.append(severity)
    if category:
        conditions.append("category = %s")
        params.append(category)

    where = " AND ".join(conditions)
    params.append(limit)

    return await execute(
        f"""SELECT id, timestamp, category, severity, event_type,
                   device_id, account_id, message, context,
                   resolved, resolved_by, resolved_at
            FROM system_events
            WHERE {where}
            ORDER BY timestamp DESC
            LIMIT %s""",
        tuple(params),
    )


def resolve(event_id: int, resolved_by: str = "human") -> bool:
    """Mark an event as resolved (sync)."""
    try:
        sync_execute(
            """UPDATE system_events
               SET resolved = true, resolved_by = %s, resolved_at = now()
               WHERE id = %s""",
            (resolved_by, event_id),
        )
        return True
    except Exception:
        logger.warning("Failed to resolve event %d", event_id, exc_info=True)
        return False


async def async_resolve(event_id: int, resolved_by: str = "human") -> bool:
    """Mark an event as resolved (async)."""
    try:
        await execute(
            """UPDATE system_events
               SET resolved = true, resolved_by = %s, resolved_at = now()
               WHERE id = %s""",
            (resolved_by, event_id),
        )
        return True
    except Exception:
        logger.warning("Failed to resolve event %d", event_id, exc_info=True)
        return False


async def async_get_events(
    severity: str | None = None,
    category: str | None = None,
    event_type: str | None = None,
    device_id: str | None = None,
    account_id: str | None = None,
    resolved: bool | None = None,
    limit: int = 100,
    after_id: int | None = None,
) -> list[dict[str, Any]]:
    """Flexible event query for the dashboard API."""
    conditions: list[str] = []
    params: list[Any] = []

    if severity:
        conditions.append("severity = %s")
        params.append(severity)
    if category:
        conditions.append("category = %s")
        params.append(category)
    if event_type:
        conditions.append("event_type = %s")
        params.append(event_type)
    if device_id:
        conditions.append("device_id = %s")
        params.append(device_id)
    if account_id:
        conditions.append("account_id = %s")
        params.append(account_id)
    if resolved is not None:
        conditions.append("resolved = %s")
        params.append(resolved)
    if after_id is not None:
        conditions.append("id > %s")
        params.append(after_id)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)

    return await execute(
        f"""SELECT id, timestamp, category, severity, event_type,
                   device_id, account_id, message, context,
                   resolved, resolved_by, resolved_at
            FROM system_events
            {where}
            ORDER BY id DESC
            LIMIT %s""",
        tuple(params),
    )
