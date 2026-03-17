"""Device role management — seeder/warmer assignment and rotation.

10 phones: 2 seeders (create emails + accounts), 8 warmers (daily warming).
Roles rotate every 4-6 hours with random jitter. A demoted seeder enters
a 30-minute cooldown before resuming warming.

All role state lives in the DB:
- devices.current_role (denormalized cache for scheduler hot path)
- device_role_assignments (history with rotation_id for audit)
"""

from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sovi import events
from sovi.db import sync_conn, sync_execute, sync_execute_one

logger = logging.getLogger(__name__)

# Role constants matching the device_role enum
SEEDER = "seeder"
WARMER = "warmer"
IDLE = "idle"

# Configuration
SEEDER_COUNT = 2
ROTATION_INTERVAL_MIN_H = 4
ROTATION_INTERVAL_MAX_H = 6
SEEDER_COOLDOWN_MINUTES = 30


# ---------------------------------------------------------------------------
# Role queries
# ---------------------------------------------------------------------------


def get_current_role(device_id: str) -> str:
    """Get the current role for a device. Returns 'idle' if unassigned."""
    row = sync_execute_one(
        'SELECT "current_role" FROM devices WHERE id = %s',
        (device_id,),
    )
    return row["current_role"] if row and row["current_role"] else IDLE


def get_seeders() -> list[dict[str, Any]]:
    """Get all devices currently assigned as seeders."""
    return sync_execute(
        """SELECT id, name, udid, wda_port, "current_role",
                  role_changed_at, seeder_cooldown_until, status
           FROM devices
           WHERE "current_role" = 'seeder' AND status = 'active'
           ORDER BY role_changed_at""",
    )


def get_warmers() -> list[dict[str, Any]]:
    """Get all active warmer devices."""
    return sync_execute(
        """SELECT id, name, udid, wda_port, "current_role",
                  role_changed_at, seeder_cooldown_until, status
           FROM devices
           WHERE "current_role" = 'warmer' AND status = 'active'
           ORDER BY name""",
    )


def is_in_cooldown(device_id: str) -> bool:
    """Check if device is in post-seeder cooldown."""
    row = sync_execute_one(
        "SELECT seeder_cooldown_until FROM devices WHERE id = %s",
        (device_id,),
    )
    if not row or not row["seeder_cooldown_until"]:
        return False
    cooldown_until: datetime = row["seeder_cooldown_until"]
    if cooldown_until.tzinfo is None:
        cooldown_until = cooldown_until.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) < cooldown_until


def get_warmer_with_fewest_bindings() -> str | None:
    """Pick the warmer device with the fewest active account bindings.

    Used when a seeder creates a new account and needs to assign it
    to a warmer for the warming pipeline.
    """
    row = sync_execute_one(
        """SELECT d.id
           FROM devices d
           LEFT JOIN device_account_bindings dab
                ON d.id = dab.device_id AND dab.unbound_at IS NULL
           WHERE d."current_role" = 'warmer' AND d.status = 'active'
             AND (d.seeder_cooldown_until IS NULL OR d.seeder_cooldown_until < now())
           GROUP BY d.id
           ORDER BY COUNT(dab.id) ASC, random()
           LIMIT 1""",
    )
    return str(row["id"]) if row else None


# ---------------------------------------------------------------------------
# Role assignment
# ---------------------------------------------------------------------------


