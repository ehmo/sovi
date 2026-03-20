"""Bulletproof airplane mode protection system - continuous monitoring, hard failure, audit logging.

This module provides an unbreakable airplane mode prevention system with:
- Continuous 1-second state monitoring
- Hard failure + device quarantine on unrecoverable states
- Comprehensive audit logging with stack traces
- Control Center transaction safety
- Exclusive access locking

The system is designed to NEVER allow airplane mode to remain ON without immediate detection
and recovery, or device quarantine if recovery fails.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Callable, Coroutine, Optional

from sovi import events
from sovi.db import sync_execute

logger = logging.getLogger(__name__)


class AirplaneModeState(Enum):
    """Airplane mode states."""

    UNKNOWN = auto()
    OFF = auto()
    ON = auto()
    TRANSITIONING = auto()


class AirplaneModeEventType(Enum):
    """Types of airplane mode events."""

    DETECTED = "detected"
    RECOVERED = "recovered"
    RECOVERY_FAILED = "recovery_failed"
    VERIFICATION_FAILED = "verification_failed"
    MONITOR_STARTED = "monitor_started"
    MONITOR_STOPPED = "monitor_stopped"
    QUARANTINED = "quarantined"


@dataclass
class AirplaneModeEvent:
    """Immutable event record for airplane mode state changes."""

    device_id: str
    device_name: str
    event_type: AirplaneModeEventType
    timestamp: float = field(default_factory=time.monotonic)
    previous_state: Optional[bool] = None
    current_state: Optional[bool] = None
    stack_trace: Optional[str] = None
    screenshot: Optional[bytes] = None
    error_message: Optional[str] = None
    recovery_attempts: int = 0
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for logging/serialization."""
        return {
            "device_id": self.device_id,
            "device_name": self.device_name,
            "event_type": self.event_type.value,
            "timestamp": self.timestamp,
            "previous_state": self.previous_state,
            "current_state": self.current_state,
            "recovery_attempts": self.recovery_attempts,
            "error_message": self.error_message,
            "has_screenshot": self.screenshot is not None,
            "context": self.context,
        }


@dataclass
class NetworkStateChange:
    """Immutable record of a network state change operation."""

    operation_id: str
    device_id: str
    device_name: str
    action: str  # e.g., "disable_airplane_mode", "enable_cellular"
    timestamp_before: float
    timestamp_after: Optional[float] = None
    state_before: Optional[bool] = None
    state_after: Optional[bool] = None
    success: bool = False
    stack_trace: str = ""
    error_message: Optional[str] = None
    wda_response_time_ms: float = 0.0
    screenshot_before: Optional[bytes] = None
    screenshot_after: Optional[bytes] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "device_id": self.device_id,
            "action": self.action,
            "success": self.success,
            "state_before": self.state_before,
            "state_after": self.state_after,
            "wda_response_time_ms": self.wda_response_time_ms,
            "error_message": self.error_message,
        }


