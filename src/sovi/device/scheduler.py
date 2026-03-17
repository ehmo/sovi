"""Continuous device scheduler — 95% utilization, 24/7 operation.

Architecture: one thread per device, each running an infinite loop:
  1. get_next_task() → warm existing account OR create persona resources OR create new account
  2. Execute task (delete → install → login → warm 30 min → log)
  3. Emit events to system_events table
  4. Repeat

Task priority:
  1. Warm existing account (not yet warmed today, earlier phases first)
  2. Create platform accounts for personas with email but missing accounts
  3. Create emails for personas without email
  4. Create new account on platform/niche with fewest accounts (legacy fallback)
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from sovi import events
from sovi.config import settings
from sovi.db import sync_conn, sync_execute, sync_execute_one
from sovi.device._clean_room import enforce as enforce_clean_room
from sovi.device.app_lifecycle import delete_app, install_from_app_store, login_account, reset_idfa
from sovi.device.device_registry import (
    set_device_status,
    to_wda_device,
    update_heartbeat,
)
from sovi.device.identity_guard import (
    end_session,
    run_pre_session_checks,
    start_session,
)
from sovi.device.service_diagnostics import diagnose_wda_failure
from sovi.device.roles import (
    SEEDER,
    WARMER,
    dedupe_open_seeder_tasks,
    RoleRotator,
    get_current_role,
    is_in_cooldown,
    populate_seeder_tasks,
    recover_interrupted_seeder_tasks,
)
from sovi.device.seeder import run_seeder_cycle
from sovi.device.warming import WarmingConfig, WarmingPhase, run_warming
from sovi.device.wda_client import WDADevice, WDASession
from sovi.models import AccountState

logger = logging.getLogger(__name__)

# Session timing constants
WARMING_DURATION_MIN = 30
OVERHEAD_MIN = 15  # delete + install + login + cooldown

# Platforms we warm (TikTok + Instagram only to start)
WARMABLE_PLATFORMS = ("tiktok", "instagram")


@dataclass
class DeviceThread:
    """State for a device's scheduler thread."""
    device_id: str
    device_name: str
    thread: threading.Thread | None = None
    current_task: str = "idle"
    current_account: str | None = None
    sessions_today: int = 0
    last_session_at: datetime | None = None
    running: bool = False
    error: str | None = None
    network_guard_state: str = "unknown"
    network_guard_last_checked_at: datetime | None = None
    network_guard_last_recovered_at: datetime | None = None
    network_guard_last_error: str | None = None
    network_guard_consecutive_failures: int = 0
    network_guard_last_cellular_reset_at: datetime | None = None