def bootstrap_roles() -> None:
    """Initial role assignment — pick 2 random seeders, rest are warmers.

    Only runs if no devices have roles assigned (all are 'idle' or NULL).
    """
    active = sync_execute(
        "SELECT id, name FROM devices WHERE status = 'active' ORDER BY random()",
    )
    if not active:
        logger.warning("No active devices to bootstrap roles")
        return

    # Check if roles already assigned
    assigned = sync_execute(
        """SELECT id FROM devices WHERE "current_role" IS NOT NULL AND "current_role" != 'idle' AND status = 'active'""",
    )
    if assigned:
        logger.info("Roles already assigned to %d devices, skipping bootstrap", len(assigned))
        return

    rotation_id = str(uuid4())
    seeders = active[:SEEDER_COUNT]
    warmers = active[SEEDER_COUNT:]

    with sync_conn() as conn:
        with conn.cursor() as cur:
            for device in seeders:
                device_id = str(device["id"])
                cur.execute(
                    """UPDATE devices SET "current_role" = 'seeder', role_changed_at = now()
                       WHERE id = %s""",
                    (device_id,),
                )
                cur.execute(
                    """INSERT INTO device_role_assignments (device_id, role, rotation_id)
                       VALUES (%s, 'seeder', %s)""",
                    (device_id, rotation_id),
                )
                logger.info("Bootstrapped %s as seeder", device["name"])

            for device in warmers:
                device_id = str(device["id"])
                cur.execute(
                    """UPDATE devices SET "current_role" = 'warmer', role_changed_at = now()
                       WHERE id = %s""",
                    (device_id,),
                )
                cur.execute(
                    """INSERT INTO device_role_assignments (device_id, role, rotation_id)
                       VALUES (%s, 'warmer', %s)""",
                    (device_id, rotation_id),
                )

        conn.commit()

    events.emit("scheduler", "info", "roles_bootstrapped",
                f"Bootstrapped roles: {len(seeders)} seeders, {len(warmers)} warmers",
                context={
                    "seeders": [d["name"] for d in seeders],
                    "warmers": [d["name"] for d in warmers],
                    "rotation_id": rotation_id,
                })