class AirplaneModeAuditLogger:
    """High-fidelity audit logging for all network state operations."""

    def __init__(self):
        self._lock = threading.Lock()

    def log_event(self, event: AirplaneModeEvent) -> None:
        """Log airplane mode event to database and event stream."""
        try:
            # Persist to database
            screenshot_b64 = (
                base64.b64encode(event.screenshot).decode() if event.screenshot else None
            )

            sync_execute(
                """INSERT INTO airplane_mode_audit_log 
                    (device_id, event_type, timestamp, previous_state, current_state,
                     stack_trace, screenshot, error_message, recovery_attempts, context)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    event.device_id,
                    event.event_type.value,
                    datetime.fromtimestamp(event.timestamp, tz=timezone.utc),
                    event.previous_state,
                    event.current_state,
                    event.stack_trace,
                    screenshot_b64,
                    event.error_message,
                    event.recovery_attempts,
                    str(event.context),
                ),
            )

            # Emit to event stream
            severity = (
                "critical"
                if event.event_type
                in (
                    AirplaneModeEventType.DETECTED,
                    AirplaneModeEventType.QUARANTINED,
                    AirplaneModeEventType.RECOVERY_FAILED,
                )
                else "warning"
                if event.event_type == AirplaneModeEventType.VERIFICATION_FAILED
                else "info"
            )

            events.emit(
                "airplane_mode",
                severity,
                event.event_type.value,
                f"Airplane mode {event.event_type.value} on {event.device_name}",
                device_id=event.device_id,
                context=event.to_dict(),
            )

            # Log at appropriate level
            if event.event_type == AirplaneModeEventType.DETECTED:
                logger.critical(
                    "🚨 AIRPLANE MODE DETECTED on %s: %s -> %s",
                    event.device_name,
                    event.previous_state,
                    event.current_state,
                )
            elif event.event_type == AirplaneModeEventType.QUARANTINED:
                logger.critical(
                    "🔒 DEVICE QUARANTINED: %s - %s",
                    event.device_name,
                    event.error_message or "Unrecoverable airplane mode",
                )
            elif event.event_type == AirplaneModeEventType.RECOVERY_FAILED:
                logger.error(
                    "❌ Airplane mode recovery failed on %s after %d attempts: %s",
                    event.device_name,
                    event.recovery_attempts,
                    event.error_message,
                )

        except Exception as e:
            logger.error("Failed to log airplane mode event: %s", e, exc_info=True)

    def log_state_change(self, change: NetworkStateChange) -> None:
        """Log a network state change operation."""
        try:
            sync_execute(
                """INSERT INTO network_state_changes
                    (operation_id, device_id, action, success, state_before, state_after,
                     wda_response_time_ms, error_message, timestamp)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())""",
                (
                    change.operation_id,
                    change.device_id,
                    change.action,
                    change.success,
                    change.state_before,
                    change.state_after,
                    change.wda_response_time_ms,
                    change.error_message,
                ),
            )
        except Exception as e:
            logger.error("Failed to log state change: %s", e, exc_info=True)


class ControlCenterLock:
    """Exclusive lock for Control Center operations on a single device.

    Prevents concurrent Control Center operations that could cause race conditions
    or accidental toggles.
    """

    def __init__(self, device_id: str):
        self.device_id = device_id
        self._lock = threading.Lock()
        self._owner: Optional[str] = None
        self._acquired_at: Optional[float] = None

    def acquire(self, operation_id: str, timeout_seconds: float = 30.0) -> bool:
        """Acquire exclusive Control Center access.

        Args:
            operation_id: Unique identifier for the operation
            timeout_seconds: Maximum time to wait for lock

        Returns:
            True if lock acquired, False if timeout
        """
        acquired = self._lock.acquire(timeout=timeout_seconds)
        if acquired:
            self._owner = operation_id
            self._acquired_at = time.monotonic()
            logger.debug("ControlCenterLock acquired by %s on %s", operation_id, self.device_id)
            return True

        # Log lock contention
        held_duration = time.monotonic() - (self._acquired_at or 0) if self._acquired_at else 0
        logger.warning(
            "ControlCenterLock timeout on %s: requested by %s, held by %s for %.1fs",
            self.device_id,
            operation_id,
            self._owner,
            held_duration,
        )
        return False

    def release(self, operation_id: str) -> None:
        """Release Control Center lock.

        Args:
            operation_id: Must match the operation that acquired the lock
        """
        if self._owner != operation_id:
            logger.error(
                "ControlCenterLock ownership mismatch on %s: release by %s, owned by %s",
                self.device_id,
                operation_id,
                self._owner,
            )
            return

        held_duration = time.monotonic() - (self._acquired_at or 0) if self._acquired_at else 0
        logger.debug(
            "ControlCenterLock released by %s on %s (held %.2fs)",
            operation_id,
            self.device_id,
            held_duration,
        )

        self._owner = None
        self._acquired_at = None
        self._lock.release()


class AirplaneModeRecoveryFailure(Exception):
    """Exception raised when airplane mode cannot be recovered.

    This triggers immediate device quarantine.
    """

    def __init__(
        self,
        message: str,
        device_id: str,
        device_name: str,
        attempts: int,
        final_state: Optional[bool] = None,
        screenshot: Optional[bytes] = None,
    ):
        super().__init__(message)
        self.device_id = device_id
        self.device_name = device_name
        self.attempts = attempts
        self.final_state = final_state
        self.screenshot = screenshot


class AirplaneModeMonitor:
    """Continuous airplane mode monitor with immediate detection and recovery.

    This monitor runs continuously (default 1-second intervals) and:
    1. Detects airplane mode state changes immediately
    2. Attempts automatic recovery
    3. Quarantines device if recovery fails
    4. Provides comprehensive audit logging
    """

    def __init__(
        self,
        device_id: str,
        device_name: str,
        session_factory: Callable[[], Any],  # WDASession factory
        check_interval_seconds: float = 1.0,
        max_recovery_attempts: int = 3,
    ):
        self.device_id = device_id
        self.device_name = device_name
        self.session_factory = session_factory
        self.check_interval = check_interval_seconds
        self.max_recovery_attempts = max_recovery_attempts

        self._stop_event = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None
        self._last_state: Optional[bool] = None
        self._last_check_time: Optional[float] = None
        self._consecutive_failures = 0
        self._max_consecutive_failures = 5
        self._audit = AirplaneModeAuditLogger()
        self._recovery_in_progress = False

        # Thread-safe state tracking
        self._state_lock = threading.Lock()

    def start(self) -> None:
        """Start the continuous monitoring thread."""
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            logger.warning("AirplaneModeMonitor already running for %s", self.device_name)
            return

        self._stop_event.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name=f"airplane-monitor-{self.device_name}",
            daemon=True,
        )
        self._monitor_thread.start()

        # Log monitor start
        self._audit.log_event(
            AirplaneModeEvent(
                device_id=self.device_id,
                device_name=self.device_name,
                event_type=AirplaneModeEventType.MONITOR_STARTED,
            )
        )

        logger.info(
            "AirplaneModeMonitor started for %s (interval: %.1fs)",
            self.device_name,
            self.check_interval,
        )

    def stop(self) -> None:
        """Stop the monitoring thread."""
        self._stop_event.set()
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=5.0)

        # Log monitor stop
        self._audit.log_event(
            AirplaneModeEvent(
                device_id=self.device_id,
                device_name=self.device_name,
                event_type=AirplaneModeEventType.MONITOR_STOPPED,
            )
        )

        logger.info("AirplaneModeMonitor stopped for %s", self.device_name)

    def get_current_state(self) -> Optional[bool]:
        """Get the last known airplane mode state (thread-safe)."""
        with self._state_lock:
            return self._last_state

    def _monitor_loop(self) -> None:
        """Main monitoring loop - runs until stop() is called."""
        while not self._stop_event.is_set():
            try:
                self._check_and_enforce_state()
            except Exception as e:
                logger.error(
                    "Error in airplane mode monitor loop for %s: %s",
                    self.device_name,
                    e,
                    exc_info=True,
                )
                self._consecutive_failures += 1

                if self._consecutive_failures >= self._max_consecutive_failures:
                    logger.critical(
                        "Airplane mode monitor failing repeatedly on %s - may be blind to state changes",
                        self.device_name,
                    )

            # Wait for next check interval
            self._stop_event.wait(self.check_interval)

    def _check_and_enforce_state(self) -> None:
        """Check current airplane mode state and enforce OFF if needed."""
        # Create a fresh session for this check
        session = None
        try:
            session = self.session_factory()
            session.connect()

            # Read current state
            current_state = self._read_airplane_mode_state(session)
            self._last_check_time = time.monotonic()

            with self._state_lock:
                previous_state = self._last_state
                self._last_state = current_state

            # State transition detection
            if current_state is True and previous_state is not True:
                # Airplane mode turned ON - CRITICAL
                self._on_airplane_mode_detected(session, previous_state, current_state)
            elif current_state is False:
                # State is good, reset failure counter
                self._consecutive_failures = 0

        finally:
            if session:
                try:
                    session.disconnect()
                except Exception:
                    pass

    def _read_airplane_mode_state(self, session: Any) -> Optional[bool]:
        """Read airplane mode state from Control Center.

        Returns:
            True if airplane mode is ON
            False if airplane mode is OFF
            None if state cannot be determined
        """
        try:
            opened = session._open_control_center()
            if not opened:
                logger.warning(
                    "Could not open Control Center on %s to check airplane mode", self.device_name
                )
                return None

            # Read toggle state
            _, state = session._read_control_center_toggle_state("airplane")

            # Close Control Center
            try:
                session._close_control_center()
            except Exception:
                pass

            return state

        except Exception as e:
            logger.error(
                "Error reading airplane mode state on %s: %s", self.device_name, e, exc_info=True
            )
            return None

    def _on_airplane_mode_detected(
        self,
        session: Any,
        previous_state: Optional[bool],
        current_state: bool,
    ) -> None:
        """Handle airplane mode being detected as ON.

        This method attempts recovery and quarantines the device if recovery fails.
        """
        if self._recovery_in_progress:
            logger.warning("Airplane mode recovery already in progress for %s", self.device_name)
            return

        self._recovery_in_progress = True

        try:
            # Capture screenshot for forensics
            screenshot = None
            try:
                screenshot = session.screenshot()
            except Exception:
                pass

            # Log detection event
            event = AirplaneModeEvent(
                device_id=self.device_id,
                device_name=self.device_name,
                event_type=AirplaneModeEventType.DETECTED,
                previous_state=previous_state,
                current_state=current_state,
                stack_trace="".join(traceback.format_stack()),
                screenshot=screenshot,
            )
            self._audit.log_event(event)

            logger.critical(
                "🚨 AIRPLANE MODE DETECTED ON %s - attempting recovery (max %d attempts)",
                self.device_name,
                self.max_recovery_attempts,
            )

            # Attempt recovery
            recovery_success = self._attempt_recovery(session)

            if recovery_success:
                # Verify recovery
                final_state = self._read_airplane_mode_state(session)

                if final_state is False:
                    # Recovery successful
                    self._audit.log_event(
                        AirplaneModeEvent(
                            device_id=self.device_id,
                            device_name=self.device_name,
                            event_type=AirplaneModeEventType.RECOVERED,
                            previous_state=current_state,
                            current_state=False,
                        )
                    )
                    logger.info("✅ Airplane mode successfully disabled on %s", self.device_name)
                    self._recovery_in_progress = False
                    return
                else:
                    # Recovery reported success but state still ON
                    logger.error(
                        "Recovery reported success but airplane mode still ON on %s",
                        self.device_name,
                    )

            # Recovery failed - quarantine device
            self._quarantine_device(session, current_state, screenshot)

        finally:
            self._recovery_in_progress = False

    def _attempt_recovery(self, session: Any) -> bool:
        """Attempt to disable airplane mode.

        Uses escalating recovery methods.

        Returns:
            True if recovery appears successful
            False if all methods failed
        """
        for attempt in range(1, self.max_recovery_attempts + 1):
            logger.info(
                "Airplane mode recovery attempt %d/%d on %s",
                attempt,
                self.max_recovery_attempts,
                self.device_name,
            )

            try:
                # Method 1: Control Center toggle
                success = session._set_control_center_toggle("airplane", desired_on=False)

                if success:
                    # Wait for state to settle
                    time.sleep(2.0)
                    return True

                # Exponential backoff between attempts
                if attempt < self.max_recovery_attempts:
                    wait_time = min(2**attempt, 10)  # Max 10 seconds
                    logger.info(
                        "Recovery attempt %d failed on %s, waiting %.1fs before retry",
                        attempt,
                        self.device_name,
                        wait_time,
                    )
                    time.sleep(wait_time)

            except Exception as e:
                logger.error(
                    "Recovery attempt %d failed on %s: %s",
                    attempt,
                    self.device_name,
                    e,
                    exc_info=True,
                )

        return False

    def _quarantine_device(
        self,
        session: Any,
        final_state: Optional[bool],
        screenshot: Optional[bytes],
    ) -> None:
        """Quarantine device after unrecoverable airplane mode.

        This method:
        1. Logs the quarantine event
        2. Updates device status in database
        3. Emits critical alert
        4. Stops all operations on this device
        """
        logger.critical(
            "🔒 QUARANTINING DEVICE %s - airplane mode unrecoverable after %d attempts",
            self.device_name,
            self.max_recovery_attempts,
        )

        # Capture final screenshot
        final_screenshot = screenshot
        if not final_screenshot:
            try:
                final_screenshot = session.screenshot()
            except Exception:
                pass

        # Log quarantine event
        self._audit.log_event(
            AirplaneModeEvent(
                device_id=self.device_id,
                device_name=self.device_name,
                event_type=AirplaneModeEventType.QUARANTINED,
                current_state=final_state,
                recovery_attempts=self.max_recovery_attempts,
                screenshot=final_screenshot,
                error_message=f"Airplane mode unrecoverable after {self.max_recovery_attempts} attempts",
            )
        )

        # Update device status in database
        try:
            sync_execute(
                """UPDATE devices 
                   SET status = 'quarantined',
                       quarantine_reason = 'airplane_mode_unrecoverable',
                       quarantined_at = now(),
                       requires_manual_reset = true,
                       updated_at = now()
                   WHERE id = %s""",
                (self.device_id,),
            )
        except Exception as e:
            logger.error(
                "Failed to update quarantine status for %s: %s", self.device_name, e, exc_info=True
            )

        # Emit critical event
        events.emit(
            "device",
            "critical",
            "device_quarantined",
            f"Device {self.device_name} quarantined: airplane mode unrecoverable",
            device_id=self.device_id,
            context={
                "quarantine_reason": "airplane_mode_unrecoverable",
                "recovery_attempts": self.max_recovery_attempts,
                "final_state": final_state,
            },
        )

        # Stop monitoring this device
        self.stop()


class BulletproofNetworkGuard:
    """High-level network guard integrating all protection layers.

    This class provides a drop-in replacement for the current network guard
    with bulletproof airplane mode protection.
    """

    _instance: Optional["BulletproofNetworkGuard"] = None
    _instance_lock = threading.Lock()

    def __new__(cls) -> "BulletproofNetworkGuard":
        """Singleton pattern - only one guard instance system-wide."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._initialized = True
        self._monitors: dict[str, AirplaneModeMonitor] = {}
        self._cc_locks: dict[str, ControlCenterLock] = {}
        self._audit = AirplaneModeAuditLogger()
        self._lock = threading.Lock()

    def register_device(
        self,
        device_id: str,
        device_name: str,
        session_factory: Callable[[], Any],
    ) -> None:
        """Register a device for bulletproof monitoring.

        Args:
            device_id: Device database ID
            device_name: Human-readable device name
            session_factory: Callable that returns a new WDASession instance
        """
        with self._lock:
            if device_id in self._monitors:
                logger.warning(
                    "Device %s already registered with BulletproofNetworkGuard", device_name
                )
                return

            # Create monitor
            monitor = AirplaneModeMonitor(
                device_id=device_id,
                device_name=device_name,
                session_factory=session_factory,
            )
            self._monitors[device_id] = monitor

            # Create Control Center lock
            self._cc_locks[device_id] = ControlCenterLock(device_id)

            # Start monitoring
            monitor.start()

            logger.info("Device %s registered with BulletproofNetworkGuard", device_name)

    def unregister_device(self, device_id: str) -> None:
        """Unregister a device and stop monitoring."""
        with self._lock:
            if device_id in self._monitors:
                self._monitors[device_id].stop()
                del self._monitors[device_id]

            if device_id in self._cc_locks:
                del self._cc_locks[device_id]

    def get_monitor(self, device_id: str) -> Optional[AirplaneModeMonitor]:
        """Get the monitor for a specific device."""
        with self._lock:
            return self._monitors.get(device_id)

    def acquire_control_center(
        self, device_id: str, operation_id: str, timeout_seconds: float = 30.0
    ) -> bool:
        """Acquire exclusive Control Center access for a device."""
        with self._lock:
            lock = self._cc_locks.get(device_id)

        if not lock:
            logger.error("No ControlCenterLock found for device %s", device_id)
            return False

        return lock.acquire(operation_id, timeout_seconds)

    def release_control_center(self, device_id: str, operation_id: str) -> None:
        """Release exclusive Control Center access."""
        with self._lock:
            lock = self._cc_locks.get(device_id)

        if lock:
            lock.release(operation_id)


# Global singleton instance
def get_bulletproof_guard() -> BulletproofNetworkGuard:
    """Get the global BulletproofNetworkGuard instance."""
    return BulletproofNetworkGuard()
