"""Identity isolation guardrails — prevent platform fingerprint linkage.

Every session must pass run_pre_session_checks() before executing.
Checks enforce device-account affinity, cooldowns, daily caps, and proxy assignment.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sovi import events
from sovi.config import settings
from sovi.db import sync_execute, sync_execute_one

logger = logging.getLogger(__name__)


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    wait_seconds: float = 0  # how long to wait before retrying


@dataclass
class PreSessionReport:
    device_id: str
    account_id: str | None
    passed: bool
    checks: list[CheckResult] = field(default_factory=list)
    wait_seconds: float = 0  # max wait across failed checks

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "device_id": self.device_id,
            "account_id": self.account_id,
            "wait_seconds": self.wait_seconds,
            "checks": {c.name: {"passed": c.passed, "detail": c.detail} for c in self.checks},
        }


def run_pre_session_checks(
    device_id: str,
    account_id: str | None = None,
) -> PreSessionReport:
    """Run all pre-session identity checks. All must pass.

    Order:
    1. no_concurrent_session — no open session_log entry on this device
    2. proxy_assigned — device has healthy proxy
    3. daily_cap — < max sessions today
    4. cooldown — randomized inter-session gap
    5. device_affinity — account bound to this device (auto-binds on first use)
    """
    if not settings.identity_guard_enabled:
        return PreSessionReport(
            device_id=device_id,
            account_id=account_id,
            passed=True,
            checks=[CheckResult("kill_switch", True, "identity guard disabled")],
        )

    report = PreSessionReport(device_id=device_id, account_id=account_id, passed=True)

    checkers = [
        ("no_concurrent_session", lambda: check_no_concurrent_session(device_id)),
        ("proxy_assigned", lambda: check_proxy_assigned(device_id)),
        ("daily_cap", lambda: check_daily_cap(device_id)),
        ("cooldown", lambda: check_cooldown(device_id)),
    ]
    if account_id:
        checkers.append(
            ("device_affinity", lambda: validate_device_account_affinity(device_id, account_id)),
        )

    for name, checker in checkers:
        result = checker()
        report.checks.append(result)
        if not result.passed:
            report.passed = False
            report.wait_seconds = max(report.wait_seconds, result.wait_seconds)

    events.emit(
        "identity_guard", "info" if report.passed else "warning",
        "pre_session_check",
        f"Pre-session {'PASSED' if report.passed else 'REJECTED'} for device {device_id[:8]}",
        device_id=device_id, account_id=account_id,
        context=report.to_dict(),
    )

    return report


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_no_concurrent_session(device_id: str) -> CheckResult:
    """Ensure no open session_log entry exists on this device.

    Sessions open for more than 2 hours are considered stale (crash recovery)
    and auto-closed as 'aborted'.
    """
    row = sync_execute_one(
        """SELECT id, started_at FROM session_log
           WHERE device_id = %s AND ended_at IS NULL
           LIMIT 1""",
        (device_id,),
    )
    if row:
        started: datetime = row["started_at"]
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - started).total_seconds() / 3600

        if age_hours > 2:
            # Stale session — auto-close as aborted
            sync_execute(
                "UPDATE session_log SET ended_at = now(), outcome = 'aborted' WHERE id = %s",
                (str(row["id"]),),
            )
            logger.warning("Auto-closed stale session %s (%.1fh old)", row["id"], age_hours)
            return CheckResult("no_concurrent_session", True,
                               f"Auto-closed stale session {row['id']}")

        return CheckResult("no_concurrent_session", False,
                           f"Open session {row['id']} still running", wait_seconds=30)
    return CheckResult("no_concurrent_session", True)


def check_proxy_assigned(device_id: str) -> CheckResult:
    """Verify device has a healthy proxy assigned."""
    row = sync_execute_one(
        """SELECT p.id, p.is_healthy, p.host, p.port
           FROM proxies p
           WHERE p.assigned_device_id = %s""",
        (device_id,),
    )
    if not row:
        return CheckResult("proxy_assigned", True, "No proxy — using cellular connection")
    if not row["is_healthy"]:
        return CheckResult("proxy_assigned", False,
                           f"Proxy {row['host']}:{row['port']} marked unhealthy")
    return CheckResult("proxy_assigned", True,
                       f"Proxy {row['host']}:{row['port']} healthy")


def check_daily_cap(device_id: str) -> CheckResult:
    """Ensure device hasn't exceeded daily session cap."""
    row = sync_execute_one(
        """SELECT COUNT(*) as cnt FROM session_log
           WHERE device_id = %s
             AND started_at >= CURRENT_DATE""",
        (device_id,),
    )
    count = row["cnt"] if row else 0
    cap = settings.max_sessions_per_device_day

    if count >= cap:
        # Wait until midnight + jitter
        return CheckResult("daily_cap", False,
                           f"{count}/{cap} sessions today — cap reached",
                           wait_seconds=3600)
    return CheckResult("daily_cap", True, f"{count}/{cap} sessions today")