def execute_rotation() -> dict[str, Any]:
    """Execute a role rotation — pick 2 new seeders from warmer pool.

    Returns dict with rotation details or error info.
    """
    rotation_id = str(uuid4())

    # Check no in-progress seeder tasks
    in_progress = sync_execute(
        """SELECT id, claimed_by FROM seeder_tasks
           WHERE status = 'in_progress'""",
    )
    if in_progress:
        # Wait briefly for tasks to complete
        logger.info("Waiting for %d in-progress seeder tasks before rotation", len(in_progress))
        for _ in range(20):  # wait up to 10 min
            time.sleep(30)
            remaining = sync_execute(
                "SELECT id FROM seeder_tasks WHERE status = 'in_progress'",
            )
            if not remaining:
                break
        else:
            # Force-cancel stuck tasks
            sync_execute(
                """UPDATE seeder_tasks SET status = 'cancelled', updated_at = now()
                   WHERE status = 'in_progress'""",
            )
            logger.warning("Force-cancelled %d stuck seeder tasks for rotation", len(in_progress))

    # Get current seeders
    current_seeders = get_seeders()
    current_seeder_ids = {str(s["id"]) for s in current_seeders}

    # Pick 2 new seeders from eligible warmers
    eligible = sync_execute(
        """SELECT id, name FROM devices
           WHERE "current_role" = 'warmer' AND status = 'active'
             AND (seeder_cooldown_until IS NULL OR seeder_cooldown_until < now())
           ORDER BY role_changed_at ASC NULLS FIRST""",
    )

    if len(eligible) < SEEDER_COUNT:
        logger.error("Not enough eligible warmers for rotation (have %d, need %d)",
                     len(eligible), SEEDER_COUNT)
        events.emit("scheduler", "error", "rotation_failed",
                     "Not enough eligible warmer devices for rotation",
                     context={"eligible": len(eligible), "needed": SEEDER_COUNT})
        return {"error": "not_enough_warmers", "eligible": len(eligible)}

    # Pick from top 4 eligible (prefer least-recently-seeded), with randomness
    pool = eligible[:min(4, len(eligible))]
    new_seeders = random.sample(pool, min(SEEDER_COUNT, len(pool)))
    new_seeder_ids = [str(s["id"]) for s in new_seeders]

    # Execute rotation in a single transaction
    with sync_conn() as conn:
        with conn.cursor() as cur:
            # Demote current seeders
            for seeder in current_seeders:
                sid = str(seeder["id"])
                if sid in new_seeder_ids:
                    continue  # staying as seeder (unlikely but possible)
                # End seeder assignment
                cur.execute(
                    """UPDATE device_role_assignments SET ended_at = now()
                       WHERE device_id = %s AND role = 'seeder' AND ended_at IS NULL""",
                    (sid,),
                )
                # Create warmer assignment with cooldown
                cur.execute(
                    """INSERT INTO device_role_assignments
                       (device_id, role, rotation_id, cooldown_until)
                       VALUES (%s, 'warmer', %s, now() + interval '%s minutes')""",
                    (sid, rotation_id, SEEDER_COOLDOWN_MINUTES),
                )
                # Update device
                cur.execute(
                    """UPDATE devices SET "current_role" = 'warmer', role_changed_at = now(),
                       seeder_cooldown_until = now() + interval '%s minutes'
                       WHERE id = %s""",
                    (SEEDER_COOLDOWN_MINUTES, sid),
                )

            # Promote new seeders
            for seeder in new_seeders:
                sid = str(seeder["id"])
                if sid in current_seeder_ids:
                    continue  # already a seeder
                # End warmer assignment
                cur.execute(
                    """UPDATE device_role_assignments SET ended_at = now()
                       WHERE device_id = %s AND role = 'warmer' AND ended_at IS NULL""",
                    (sid,),
                )
                # Create seeder assignment
                cur.execute(
                    """INSERT INTO device_role_assignments (device_id, role, rotation_id)
                       VALUES (%s, 'seeder', %s)""",
                    (sid, rotation_id),
                )
                # Update device
                cur.execute(
                    """UPDATE devices SET "current_role" = 'seeder', role_changed_at = now(),
                       seeder_cooldown_until = NULL
                       WHERE id = %s""",
                    (sid,),
                )

        conn.commit()

    demoted = [s["name"] for s in current_seeders if str(s["id"]) not in new_seeder_ids]
    promoted = [s["name"] for s in new_seeders if str(s["id"]) not in current_seeder_ids]

    events.emit("scheduler", "info", "role_rotation",
                f"Rotation {rotation_id[:8]}: promoted {promoted}, demoted {demoted}",
                context={
                    "rotation_id": rotation_id,
                    "promoted_to_seeder": promoted,
                    "demoted_to_warmer": demoted,
                    "cooldown_minutes": SEEDER_COOLDOWN_MINUTES,
                })

    logger.info("Rotation complete: promoted=%s, demoted=%s", promoted, demoted)
    return {
        "rotation_id": rotation_id,
        "promoted": promoted,
        "demoted": demoted,
    }


# ---------------------------------------------------------------------------
# Seeder task management
# ---------------------------------------------------------------------------