class DeviceScheduler:
    """Continuous scheduler managing all devices."""

    def __init__(self) -> None:
        self._threads: dict[str, DeviceThread] = {}
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._rotator = RoleRotator()

    @staticmethod
    def _get_schedulable_devices() -> list[dict[str, Any]]:
        """Return devices the scheduler should keep trying to manage across restarts."""
        return sync_execute(
            """SELECT id, name, model, udid, ios_version,
                      wda_port, status, "current_role",
                      connected_since,
                      role_changed_at, seeder_cooldown_until
               FROM devices
               WHERE status IN ('active', 'disconnected', 'maintenance')
               ORDER BY name"""
        )

    def start(self) -> None:
        """Start scheduler threads for all active devices."""
        # Block quarantined modules from being imported in device context
        enforce_clean_room()

        self._stop_event.clear()
        devices = self._get_schedulable_devices()

        if not devices:
            logger.warning("No active devices found")
            events.emit("scheduler", "warning", "no_devices",
                       "Scheduler started but no active devices found")
            return

        # Start role rotator (bootstraps roles on first run, rotates every 4-6h)
        self._rotator.start()

        # Any claimed/in-progress seeder tasks from a prior dashboard process
        # are orphaned once this scheduler starts. Requeue them before claiming.
        try:
            recover_interrupted_seeder_tasks()
        except Exception:
            logger.error("Failed to recover interrupted seeder tasks", exc_info=True)

        try:
            dedupe_open_seeder_tasks()
        except Exception:
            logger.error("Failed to dedupe open seeder tasks", exc_info=True)

        # Populate seeder task queue from pending personas
        try:
            populate_seeder_tasks()
        except Exception:
            logger.error("Failed to populate seeder tasks at startup", exc_info=True)

        events.emit("scheduler", "info", "scheduler_started",
                    f"Starting scheduler with {len(devices)} devices",
                    context={"device_count": len(devices)})

        for device_row in devices:
            device_id = str(device_row["id"])
            name = device_row["name"] or device_id[:8]

            dt = DeviceThread(device_id=device_id, device_name=name, running=True)
            t = threading.Thread(
                target=self._device_loop,
                args=(device_row, dt),
                name=f"scheduler-{name}",
                daemon=True,
            )
            dt.thread = t
            self._threads[device_id] = dt
            t.start()
            logger.info("Started scheduler thread for %s", name)

    def stop(self) -> None:
        """Gracefully stop all scheduler threads."""
        logger.info("Stopping scheduler...")
        self._stop_event.set()
        self._rotator.stop()

        events.emit("scheduler", "info", "scheduler_stopping",
                    "Scheduler stop requested")

        for dt in self._threads.values():
            dt.running = False
            if dt.thread and dt.thread.is_alive():
                dt.thread.join(timeout=30)

        self._threads.clear()
        events.emit("scheduler", "info", "scheduler_stopped",
                    "Scheduler stopped")

    @property
    def is_running(self) -> bool:
        return not self._stop_event.is_set() and bool(self._threads)

    def status(self) -> dict[str, Any]:
        """Get scheduler status for dashboard/API."""
        thread_status = {}
        for device_id, dt in self._threads.items():
            thread_status[device_id] = {
                "device_name": dt.device_name,
                "current_task": dt.current_task,
                "current_account": dt.current_account,
                "sessions_today": dt.sessions_today,
                "last_session_at": dt.last_session_at.isoformat() if dt.last_session_at else None,
                "running": dt.running,
                "alive": dt.thread.is_alive() if dt.thread else False,
                "error": dt.error,
                "network_guard": {
                    "state": dt.network_guard_state,
                    "last_checked_at": (
                        dt.network_guard_last_checked_at.isoformat()
                        if dt.network_guard_last_checked_at else None
                    ),
                    "last_recovered_at": (
                        dt.network_guard_last_recovered_at.isoformat()
                        if dt.network_guard_last_recovered_at else None
                    ),
                    "last_error": dt.network_guard_last_error,
                    "consecutive_failures": dt.network_guard_consecutive_failures,
                    "last_cellular_reset_at": (
                        dt.network_guard_last_cellular_reset_at.isoformat()
                        if dt.network_guard_last_cellular_reset_at else None
                    ),
                },
            }

        return {
            "running": self.is_running,
            "device_count": len(self._threads),
            "threads": thread_status,
            "sessions_per_day_target": settings.max_sessions_per_device_day,
        }

    @staticmethod
    def _set_network_guard_state(dt: DeviceThread, state: str) -> None:
        dt.network_guard_state = state
        dt.network_guard_last_checked_at = datetime.now(timezone.utc)

    def _record_network_guard_failure(
        self,
        dt: DeviceThread,
        *,
        device: WDADevice,
        error: str,
        state: str = "unhealthy",
    ) -> None:
        previous_state = dt.network_guard_state
        previous_error = dt.network_guard_last_error
        self._set_network_guard_state(dt, state)
        dt.network_guard_consecutive_failures += 1
        dt.network_guard_last_error = error

        if (
            dt.network_guard_consecutive_failures == 1
            or previous_state != state
            or previous_error != error
        ):
            events.emit(
                "device",
                "error",
                "network_guard_unhealthy",
                f"Network guard unhealthy on {device.name}: {error}",
                device_id=dt.device_id,
                context={
                    "device_name": device.name,
                    "state": state,
                    "error": error,
                    "consecutive_failures": dt.network_guard_consecutive_failures,
                },
            )

    def _record_network_guard_healthy(
        self,
        dt: DeviceThread,
        *,
        device: WDADevice,
        state: str = "healthy",
    ) -> None:
        had_failures = dt.network_guard_consecutive_failures > 0
        previous_state = dt.network_guard_state
        self._set_network_guard_state(dt, state)
        dt.network_guard_last_error = None
        dt.network_guard_consecutive_failures = 0
        if had_failures or dt.network_guard_last_recovered_at is None or previous_state != state:
            dt.network_guard_last_recovered_at = dt.network_guard_last_checked_at
        if dt.error == "network_guard_unhealthy":
            dt.error = None
        if had_failures:
            events.emit(
                "device",
                "info",
                "network_guard_recovered",
                f"Network guard recovered on {device.name}",
                device_id=dt.device_id,
                context={
                    "device_name": device.name,
                    "state": state,
                    "last_cellular_reset_at": (
                        dt.network_guard_last_cellular_reset_at.isoformat()
                        if dt.network_guard_last_cellular_reset_at else None
                    ),
                },
            )

    @staticmethod
    def _can_reset_cellular(dt: DeviceThread) -> bool:
        last_reset = dt.network_guard_last_cellular_reset_at
        if last_reset is None:
            return True
        return (
            datetime.now(timezone.utc) - last_reset
        ).total_seconds() >= settings.device_network_reset_cooldown_seconds

    def _run_network_guard_check(
        self,
        device: WDADevice,
        dt: DeviceThread,
        *,
        allow_cellular_reset: bool,
    ) -> bool:
        """Continuously enforce carrier-ready cellular-only networking outside task execution."""
        if not settings.device_network_monitor_enabled:
            self._record_network_guard_healthy(dt, device=device, state="disabled")
            return True

        self._set_network_guard_state(dt, "checking")
        session = WDASession(device)
        try:
            session.connect()
        except Exception:
            self._record_network_guard_failure(
                dt,
                device=device,
                state="session_unavailable",
                error="Could not create WDA session for network guard",
            )
            dt.error = "network_guard_unhealthy"
            return False

        try:
            if session.ensure_cellular_ready(
                probe_attempts=settings.device_network_probe_attempts,
                probe_wait_s=settings.device_network_probe_wait_seconds,
                cleanup=True,
            ):
                self._record_network_guard_healthy(dt, device=device)
                return True

            if allow_cellular_reset and self._can_reset_cellular(dt):
                self._set_network_guard_state(dt, "resetting_cellular")
                if session.reset_cellular_data_connection(
                    wait_off_seconds=settings.device_network_reset_disabled_seconds,
                    recovery_wait_s=settings.device_network_reset_settle_seconds,
                    probe_attempts=settings.device_network_probe_attempts,
                    probe_wait_s=settings.device_network_probe_wait_seconds,
                ):
                    dt.network_guard_last_cellular_reset_at = datetime.now(timezone.utc)
                    self._record_network_guard_healthy(dt, device=device)
                    return True
                self._record_network_guard_failure(
                    dt,
                    device=device,
                    state="reset_failed",
                    error="Cellular reset failed to restore carrier-ready cellular-only state",
                )
                dt.error = "network_guard_unhealthy"
                return False

            failure_state = "awaiting_reset_window" if allow_cellular_reset else "unhealthy"
            failure_error = (
                "Carrier-ready cellular verification failed; reset window is cooling down"
                if failure_state == "awaiting_reset_window"
                else "Carrier-ready cellular verification failed"
            )
            self._record_network_guard_failure(
                dt,
                device=device,
                state=failure_state,
                error=failure_error,
            )
            dt.error = "network_guard_unhealthy"
            return False
        except Exception:
            logger.error("Network guard check failed for %s", device.name, exc_info=True)
            self._record_network_guard_failure(
                dt,
                device=device,
                state="monitor_error",
                error="Network guard check raised an exception",
            )
            dt.error = "network_guard_unhealthy"
            return False
        finally:
            session.disconnect()

    def _wait_with_network_guard(
        self,
        device: WDADevice,
        dt: DeviceThread,
        seconds: float,
        *,
        task_label: str,
    ) -> None:
        """Chunk sleeps so idle/cooldown periods continue guarding network health."""
        if seconds <= 0:
            return
        if not settings.device_network_monitor_enabled:
            dt.current_task = task_label
            self._stop_event.wait(seconds)
            return

        deadline = time.monotonic() + seconds
        while not self._stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return

            dt.current_task = task_label
            if not self._run_network_guard_check(device, dt, allow_cellular_reset=True):
                pause = min(settings.device_network_monitor_unhealthy_wait_seconds, remaining)
                if pause > 0:
                    self._stop_event.wait(pause)
                continue

            self._stop_event.wait(min(settings.device_network_monitor_interval_seconds, remaining))

    def _enforce_cellular_only(
        self,
        session: WDASession,
        dt: DeviceThread,
        *,
        device_name: str,
        platform: str | None = None,
        account_id: str | None = None,
    ) -> bool:
        """Ensure the device is using carrier-ready cellular-only networking before continuing."""
        dt.current_task = f"enforcing_cellular_only:{device_name}"
        if session.ensure_cellular_ready(
            probe_attempts=settings.device_network_probe_attempts,
            probe_wait_s=settings.device_network_probe_wait_seconds,
            cleanup=True,
        ):
            self._record_network_guard_healthy(dt, device=session.device)
            return True

        self._record_network_guard_failure(
            dt,
            device=session.device,
            error="Carrier-ready cellular verification failed at task gate",
        )

        context = {"device_name": device_name}
        if platform:
            context["platform"] = platform

        emit_kwargs: dict[str, Any] = {
            "device_id": dt.device_id,
            "context": context,
        }
        if account_id:
            emit_kwargs["account_id"] = account_id

        events.emit(
            "scheduler",
            "error",
            "cellular_enforcement_failed",
            f"Could not verify carrier-ready cellular-only state on {device_name}",
            **emit_kwargs,
        )
        return False

    def _handle_wda_unreachable(self, device: WDADevice, dt: DeviceThread) -> None:
        """Record the best available diagnostic when WDA cannot be reached."""
        diagnostic = diagnose_wda_failure(device.name)
        if diagnostic:
            dt.current_task = diagnostic.reason
            self._record_network_guard_failure(
                dt,
                device=device,
                state="wda_unreachable",
                error=diagnostic.summary,
            )
            dt.error = diagnostic.reason
            event_type = "device_setup_required" if diagnostic.manual_action_required else "device_disconnected"
            severity = "error" if diagnostic.manual_action_required else "critical"
            events.emit(
                "device",
                severity,
                event_type,
                diagnostic.summary,
                device_id=dt.device_id,
                context={
                    "device_name": device.name,
                    "wda_port": device.wda_port,
                    "reason": diagnostic.reason,
                    "hint": diagnostic.hint,
                    "launchd_label": diagnostic.launchd_label,
                    "log_path": diagnostic.log_path,
                    "retry_after_seconds": diagnostic.retry_after_seconds,
                },
            )
            logger.warning("%s | %s", diagnostic.summary, diagnostic.hint)
            set_device_status(dt.device_id, "disconnected")
            self._stop_event.wait(diagnostic.retry_after_seconds)
            return

        dt.current_task = "wda_unreachable"
        self._record_network_guard_failure(
            dt,
            device=device,
            state="wda_unreachable",
            error="WDA not responding",
        )
        dt.error = "WDA not responding"
        events.emit("device", "critical", "device_disconnected",
                   f"WDA not responding on {device.name}",
                   device_id=dt.device_id,
                   context={"device_name": device.name, "wda_port": device.wda_port})
        set_device_status(dt.device_id, "disconnected")
        self._stop_event.wait(60)

    def _device_loop(self, device_row: dict[str, Any], dt: DeviceThread) -> None:
        """Main loop for a single device thread.

        Dispatches to seeder or warmer behaviour based on the device's
        current role (checked every iteration so role rotations take
        effect without restarting threads).
        """
        device = to_wda_device(device_row)
        device_id = dt.device_id

        logger.info("Device loop started: %s (port %d)", device.name, device.wda_port)

        while not self._stop_event.is_set() and dt.running:
            try:
                # Wait for WDA to be responsive
                dt.current_task = "waiting_for_wda"
                wda_state = self._wait_for_wda(device)
                if wda_state is None:
                    dt.current_task = "wda_busy"
                    self._set_network_guard_state(dt, "wda_busy")
                    logger.info("WDA busy on %s, retrying in 30s", device.name)
                    self._stop_event.wait(30)
                    continue
                if not wda_state:
                    self._handle_wda_unreachable(device, dt)
                    continue

                if not self._run_network_guard_check(device, dt, allow_cellular_reset=True):
                    dt.current_task = "paused:network_guard"
                    self._stop_event.wait(settings.device_network_monitor_unhealthy_wait_seconds)
                    continue

                # Only heartbeat devices after network guard proves they are healthy.
                update_heartbeat(device_id)
                dt.error = None

                # Check current role (may change between iterations via RoleRotator)
                role = get_current_role(device_id)

                if role == SEEDER:
                    self._run_seeder_iteration(device, dt)
                elif role == WARMER:
                    # Respect post-seeder cooldown
                    if is_in_cooldown(device_id):
                        dt.current_task = "seeder_cooldown"
                        logger.debug("%s in post-seeder cooldown, sleeping 60s", device.name)
                        self._wait_with_network_guard(device, dt, 60, task_label="seeder_cooldown")
                        continue
                    self._run_warmer_iteration(device, dt)
                else:
                    # Idle / unassigned — wait for role bootstrap
                    self._wait_with_network_guard(device, dt, 30, task_label="idle:no_role")

            except Exception:
                dt.error = "Unhandled exception in device loop"
                logger.error("Error in device loop for %s", device.name, exc_info=True)
                events.emit("scheduler", "error", "device_loop_error",
                           f"Unhandled error in {device.name} loop",
                           device_id=device_id,
                           context={"device_name": device.name})
                self._stop_event.wait(60)

        dt.running = False
        dt.current_task = "stopped"
        logger.info("Device loop ended: %s", device.name)

    # ------------------------------------------------------------------
    # Seeder iteration
    # ------------------------------------------------------------------

    def _run_seeder_iteration(self, device: WDADevice, dt: DeviceThread) -> None:
        """Execute one seeder cycle: claim task → create email/account on-device."""
        dt.current_task = "seeder:claiming"
        session = WDASession(device)
        try:
            session.connect()

            # CRITICAL: all persona-facing traffic must be cellular-only.
            if not self._enforce_cellular_only(session, dt, device_name=device.name):
                dt.error = "cellular_enforcement_failed"
                self._wait_with_network_guard(
                    device,
                    dt,
                    60,
                    task_label="paused:network_guard",
                )
                return

            dt.current_task = "seeder:executing"
            result = run_seeder_cycle(
                session,
                dt.device_id,
                device.name,
                stop_event=self._stop_event,
            )
            if result:
                dt.sessions_today += 1
                dt.last_session_at = datetime.now(timezone.utc)
            else:
                # No tasks available — short sleep before checking again
                self._wait_with_network_guard(device, dt, 30, task_label="seeder:idle")
        except Exception:
            logger.error("Seeder iteration failed for %s", device.name, exc_info=True)
            dt.error = "seeder_error"
            self._stop_event.wait(60)
        finally:
            session.disconnect()

    # ------------------------------------------------------------------
    # Warmer iteration
    # ------------------------------------------------------------------

    def _run_warmer_iteration(self, device: WDADevice, dt: DeviceThread) -> None:
        """Execute one warming iteration for a warmer device."""
        device_id = dt.device_id

        # The background network guard keeps the device cellular-only between
        # tasks; task-specific execution still re-validates before any traffic.

        # Get next warming task (device-affinity aware)
        dt.current_task = "selecting_task"
        task = self._get_next_task(device_id)

        if task is None:
            self._wait_with_network_guard(device, dt, 30, task_label="idle")
            return

        # Pre-session identity checks (skip proxy — cellular connection)
        account_id = None
        if task["type"] == "warm":
            account_id = str(task["account"]["id"])

        dt.current_task = "identity_checks"
        report = run_pre_session_checks(device_id, account_id)

        if not report.passed:
            wait = max(report.wait_seconds, 30)
            logger.info("Pre-session rejected for %s, waiting %.0fs", device.name, wait)
            self._wait_with_network_guard(device, dt, wait, task_label=f"cooldown:{wait:.0f}s")
            return

        # Session log (no proxy — all traffic is cellular)
        session_type = "warming" if task["type"] == "warm" else "creation"
        session_id = start_session(
            device_id, account_id, session_type,
            identity_checks=report.to_dict(),
        )

        # Execute task
        outcome = "failed"
        if task["type"] == "warm":
            outcome = "success" if self._execute_warming(device, dt, task) else "failed"
        elif task["type"] == "create":
            skipped = self._execute_creation(device, dt, task)
            if skipped is None:
                # Stub/no-op — don't count toward daily cap
                if session_id:
                    end_session(session_id, "skipped")
                return
            outcome = "success" if skipped else "failed"
        elif task["type"] == "create_persona_account":
            outcome = "success" if self._execute_persona_account_creation(device, dt, task) else "failed"
        elif task["type"] == "create_email":
            outcome = "success" if self._execute_email_creation(device, dt, task) else "failed"

        # End session log
        if session_id:
            end_session(session_id, outcome)

        dt.sessions_today += 1
        dt.last_session_at = datetime.now(timezone.utc)

        # Randomized cooldown: uniform(5, 15) min + jitter(±2 min)
        dt.current_task = "cooldown"
        cooldown = random.uniform(
            settings.min_cooldown_seconds,
            settings.max_cooldown_seconds,
        ) + random.uniform(-120, 120)
        cooldown = max(cooldown, 60)  # floor at 1 min
        self._wait_with_network_guard(device, dt, cooldown, task_label="cooldown")

    def _get_next_task(self, device_id: str) -> dict[str, Any] | None:
        """Determine the next task for a device.

        Priority:
        1. Warm existing account bound to this device (not yet warmed today)
        2. Create new account on platform/niche with fewest accounts
        """
        # Try to claim an account that needs warming — only accounts bound to this device
        try:
            with sync_conn() as conn:
                with conn.cursor() as cur:
                    # Use FOR UPDATE SKIP LOCKED to avoid conflicts between device threads
                    # JOIN device_account_bindings to enforce device affinity
                    cur.execute(
                        """SELECT a.id, a.platform, a.username, a.current_state,
                                  a.warming_day_count, a.email_enc, a.password_enc,
                                  a.totp_secret_enc, a.niche_id, n.slug as niche_slug
                           FROM accounts a
                           JOIN niches n ON a.niche_id = n.id
                           JOIN device_account_bindings dab
                                ON a.id = dab.account_id
                                AND dab.device_id = %s
                                AND dab.unbound_at IS NULL
                           WHERE a.current_state = ANY(%s)
                             AND a.platform = ANY(%s)
                             AND a.deleted_at IS NULL
                             AND (a.last_warmed_at IS NULL
                                  OR a.last_warmed_at < CURRENT_DATE)
                           ORDER BY
                             CASE a.current_state
                               WHEN %s THEN 0
                               WHEN %s THEN 1
                               WHEN %s THEN 2
                               WHEN %s THEN 3
                               WHEN %s THEN 4
                             END,
                             a.last_warmed_at ASC NULLS FIRST
                           LIMIT 1
                           FOR UPDATE OF a SKIP LOCKED""",
                        (
                            device_id,
                            [AccountState.CREATED, AccountState.WARMING_P1, AccountState.WARMING_P2,
                             AccountState.WARMING_P3, AccountState.ACTIVE],
                            list(WARMABLE_PLATFORMS),
                            AccountState.CREATED, AccountState.WARMING_P1,
                            AccountState.WARMING_P2, AccountState.WARMING_P3,
                            AccountState.ACTIVE,
                        ),
                    )
                    row = cur.fetchone()
                    conn.commit()

                    if row:
                        return {
                            "type": "warm",
                            "account": dict(row),
                        }
        except Exception:
            logger.error("Error getting next warming task", exc_info=True)

        # Priority 2: Create platform accounts for personas that have email but missing accounts
        try:
            persona_task = sync_execute_one(
                """SELECT p.id as persona_id, p.first_name, p.last_name, p.display_name,
                          p.username_base, p.gender, p.date_of_birth, p.age,
                          p.niche_id, p.bio_short, p.occupation, p.interests,
                          plt.platform
                   FROM personas p
                   CROSS JOIN (VALUES ('tiktok'), ('instagram'), ('reddit'),
                                      ('youtube_shorts'), ('facebook'), ('linkedin')) plt(platform)
                   LEFT JOIN accounts a ON a.persona_id = p.id
                        AND a.platform = plt.platform::platform_type
                        AND a.deleted_at IS NULL
                   JOIN email_accounts ea ON ea.persona_id = p.id AND ea.status IN ('available', 'assigned')
                   WHERE p.status = 'ready' AND a.id IS NULL
                   ORDER BY
                       CASE plt.platform
                           WHEN 'tiktok' THEN 0 WHEN 'instagram' THEN 1
                           WHEN 'reddit' THEN 2 WHEN 'youtube_shorts' THEN 3
                           WHEN 'facebook' THEN 4 WHEN 'linkedin' THEN 5
                       END,
                       p.created_at ASC
                   LIMIT 1""",
            )
            if persona_task:
                return {
                    "type": "create_persona_account",
                    "persona": dict(persona_task),
                    "platform": persona_task["platform"],
                }
        except Exception:
            logger.error("Error getting persona account creation task", exc_info=True)

        # Priority 3: Create email for persona without one
        try:
            persona_needing_email = sync_execute_one(
                """SELECT p.id, p.first_name, p.last_name, p.display_name,
                          p.username_base, p.gender, p.date_of_birth, p.age,
                          p.niche_id, p.bio_short, p.occupation
                   FROM personas p
                   LEFT JOIN email_accounts ea ON ea.persona_id = p.id
                   WHERE p.status = 'ready' AND ea.id IS NULL
                   ORDER BY p.created_at ASC
                   LIMIT 1""",
            )
            if persona_needing_email:
                return {
                    "type": "create_email",
                    "persona": dict(persona_needing_email),
                }
        except Exception:
            logger.error("Error getting email creation task", exc_info=True)

        # Priority 4 (legacy fallback): Create a new account
        try:
            with sync_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """SELECT platform, COUNT(*) as cnt
                           FROM accounts
                           WHERE platform = ANY(%s) AND deleted_at IS NULL
                           GROUP BY platform""",
                        (list(WARMABLE_PLATFORMS),),
                    )
                    counts = {row["platform"]: row["cnt"] for row in cur.fetchall()}
                    conn.commit()

            tt_count = counts.get("tiktok", 0)
            ig_count = counts.get("instagram", 0)
            platform = "tiktok" if tt_count <= ig_count else "instagram"

            return {
                "type": "create",
                "platform": platform,
            }
        except Exception:
            logger.error("Error determining creation task", exc_info=True)
            return None

    def _execute_warming(
        self,
        device: WDADevice,
        dt: DeviceThread,
        task: dict[str, Any],
    ) -> bool:
        """Execute a warming session. Returns True on success."""
        account = task["account"]
        platform = account["platform"]
        username = account["username"]
        account_id = str(account["id"])
        device_id = dt.device_id
        success = False

        dt.current_task = f"warming:{platform}/{username}"
        dt.current_account = username

        # Determine warming phase from account state
        state = account["current_state"]
        phase_map = {
            AccountState.CREATED: WarmingPhase.PASSIVE,
            AccountState.WARMING_P1: WarmingPhase.PASSIVE,
            AccountState.WARMING_P2: WarmingPhase.LIGHT,
            AccountState.WARMING_P3: WarmingPhase.MODERATE,
            AccountState.ACTIVE: WarmingPhase.LIGHT,
        }
        phase = phase_map.get(state, WarmingPhase.PASSIVE)

        events.emit("scheduler", "info", "warming_started",
                    f"Warming {platform}/{username} (phase={phase.name})",
                    device_id=device_id, account_id=account_id,
                    context={
                        "platform": platform, "account_id": account_id,
                        "phase": phase.name, "duration_min": WARMING_DURATION_MIN,
                    })

        session = WDASession(device)
        try:
            session.connect()

            # Step 0a: Ensure the phone is on cellular with Wi-Fi disabled.
            if not self._enforce_cellular_only(
                session,
                dt,
                device_name=device.name,
                platform=platform,
                account_id=account_id,
            ):
                return False

            # NOTE: airplane mode toggle removed from warming path — too risky,
            # a failed toggle bricks the phone in airplane mode until manual reset.
            # IP rotation only needed for account creation (seeder path).

            # Step 1: Delete app for IDFV isolation
            dt.current_task = f"deleting:{platform}"
            if not delete_app(session, platform, device_id=device_id):
                events.emit("scheduler", "error", "app_delete_failed",
                           f"Failed to delete {platform} before warming",
                           device_id=device_id, account_id=account_id,
                           context={"platform": platform, "step": "delete"})
                return False
            time.sleep(2)

            # Step 1.5: Reset IDFA between sessions
            dt.current_task = f"resetting_idfa:{device.name}"
            if not reset_idfa(session, device_id=device_id):
                logger.warning("IDFA reset failed for %s on %s; continuing with fresh install only",
                               platform, device.name)
            else:
                time.sleep(2)

            # Step 2: Install fresh
            dt.current_task = f"installing:{platform}"
            if not install_from_app_store(session, platform, device_id=device_id):
                events.emit("scheduler", "error", "install_failed",
                           f"Failed to install {platform} for warming",
                           device_id=device_id, account_id=account_id,
                           context={"platform": platform, "retry_count": 0})
                return False

            # Step 3: Login
            dt.current_task = f"logging_in:{platform}/{username}"
            if not login_account(session, account, device_id=device_id):
                events.emit("scheduler", "error", "login_failed",
                           f"Login failed for {platform}/{username}",
                           device_id=device_id, account_id=account_id,
                           context={
                               "platform": platform,
                               "username": username,
                               "step": "login",
                           })
                return False

            # Step 4: Warm
            dt.current_task = f"warming:{platform}/{username}"
            config = WarmingConfig(
                device_name=device.name,
                platform=platform,
                phase=phase,
                duration_min=WARMING_DURATION_MIN,
            )
            result = run_warming(session, config)

            # Check for error from run_warming
            if isinstance(result, dict) and "error" in result:
                logger.warning(
                    "run_warming returned error for %s/%s: %s",
                    platform, username, result["error"],
                )
                events.emit("scheduler", "error", "warming_error",
                            f"Warming error for {platform}/{username}: {result['error']}",
                            device_id=device_id, account_id=account_id,
                            context={"platform": platform, "error": result["error"]})
                return False

            # Step 5: Update account state
            new_day_count = account["warming_day_count"] + 1
            # Phase transitions based on warming days
            if new_day_count <= 3:
                new_state = AccountState.WARMING_P1
            elif new_day_count <= 7:
                new_state = AccountState.WARMING_P2
            elif new_day_count <= 14:
                new_state = AccountState.WARMING_P3
            else:
                new_state = AccountState.ACTIVE

            sync_execute(
                """UPDATE accounts
                   SET last_warmed_at = now(),
                       warming_day_count = %s,
                       current_state = %s,
                       last_activity_at = now(),
                       updated_at = now()
                   WHERE id = %s""",
                (new_day_count, new_state, account_id),
            )

            events.emit("scheduler", "info", "warming_complete",
                        f"Warmed {platform}/{username}: {result.get('videos_watched', 0)} videos",
                        device_id=device_id, account_id=account_id,
                        context={
                            "platform": platform,
                            "videos_watched": result.get("videos_watched", 0),
                            "likes": result.get("likes", 0),
                            "duration_min": result.get("duration_min", 0),
                            "phase": phase.name,
                            "new_state": new_state,
                            "warming_day": new_day_count,
                        })

            # Distribution handoff: after 14+ warming days, unbind from device
            if new_day_count >= 14 and new_state == AccountState.ACTIVE:
                self._handoff_to_distribution(account_id, device_id, platform, username)

            success = True

        except Exception:
            logger.error("Warming failed for %s/%s on %s",
                        platform, username, device.name, exc_info=True)
            events.emit("scheduler", "error", "warming_failed",
                       f"Warming exception for {platform}/{username}",
                       device_id=device_id, account_id=account_id,
                       context={"platform": platform, "username": username})
        finally:
            self._reset_device(session)
            session.disconnect()
            dt.current_account = None
            time.sleep(2)

        return success

    def _execute_email_creation(
        self,
        device: WDADevice,
        dt: DeviceThread,
        task: dict[str, Any],
    ) -> bool:
        """Execute an email creation task for a persona."""
        persona = task["persona"]
        persona_name = persona.get("display_name", "?")
        device_id = dt.device_id
        success = False
        dt.current_task = f"creating_email:{persona_name}"

        events.emit("scheduler", "info", "email_creation_started",
                    f"Creating email for persona {persona_name} on {device.name}",
                    device_id=device_id,
                    context={"persona_id": str(persona["id"])})

        session = WDASession(device)
        try:
            session.connect()
            if not self._enforce_cellular_only(session, dt, device_name=device.name):
                return False

            from sovi.persona.email_creator import create_email_for_persona
            result = create_email_for_persona(
                session, persona, provider="outlook", device_id=device_id,
            )

            if result:
                events.emit("scheduler", "info", "email_creation_complete",
                            f"Created email for {persona_name}",
                            device_id=device_id,
                            context={"persona_id": str(persona["id"]),
                                     "email_account_id": str(result["id"])})
                success = True
            else:
                events.emit("scheduler", "error", "email_creation_failed",
                            f"Failed to create email for {persona_name}",
                            device_id=device_id,
                            context={"persona_id": str(persona["id"])})
        except Exception:
            logger.error("Email creation failed for %s on %s",
                         persona_name, device.name, exc_info=True)
            events.emit("scheduler", "error", "email_creation_error",
                        f"Email creation exception for {persona_name}",
                        device_id=device_id,
                        context={"persona_id": str(persona["id"])})
        finally:
            self._reset_device(session)
            session.disconnect()
            dt.current_account = None
            time.sleep(2)

        return success

    def _execute_persona_account_creation(
        self,
        device: WDADevice,
        dt: DeviceThread,
        task: dict[str, Any],
    ) -> bool:
        """Execute a platform account creation task for a persona."""
        persona = task["persona"]
        platform = task["platform"]
        persona_name = persona.get("display_name", "?")
        device_id = dt.device_id
        success = False
        dt.current_task = f"creating_account:{platform}/{persona_name}"

        events.emit("scheduler", "info", "persona_account_creation_started",
                    f"Creating {platform} account for {persona_name} on {device.name}",
                    device_id=device_id,
                    context={"persona_id": str(persona["persona_id"]),
                             "platform": platform})

        session = WDASession(device)
        try:
            session.connect()
            if not self._enforce_cellular_only(
                session,
                dt,
                device_name=device.name,
                platform=platform,
            ):
                return False

            from sovi.persona.account_creator import create_account_for_persona
            # Build persona dict with expected keys
            persona_data = {
                "id": persona["persona_id"],
                "niche_id": persona["niche_id"],
                "first_name": persona["first_name"],
                "last_name": persona["last_name"],
                "display_name": persona["display_name"],
                "username_base": persona["username_base"],
                "gender": persona["gender"],
                "date_of_birth": str(persona["date_of_birth"]),
                "age": persona["age"],
            }
            result = create_account_for_persona(
                session, persona_data, platform, device_id=device_id,
            )

            if result:
                events.emit("scheduler", "info", "persona_account_created",
                            f"Created {platform} account for {persona_name}: {result.get('username', '?')}",
                            device_id=device_id,
                            context={"persona_id": str(persona["persona_id"]),
                                     "platform": platform,
                                     "username": result.get("username")})
                success = True
            else:
                events.emit("scheduler", "error", "persona_account_creation_failed",
                            f"Failed to create {platform} account for {persona_name}",
                            device_id=device_id,
                            context={"persona_id": str(persona["persona_id"]),
                                     "platform": platform})
        except Exception:
            logger.error("Persona account creation failed for %s/%s on %s",
                         platform, persona_name, device.name, exc_info=True)
        finally:
            self._reset_device(session)
            session.disconnect()
            dt.current_account = None
            time.sleep(2)

        return success

    def _execute_creation(
        self,
        device: WDADevice,
        dt: DeviceThread,
        task: dict[str, Any],
    ) -> bool | None:
        """Execute an account creation task. Returns True on success, False on failure, None if skipped."""
        platform = task["platform"]
        device_id = dt.device_id
        dt.current_task = f"creating:{platform}"

        events.emit("scheduler", "info", "creation_started",
                    f"Creating new {platform} account on {device.name}",
                    device_id=device_id,
                    context={"platform": platform})

        # TODO: integrate with email provider to generate disposable email
        # For now, log that creation is needed and skip
        # When implemented: after creating account, auto-bind to this device:
        #   from sovi.device.identity_guard import validate_device_account_affinity
        #   validate_device_account_affinity(device_id, new_account_id)
        events.emit("scheduler", "warning", "creation_skipped",
                    f"Account creation for {platform}: use persona pipeline instead",
                    device_id=device_id,
                    context={"platform": platform, "reason": "email_provider_not_configured"})
        return None  # no-op until email provider is integrated

    @staticmethod
    def _handoff_to_distribution(
        account_id: str,
        device_id: str,
        platform: str,
        username: str,
    ) -> None:
        """Unbind a fully warmed account from its device for distribution.

        After 14+ warming days the account is mature enough for content
        posting. Unbinding frees the device slot for a new account.
        """
        try:
            sync_execute(
                """UPDATE device_account_bindings
                   SET unbound_at = now(), notes = 'distribution_handoff'
                   WHERE account_id = %s AND device_id = %s AND unbound_at IS NULL""",
                (account_id, device_id),
            )
            sync_execute(
                """UPDATE accounts
                   SET current_state = 'active', warmer_device_id = NULL, updated_at = now()
                   WHERE id = %s""",
                (account_id,),
            )
            events.emit("scheduler", "info", "distribution_handoff",
                        f"Account {platform}/{username} handed off to distribution",
                        device_id=device_id, account_id=account_id,
                        context={
                            "platform": platform,
                            "username": username,
                        })
            logger.info("Handed off %s/%s to distribution (unbound from %s)",
                        platform, username, device_id[:8])
        except Exception:
            logger.error("Distribution handoff failed for %s/%s",
                         platform, username, exc_info=True)

    @staticmethod
    def _reset_device(session: WDASession) -> None:
        """Return device to a clean home screen state after a task."""
        session.reset_to_home()

    @staticmethod
    def _wait_for_wda(device: WDADevice, timeout: float = 30.0) -> bool | None:
        """Wait for WDA to become responsive.

        Returns True when WDA is ready, None when it is reachable but busy,
        and False when it appears unreachable.
        """
        deadline = time.time() + timeout
        saw_busy = False
        while time.time() < deadline:
            try:
                resp = httpx.get(f"{device.base_url}/status", timeout=5.0)
                if resp.status_code == 200 and resp.json().get("value", {}).get("ready"):
                    return True
            except httpx.ReadTimeout:
                saw_busy = True
            except Exception:
                pass
            time.sleep(2)
        return None if saw_busy else False

# ---------------------------------------------------------------------------
# Module-level singleton for CLI/dashboard access
# ---------------------------------------------------------------------------

_scheduler: DeviceScheduler | None = None


def get_scheduler() -> DeviceScheduler:
    """Get or create the global scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = DeviceScheduler()
    return _scheduler
