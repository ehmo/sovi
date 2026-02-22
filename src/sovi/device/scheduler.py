"""Continuous device scheduler — 95% utilization, 24/7 operation.

Architecture: one thread per device, each running an infinite loop:
  1. get_next_task() → warm existing account OR create new one
  2. Execute task (delete → install → login → warm 30 min → log)
  3. Emit events to system_events table
  4. Repeat

Task priority:
  1. Warm existing account (not yet warmed today, earlier phases first)
  2. Create new account (when no warming tasks remain)
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
import psycopg

from sovi import events
from sovi.config import settings
from sovi.crypto import decrypt
from sovi.db import sync_conn
from sovi.device.app_lifecycle import BUNDLES, delete_app, install_from_app_store, login_account
from sovi.device.device_registry import get_active_devices, set_device_status, to_wda_device, update_heartbeat
from sovi.device.warming import WarmingConfig, WarmingPhase, run_warming
from sovi.device.wda_client import WDADevice, WDASession

logger = logging.getLogger(__name__)

# Session timing constants
WARMING_DURATION_MIN = 30
OVERHEAD_MIN = 15  # delete + install + login + cooldown
SESSION_TOTAL_MIN = WARMING_DURATION_MIN + OVERHEAD_MIN  # 45 min
SESSIONS_PER_DAY = int(24 * 60 / SESSION_TOTAL_MIN)  # 32

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


class DeviceScheduler:
    """Continuous scheduler managing all devices."""

    def __init__(self) -> None:
        self._threads: dict[str, DeviceThread] = {}
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    def start(self) -> None:
        """Start scheduler threads for all active devices."""
        self._stop_event.clear()
        devices = get_active_devices()

        if not devices:
            logger.warning("No active devices found")
            events.emit("scheduler", "warning", "no_devices",
                       "Scheduler started but no active devices found")
            return

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
            }

        return {
            "running": self.is_running,
            "device_count": len(self._threads),
            "threads": thread_status,
            "sessions_per_day_target": SESSIONS_PER_DAY,
        }

    def _device_loop(self, device_row: dict[str, Any], dt: DeviceThread) -> None:
        """Main loop for a single device thread."""
        device = to_wda_device(device_row)
        device_id = dt.device_id

        logger.info("Device loop started: %s (port %d)", device.name, device.wda_port)

        while not self._stop_event.is_set() and dt.running:
            try:
                # Heartbeat
                update_heartbeat(device_id)
                dt.error = None

                # Wait for WDA to be responsive
                dt.current_task = "waiting_for_wda"
                if not self._wait_for_wda(device):
                    dt.current_task = "wda_unreachable"
                    dt.error = "WDA not responding"
                    events.emit("device", "critical", "device_disconnected",
                               f"WDA not responding on {device.name}",
                               device_id=device_id,
                               context={"device_name": device.name, "wda_port": device.wda_port})
                    set_device_status(device_id, "disconnected")
                    # Backoff and retry
                    self._stop_event.wait(60)
                    continue

                # Get next task
                dt.current_task = "selecting_task"
                task = self._get_next_task(device_id)

                if task is None:
                    # Nothing to do — short sleep and retry
                    dt.current_task = "idle"
                    self._stop_event.wait(30)
                    continue

                # Execute task
                if task["type"] == "warm":
                    self._execute_warming(device, dt, task)
                elif task["type"] == "create":
                    self._execute_creation(device, dt, task)

                dt.sessions_today += 1
                dt.last_session_at = datetime.now(timezone.utc)

                # Brief cooldown between sessions
                dt.current_task = "cooldown"
                self._stop_event.wait(30)

            except Exception:
                dt.error = "Unhandled exception in device loop"
                logger.error("Error in device loop for %s", device.name, exc_info=True)
                events.emit("scheduler", "error", "device_loop_error",
                           f"Unhandled error in {device.name} loop",
                           device_id=device_id,
                           context={"device_name": device.name})
                # Backoff on error
                self._stop_event.wait(60)

        dt.running = False
        dt.current_task = "stopped"
        logger.info("Device loop ended: %s", device.name)

    def _get_next_task(self, device_id: str) -> dict[str, Any] | None:
        """Determine the next task for a device.

        Priority:
        1. Warm existing account not yet warmed today
        2. Create new account on platform/niche with fewest accounts
        """
        # Try to claim an account that needs warming
        try:
            with sync_conn() as conn:
                with conn.cursor() as cur:
                    # Use FOR UPDATE SKIP LOCKED to avoid conflicts between device threads
                    cur.execute(
                        """SELECT a.id, a.platform, a.username, a.current_state,
                                  a.warming_day_count, a.email_enc, a.password_enc,
                                  a.totp_secret_enc, a.niche_id, n.slug as niche_slug
                           FROM accounts a
                           JOIN niches n ON a.niche_id = n.id
                           WHERE a.current_state IN ('created', 'warming_p1', 'warming_p2', 'warming_p3', 'active')
                             AND a.platform IN %s
                             AND a.deleted_at IS NULL
                             AND (a.last_warmed_at IS NULL
                                  OR a.last_warmed_at < CURRENT_DATE)
                           ORDER BY
                             CASE a.current_state
                               WHEN 'created' THEN 0
                               WHEN 'warming_p1' THEN 1
                               WHEN 'warming_p2' THEN 2
                               WHEN 'warming_p3' THEN 3
                               WHEN 'active' THEN 4
                             END,
                             a.last_warmed_at ASC NULLS FIRST
                           LIMIT 1
                           FOR UPDATE SKIP LOCKED""",
                        (WARMABLE_PLATFORMS,),
                    )
                    row = cur.fetchone()
                    conn.commit()

                    if row:
                        return {
                            "type": "warm",
                            "account": dict(zip(
                                ["id", "platform", "username", "current_state",
                                 "warming_day_count", "email_enc", "password_enc",
                                 "totp_secret_enc", "niche_id", "niche_slug"],
                                row,
                            )),
                        }
        except Exception:
            logger.error("Error getting next warming task", exc_info=True)

        # No warming tasks — create a new account
        # Pick platform with fewest accounts, alternating tiktok/instagram
        try:
            with sync_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """SELECT platform, COUNT(*) as cnt
                           FROM accounts
                           WHERE platform IN %s AND deleted_at IS NULL
                           GROUP BY platform""",
                        (WARMABLE_PLATFORMS,),
                    )
                    counts = {row[0]: row[1] for row in cur.fetchall()}
                    conn.commit()

            # Pick platform with fewer accounts
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
    ) -> None:
        """Execute a warming session."""
        account = task["account"]
        platform = account["platform"]
        username = account["username"]
        account_id = str(account["id"])
        device_id = dt.device_id

        dt.current_task = f"warming:{platform}/{username}"
        dt.current_account = username

        # Determine warming phase from account state
        state = account["current_state"]
        phase_map = {
            "created": WarmingPhase.PASSIVE,
            "warming_p1": WarmingPhase.PASSIVE,
            "warming_p2": WarmingPhase.LIGHT,
            "warming_p3": WarmingPhase.MODERATE,
            "active": WarmingPhase.LIGHT,
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

            # Step 1: Delete app for IDFV isolation
            dt.current_task = f"deleting:{platform}"
            delete_app(session, platform, device_id=device_id)
            time.sleep(2)

            # Step 2: Install fresh
            dt.current_task = f"installing:{platform}"
            if not install_from_app_store(session, platform, device_id=device_id):
                events.emit("scheduler", "error", "install_failed",
                           f"Failed to install {platform} for warming",
                           device_id=device_id, account_id=account_id,
                           context={"platform": platform, "retry_count": 0})
                return

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
                return

            # Step 4: Warm
            dt.current_task = f"warming:{platform}/{username}"
            config = WarmingConfig(
                device_name=device.name,
                platform=platform,
                phase=phase,
                duration_min=WARMING_DURATION_MIN,
            )
            result = run_warming(session, config)

            # Step 5: Update account state
            new_day_count = account["warming_day_count"] + 1
            # Phase transitions based on warming days
            if new_day_count <= 3:
                new_state = "warming_p1"
            elif new_day_count <= 7:
                new_state = "warming_p2"
            elif new_day_count <= 14:
                new_state = "warming_p3"
            else:
                new_state = "active"

            from sovi.db import sync_execute
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

        except Exception:
            logger.error("Warming failed for %s/%s on %s",
                        platform, username, device.name, exc_info=True)
            events.emit("scheduler", "error", "warming_failed",
                       f"Warming exception for {platform}/{username}",
                       device_id=device_id, account_id=account_id,
                       context={"platform": platform, "username": username})
        finally:
            try:
                session.press_button("home")
            except Exception:
                pass
            session.disconnect()
            dt.current_account = None
            time.sleep(2)

    def _execute_creation(
        self,
        device: WDADevice,
        dt: DeviceThread,
        task: dict[str, Any],
    ) -> None:
        """Execute an account creation task."""
        platform = task["platform"]
        device_id = dt.device_id
        dt.current_task = f"creating:{platform}"

        events.emit("scheduler", "info", "creation_started",
                    f"Creating new {platform} account on {device.name}",
                    device_id=device_id,
                    context={"platform": platform})

        # TODO: integrate with email provider to generate disposable email
        # For now, log that creation is needed and skip
        events.emit("scheduler", "warning", "creation_skipped",
                    f"Account creation for {platform} requires email provider integration",
                    device_id=device_id,
                    context={"platform": platform, "reason": "email_provider_not_configured"})

    @staticmethod
    def _wait_for_wda(device: WDADevice, timeout: float = 30.0) -> bool:
        """Wait for WDA to become responsive."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                resp = httpx.get(f"{device.base_url}/status", timeout=5.0)
                if resp.status_code == 200 and resp.json().get("value", {}).get("ready"):
                    return True
            except Exception:
                pass
            time.sleep(2)
        return False


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