def populate_seeder_tasks() -> int:
    """Generate seeder_tasks for personas that need emails or platform accounts.

    Returns number of tasks created.
    """
    created = 0

    # Email tasks: personas without email accounts
    email_tasks = sync_execute(
        """SELECT p.id as persona_id
           FROM personas p
           LEFT JOIN email_accounts ea ON ea.persona_id = p.id
           LEFT JOIN seeder_tasks st ON st.persona_id = p.id
                AND st.task_type = 'create_email'
                AND st.status NOT IN ('failed', 'cancelled')
           WHERE p.status = 'ready'
             AND ea.id IS NULL
             AND st.id IS NULL
           ORDER BY p.created_at ASC""",
    )
    seen_email_personas: set[str] = set()
    for row in email_tasks:
        persona_id = str(row["persona_id"])
        if persona_id in seen_email_personas:
            continue
        seen_email_personas.add(persona_id)
        sync_execute(
            """INSERT INTO seeder_tasks (persona_id, platform, task_type)
               VALUES (%s, 'tiktok', 'create_email')
               ON CONFLICT DO NOTHING""",
            (persona_id,),
        )
        created += 1

    # Account tasks: personas with email but missing platform accounts
    platforms = ["tiktok", "instagram", "reddit", "youtube_shorts", "x_twitter", "facebook", "linkedin"]
    for platform in platforms:
        account_tasks = sync_execute(
            """SELECT p.id as persona_id
               FROM personas p
               JOIN email_accounts ea ON ea.persona_id = p.id
               LEFT JOIN accounts a ON a.persona_id = p.id
                    AND a.platform = %s::platform_type
                    AND a.deleted_at IS NULL
               LEFT JOIN seeder_tasks st ON st.persona_id = p.id
                    AND st.platform = %s::platform_type
                    AND st.task_type = 'create_account'
                    AND st.status NOT IN ('failed', 'cancelled')
               WHERE p.status = 'ready'
                 AND a.id IS NULL
                 AND st.id IS NULL
               ORDER BY p.created_at ASC""",
            (platform, platform),
        )
        seen_platform_personas: set[str] = set()
        for row in account_tasks:
            persona_id = str(row["persona_id"])
            if persona_id in seen_platform_personas:
                continue
            seen_platform_personas.add(persona_id)
            sync_execute(
                """INSERT INTO seeder_tasks (persona_id, platform, task_type)
                   VALUES (%s, %s::platform_type, 'create_account')""",
                (persona_id, platform),
            )
            created += 1

    if created:
        logger.info("Created %d seeder tasks", created)
    return created


