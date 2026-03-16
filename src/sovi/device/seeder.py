"""Seeder pipeline — creates email accounts and platform accounts on-device.

Seeder devices run this pipeline instead of the warmer pipeline.
Each cycle:
1. Claim a seeder_task (email or account creation)
2. Toggle airplane mode (fresh cellular IP)
3. Execute on-device (Safari for email, app for platform)
4. Store result in DB
5. Bind new account to a warmer device
6. Cooldown, repeat

All persona-facing traffic goes through the phone's cellular connection.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any

from sovi import events
from sovi.db import sync_execute
from sovi.device.identity_guard import end_session, start_session
from sovi.device.roles import (
    claim_seeder_task,
    complete_seeder_task,
    fail_seeder_task,
    get_current_role,
    get_warmer_with_fewest_bindings,
    SEEDER,
)
from sovi.device.seeder_email import create_protonmail_email
from sovi.device.wda_client import WDASession

logger = logging.getLogger(__name__)

# Cooldown between seeder tasks (seconds)
SEEDER_COOLDOWN_MIN = 45
SEEDER_COOLDOWN_MAX = 120
# Extra cooldown for email creation (ProtonMail rate limits)
EMAIL_COOLDOWN_EXTRA = 30


def run_seeder_cycle(
    wda: WDASession,
    device_id: str,
    device_name: str,
    *,
    stop_event: threading.Event | None = None,
) -> dict[str, Any] | None:
    """Run one seeder cycle: claim task → execute → store result.

    Returns task result dict or None if no task available.
    Called by the scheduler's seeder loop.
    """
    # Verify still a seeder
    role = get_current_role(device_id)
    if role != SEEDER:
        logger.info("Device %s no longer a seeder (role=%s), skipping", device_name, role)
        return None

    # Claim next task
    task = claim_seeder_task(device_id)
    if not task:
        logger.debug("No seeder tasks available for %s", device_name)
        return None

    task_id = str(task["id"])
    task_type = task["task_type"]
    persona_name = task.get("display_name", "?")

    logger.info(
        "Seeder %s: claimed %s for %s (task %s)",
        device_name, task_type, persona_name, task_id[:8],
    )

    events.emit("scheduler", "info", "seeder_task_claimed",
                f"Seeder {device_name}: {task_type} for {persona_name}",
                device_id=device_id,
                context={
                    "task_id": task_id,
                    "task_type": task_type,
                    "persona_name": persona_name,
                })

    # Start session log
    session_id = start_session(device_id, None, "seeding")

    # Mark task in-progress
    sync_execute(
        "UPDATE seeder_tasks SET status = 'in_progress', updated_at = now() WHERE id = %s",
        (task_id,),
    )

    result = None
    try:
        if task_type == "create_email":
            result = _execute_email_creation(wda, task, device_id, device_name)
        elif task_type == "create_account":
            result = _execute_account_creation(wda, task, device_id, device_name)
        else:
            logger.error("Unknown task type: %s", task_type)
            fail_seeder_task(task_id, f"Unknown task type: {task_type}")

        if result:
            complete_seeder_task(task_id, result_id=result.get("id"))
            events.emit("scheduler", "info", "seeder_task_completed",
                        f"Seeder {device_name}: {task_type} completed for {persona_name}",
                        device_id=device_id,
                        context={
                            "task_id": task_id,
                            "task_type": task_type,
                            "result_id": result.get("id"),
                        })
        else:
            fail_seeder_task(task_id, "Execution returned None")
            events.emit("scheduler", "error", "seeder_task_failed",
                        f"Seeder {device_name}: {task_type} failed for {persona_name}",
                        device_id=device_id,
                        context={"task_id": task_id, "task_type": task_type})

    except Exception as e:
        logger.error("Seeder task failed: %s", e, exc_info=True)
        fail_seeder_task(task_id, str(e))
    finally:
        if session_id:
            outcome = "success" if result else "failed"
            end_session(session_id, outcome)

        # Reset device to home screen
        _reset_device(wda)

    # Cooldown
    cooldown = random.uniform(SEEDER_COOLDOWN_MIN, SEEDER_COOLDOWN_MAX)
    if task_type == "create_email":
        cooldown += EMAIL_COOLDOWN_EXTRA
    logger.info("Seeder %s: cooldown %.0fs", device_name, cooldown)
    if stop_event is not None:
        stop_event.wait(cooldown)
    else:
        time.sleep(cooldown)

    return result


def _persona_from_task(task: dict[str, Any]) -> dict[str, Any]:
    """Build a persona dict from a seeder_task row.

    Shared by email and account creation to avoid duplicating the field mapping.
    """
    return {
        "id": task["persona_id"],
        "niche_id": task.get("niche_id"),
        "first_name": task.get("first_name", ""),
        "last_name": task.get("last_name", ""),
        "display_name": task.get("display_name", ""),
        "username_base": task.get("username_base", "user"),
        "gender": task.get("gender", ""),
        "date_of_birth": str(task.get("date_of_birth", "")),
        "age": task.get("age", 28),
        "bio_short": task.get("bio_short", ""),
        "occupation": task.get("occupation", ""),
        "interests": task.get("interests") or [],
    }


def _execute_email_creation(
    wda: WDASession,
    task: dict[str, Any],
    device_id: str,
    device_name: str,
) -> dict[str, Any] | None:
    """Create a ProtonMail email account on-device."""
    persona = _persona_from_task(task)
    result = create_protonmail_email(wda, persona, device_id=device_id)
    return result


def _execute_account_creation(
    wda: WDASession,
    task: dict[str, Any],
    device_id: str,
    device_name: str,
) -> dict[str, Any] | None:
    """Create a platform account on-device.

    Uses the existing account_creator module which drives app/Safari via WDA.
    """
    from sovi.persona.account_creator import create_account_for_persona

    persona = _persona_from_task(task)

    platform = task["platform"]

    # Toggle airplane mode for fresh IP
    wda.toggle_airplane_mode()
    time.sleep(6)

    result = create_account_for_persona(wda, persona, platform, device_id=device_id)

    if result:
        account_id = result.get("id") or result.get("account_id")
        if account_id:
            # Bind to a warmer device
            warmer_id = get_warmer_with_fewest_bindings()
            if warmer_id:
                _bind_account_to_warmer(str(account_id), warmer_id, device_id)
            else:
                logger.warning("No warmer available to bind account %s", account_id)

            # Record seeder device
            sync_execute(
                """UPDATE accounts SET seeded_by_device_id = %s, seeded_at = now()
                   WHERE id = %s""",
                (device_id, str(account_id)),
            )

    return result


def _bind_account_to_warmer(account_id: str, warmer_id: str, seeder_id: str) -> None:
    """Bind a newly created account to a warmer device."""
    try:
        sync_execute(
            "SELECT bind_account_to_device(%s, %s, 'seeder_assignment')",
            (account_id, warmer_id),
        )
        # Also set warmer_device_id for easy lookup
        sync_execute(
            "UPDATE accounts SET warmer_device_id = %s WHERE id = %s",
            (warmer_id, account_id),
        )
        logger.info(
            "Bound account %s to warmer %s (seeded by %s)",
            account_id[:8], warmer_id[:8], seeder_id[:8],
        )
        events.emit("scheduler", "info", "account_bound_to_warmer",
                    f"Account {account_id[:8]} bound to warmer {warmer_id[:8]}",
                    device_id=seeder_id,
                    account_id=account_id,
                    context={"warmer_device_id": warmer_id})
    except Exception:
        logger.error("Failed to bind account %s to warmer", account_id[:8], exc_info=True)


def _reset_device(wda: WDASession) -> None:
    """Return device to clean state after seeder task."""
    wda.reset_to_home()