def check_cooldown(device_id: str) -> CheckResult:
    """Enforce randomized inter-session cooldown."""
    row = sync_execute_one(
        """SELECT last_session_ended_at FROM devices WHERE id = %s""",
        (device_id,),
    )
    if not row or not row["last_session_ended_at"]:
        return CheckResult("cooldown", True, "No previous session")

    last_ended: datetime = row["last_session_ended_at"]
    if last_ended.tzinfo is None:
        last_ended = last_ended.replace(tzinfo=timezone.utc)

    elapsed = (datetime.now(timezone.utc) - last_ended).total_seconds()
    # Seed RNG with device_id + session end time so the cooldown is
    # deterministic across repeated checks for the same session boundary.
    rng = random.Random(f"{device_id}:{last_ended.isoformat()}")
    required = rng.uniform(settings.min_cooldown_seconds, settings.max_cooldown_seconds)
    remaining = required - elapsed

    if remaining > 0:
        return CheckResult("cooldown", False,
                           f"Cooldown: {remaining:.0f}s remaining (need {required:.0f}s)",
                           wait_seconds=remaining)
    return CheckResult("cooldown", True,
                       f"Cooldown satisfied ({elapsed:.0f}s elapsed)")


def validate_device_account_affinity(device_id: str, account_id: str) -> CheckResult:
    """Check account is bound to this device. Auto-binds on first use."""
    row = sync_execute_one(
        """SELECT device_id FROM device_account_bindings
           WHERE account_id = %s AND unbound_at IS NULL""",
        (account_id,),
    )
    if row:
        bound_device = str(row["device_id"])
        if bound_device == device_id:
            return CheckResult("device_affinity", True, "Account bound to this device")
        return CheckResult("device_affinity", False,
                           f"Account bound to different device {bound_device[:8]}")

    # No binding exists — auto-bind
    try:
        sync_execute(
            "SELECT bind_account_to_device(%s, %s, 'initial')",
            (account_id, device_id),
        )
        logger.info("Auto-bound account %s to device %s", account_id[:8], device_id[:8])
        events.emit("identity_guard", "info", "account_auto_bound",
                     f"Account {account_id[:8]} auto-bound to device {device_id[:8]}",
                     device_id=device_id, account_id=account_id)
        return CheckResult("device_affinity", True, "Auto-bound to this device")
    except Exception:
        logger.error("Failed to auto-bind account %s", account_id[:8], exc_info=True)
        return CheckResult("device_affinity", False, "Auto-bind failed")


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


def start_session(
    device_id: str,
    account_id: str | None,
    session_type: str,
    proxy_id: str | None = None,
    identity_checks: dict[str, Any] | None = None,
) -> str | None:
    """Open a session_log entry. Returns session_log.id."""
    try:
        row = sync_execute_one(
            """INSERT INTO session_log
               (device_id, account_id, session_type, proxy_id, identity_checks)
               VALUES (%s, %s, %s, %s, %s)
               RETURNING id""",
            (
                device_id,
                account_id,
                session_type,
                proxy_id,
                json.dumps(identity_checks or {}),
            ),
        )
        session_id = str(row["id"]) if row else None
        logger.info("Session started: %s (device=%s, type=%s)",
                     session_id, device_id[:8], session_type)
        return session_id
    except Exception:
        logger.error("Failed to start session", exc_info=True)
        return None


def end_session(session_id: str, outcome: str) -> None:
    """Close a session_log entry and update device.last_session_ended_at."""
    try:
        row = sync_execute_one(
            """UPDATE session_log
               SET ended_at = now(), outcome = %s
               WHERE id = %s
               RETURNING device_id""",
            (outcome, session_id),
        )
        if row:
            sync_execute(
                "UPDATE devices SET last_session_ended_at = now() WHERE id = %s",
                (str(row["device_id"]),),
            )
        logger.info("Session ended: %s outcome=%s", session_id, outcome)
    except Exception:
        logger.error("Failed to end session %s", session_id, exc_info=True)