def claim_seeder_task(device_id: str) -> dict[str, Any] | None:
    """Claim the next pending seeder task for a device.

    Priority: create_email > create_account (email must exist first).
    Uses FOR UPDATE SKIP LOCKED for safe concurrent claiming.
    """
    with sync_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT st.id, st.persona_id, st.platform, st.task_type,
                          p.first_name, p.last_name, p.display_name,
                          p.username_base, p.gender, p.date_of_birth, p.age,
                          p.niche_id, p.bio_short, p.occupation, p.interests
                   FROM seeder_tasks st
                   JOIN personas p ON p.id = st.persona_id
                   WHERE st.status = 'pending'
                     AND st.attempts < st.max_attempts
                   ORDER BY
                       CASE st.task_type
                           WHEN 'create_email' THEN 0
                           WHEN 'create_account' THEN 1
                       END,
                       st.created_at ASC
                   LIMIT 1
                   FOR UPDATE OF st SKIP LOCKED""",
                (),
            )
            row = cur.fetchone()
            if not row:
                conn.commit()
                return None

            task = dict(row)

            # Claim it
            cur.execute(
                """UPDATE seeder_tasks SET
                       status = 'claimed', claimed_by = %s, claimed_at = now(),
                       attempts = attempts + 1, updated_at = now()
                   WHERE id = %s""",
                (device_id, str(task["id"])),
            )
            conn.commit()

    return task


def complete_seeder_task(task_id: str, result_id: str | None = None) -> None:
    """Mark a seeder task as completed."""
    sync_execute(
        """UPDATE seeder_tasks SET
               status = 'completed', completed_at = now(),
               result_id = %s, updated_at = now()
           WHERE id = %s""",
        (result_id, task_id),
    )


def fail_seeder_task(task_id: str, error: str) -> None:
    """Mark a seeder task as failed. Will be retried if attempts < max_attempts."""
    # Check if we should mark as permanently failed
    row = sync_execute_one(
        "SELECT attempts, max_attempts FROM seeder_tasks WHERE id = %s",
        (task_id,),
    )
    if row and row["attempts"] >= row["max_attempts"]:
        status = "failed"
    else:
        status = "pending"  # reset to pending for retry

    sync_execute(
        """UPDATE seeder_tasks SET
               status = %s,
               error_message = %s,
               claimed_by = NULL,
               claimed_at = NULL,
               updated_at = now()
           WHERE id = %s""",
        (status, error, task_id),
    )


def recover_interrupted_seeder_tasks() -> int:
    """Requeue or fail claimed tasks left behind by a prior scheduler process."""
    rows = sync_execute(
        """SELECT id, attempts, max_attempts
           FROM seeder_tasks
           WHERE status IN ('claimed', 'in_progress')""",
    )
    if not rows:
        return 0

    recovered = 0
    interrupted_reason = "Interrupted by scheduler restart"
    for row in rows:
        task_id = str(row["id"])
        status = "failed" if row["attempts"] >= row["max_attempts"] else "pending"
        sync_execute(
            """UPDATE seeder_tasks SET
                   status = %s,
                   error_message = CASE
                       WHEN error_message IS NULL OR error_message = '' THEN %s
                       ELSE error_message
                   END,
                   claimed_by = NULL,
                   claimed_at = NULL,
                   updated_at = now()
               WHERE id = %s""",
            (status, interrupted_reason, task_id),
        )
        recovered += 1

    events.emit(
        "scheduler",
        "warning",
        "seeder_tasks_recovered",
        f"Recovered {recovered} interrupted seeder tasks",
        context={"recovered": recovered},
    )
    logger.warning("Recovered %d interrupted seeder tasks", recovered)
    return recovered


def dedupe_open_seeder_tasks() -> int:
    """Cancel duplicate open seeder tasks, keeping the oldest task per key."""
    rows = sync_execute(
        """SELECT id, persona_id, platform, task_type
           FROM seeder_tasks
           WHERE status IN ('pending', 'claimed', 'in_progress')
           ORDER BY created_at ASC, id ASC""",
    )
    if not rows:
        return 0

    cancelled = 0
    seen: set[tuple[str, str, str]] = set()
    duplicate_reason = "Cancelled duplicate seeder task"
    for row in rows:
        task_key = (
            str(row["persona_id"]),
            str(row["platform"]),
            str(row["task_type"]),
        )
        if task_key in seen:
            sync_execute(
                """UPDATE seeder_tasks SET
                       status = 'cancelled',
                       error_message = %s,
                       claimed_by = NULL,
                       claimed_at = NULL,
                       updated_at = now()
                   WHERE id = %s""",
                (duplicate_reason, str(row["id"])),
            )
            cancelled += 1
            continue
        seen.add(task_key)

    if cancelled:
        events.emit(
            "scheduler",
            "warning",
            "seeder_tasks_deduped",
            f"Cancelled {cancelled} duplicate seeder tasks",
            context={"cancelled": cancelled},
        )
        logger.warning("Cancelled %d duplicate seeder tasks", cancelled)
    return cancelled


# ---------------------------------------------------------------------------
# Role rotation thread
# ---------------------------------------------------------------------------


class RoleRotator:
    """Background thread that rotates seeder/warmer roles every 4-6 hours."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the rotation thread."""
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="role-rotator",
            daemon=True,
        )
        self._thread.start()
        logger.info("RoleRotator started")

    def stop(self) -> None:
        """Stop the rotation thread."""
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=30)
        logger.info("RoleRotator stopped")

    def _loop(self) -> None:
        """Main rotation loop."""
        # Bootstrap roles on first run
        try:
            bootstrap_roles()
        except Exception:
            logger.error("Failed to bootstrap roles", exc_info=True)

        while not self._stop.is_set():
            # Random interval: 4-6 hours
            interval = random.uniform(
                ROTATION_INTERVAL_MIN_H * 3600,
                ROTATION_INTERVAL_MAX_H * 3600,
            )
            logger.info("Next rotation in %.1f hours", interval / 3600)

            if self._stop.wait(timeout=interval):
                break  # stop requested

            try:
                result = execute_rotation()
                if "error" in result:
                    logger.error("Rotation failed: %s", result["error"])
                    # Retry in 1 hour
                    self._stop.wait(timeout=3600)
            except Exception:
                logger.error("Rotation exception", exc_info=True)
                self._stop.wait(timeout=3600)

            # Also refresh seeder task queue
            try:
                populate_seeder_tasks()
            except Exception:
                logger.error("Failed to populate seeder tasks", exc_info=True)
