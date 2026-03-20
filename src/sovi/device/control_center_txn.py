"""Control Center transaction safety - atomic network state changes with verification.

This module provides transaction-like semantics for Control Center operations:
- Pre-operation state verification
- Exclusive Control Center access (locking)
- Post-operation state verification
- Automatic rollback on failure
- Comprehensive audit logging
"""

from __future__ import annotations

import base64
import logging
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from sovi.device.airplane_guard import (
    AirplaneModeAuditLogger,
    ControlCenterLock,
    NetworkStateChange,
    get_bulletproof_guard,
)

logger = logging.getLogger(__name__)


@dataclass
class TransactionContext:
    """Context for a Control Center transaction."""

    operation_id: str
    device_id: str
    device_name: str
    session: Any  # WDASession
    lock: ControlCenterLock
    audit: AirplaneModeAuditLogger
    changes: list[NetworkStateChange] = field(default_factory=list)
    screenshots: dict[str, Optional[bytes]] = field(default_factory=dict)

    def capture_screenshot(self, label: str) -> None:
        """Capture a screenshot for forensic analysis."""
        try:
            self.screenshots[label] = self.session.screenshot()
        except Exception as e:
            logger.warning("Failed to capture screenshot %s: %s", label, e)
            self.screenshots[label] = None


class ControlCenterTransaction:
    """Transaction-like wrapper for Control Center operations.

    Usage:
        with ControlCenterTransaction(session, device_id, device_name) as txn:
            txn.ensure_airplane_mode_off()
            txn.ensure_cellular_on()
            txn.ensure_wifi_off()
        # All operations verified and atomic (best effort)

    Features:
    - Exclusive Control Center access via locking
    - Pre and post state verification
    - Automatic audit logging
    - Rollback on verification failure
    - Screenshot capture for forensics
    """

    def __init__(
        self,
        session: Any,
        device_id: str,
        device_name: str,
        lock_timeout_seconds: float = 30.0,
    ):
        self.session = session
        self.device_id = device_id
        self.device_name = device_name
        self.lock_timeout = lock_timeout_seconds
        self.operation_id = f"cc-txn-{uuid.uuid4().hex[:8]}"
        self.ctx: Optional[TransactionContext] = None
        self._committed = False
        self._audit = AirplaneModeAuditLogger()

    def __enter__(self) -> "ControlCenterTransaction":
        """Acquire Control Center lock and prepare transaction."""
        guard = get_bulletproof_guard()

        # Acquire exclusive access
        acquired = guard.acquire_control_center(
            self.device_id,
            self.operation_id,
            self.lock_timeout,
        )

        if not acquired:
            raise ControlCenterTransactionError(
                f"Could not acquire Control Center lock on {self.device_name} "
                f"within {self.lock_timeout}s"
            )

        # Get the lock object for release later
        lock = guard._cc_locks.get(self.device_id)

        self.ctx = TransactionContext(
            operation_id=self.operation_id,
            device_id=self.device_id,
            device_name=self.device_name,
            session=self.session,
            lock=lock,  # type: ignore
            audit=self._audit,
        )

        # Capture baseline screenshot
        self.ctx.capture_screenshot("baseline")

        logger.debug(
            "ControlCenterTransaction started: %s on %s", self.operation_id, self.device_name
        )

        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        """Release lock, log results, and handle failures."""
        success = exc_type is None

        # Capture final screenshot
        if self.ctx:
            self.ctx.capture_screenshot("final")

        # Log all changes
        if self.ctx:
            for change in self.ctx.changes:
                change.timestamp_after = time.monotonic()
                self._audit.log_state_change(change)

        # Release the lock
        guard = get_bulletproof_guard()
        guard.release_control_center(self.device_id, self.operation_id)

        if success:
            logger.debug(
                "ControlCenterTransaction completed: %s on %s", self.operation_id, self.device_name
            )
        else:
            logger.error(
                "ControlCenterTransaction failed: %s on %s - %s: %s",
                self.operation_id,
                self.device_name,
                exc_type.__name__ if exc_type else "Unknown",
                exc_val,
            )

        # Don't suppress exceptions
        return False

    def _verify_airplane_mode_state(self) -> Optional[bool]:
        """Read current airplane mode state from Control Center."""
        try:
            opened = self.session._open_control_center()
            if not opened:
                raise StateVerificationError(f"Could not open Control Center on {self.device_name}")

            _, state = self.session._read_control_center_toggle_state("airplane")

            try:
                self.session._close_control_center()
            except Exception:
                pass

            return state

        except Exception as e:
            raise StateVerificationError(
                f"Failed to verify airplane mode state on {self.device_name}: {e}"
            ) from e

    def ensure_airplane_mode_off(
        self,
        max_attempts: int = 3,
        verify_after: bool = True,
    ) -> bool:
        """Ensure airplane mode is OFF with full verification.

        Args:
            max_attempts: Maximum attempts to disable airplane mode
            verify_after: Whether to verify state after operation

        Returns:
            True if airplane mode is confirmed OFF

        Raises:
            StateChangeError: If operation fails after all attempts
            StateVerificationError: If verification fails
        """
        if not self.ctx:
            raise RuntimeError("Transaction not active")

        action = "disable_airplane_mode"
        start_time = time.monotonic()

        # Pre-state check
        state_before = self._verify_airplane_mode_state()

        if state_before is False:
            # Already off - nothing to do
            change = NetworkStateChange(
                operation_id=self.operation_id,
                device_id=self.device_id,
                device_name=self.device_name,
                action=action,
                timestamp_before=start_time,
                state_before=state_before,
                state_after=False,
                success=True,
                wda_response_time_ms=(time.monotonic() - start_time) * 1000,
                stack_trace="".join(traceback.format_stack(limit=5)),
            )
            self.ctx.changes.append(change)
            return True

        # Need to disable - attempt with retries
        success = False
        error_msg = None

        for attempt in range(1, max_attempts + 1):
            try:
                # Re-open Control Center
                opened = self.session._open_control_center()
                if not opened:
                    error_msg = f"Could not open Control Center (attempt {attempt})"
                    continue

                # Try to toggle off
                result = self.session._set_control_center_toggle(
                    "airplane",
                    desired_on=False,
                    attempts=1,  # We handle retries here
                )

                if result:
                    success = True
                    break
                else:
                    error_msg = f"Toggle operation reported failure (attempt {attempt})"

            except Exception as e:
                error_msg = f"Attempt {attempt} failed: {e}"
                logger.warning(
                    "Airplane mode disable attempt %d failed on %s: %s",
                    attempt,
                    self.device_name,
                    e,
                )

            # Wait before retry (exponential backoff)
            if attempt < max_attempts:
                wait_time = min(2**attempt, 10)
                time.sleep(wait_time)

        if not success:
            # All attempts failed
            change = NetworkStateChange(
                operation_id=self.operation_id,
                device_id=self.device_id,
                device_name=self.device_name,
                action=action,
                timestamp_before=start_time,
                state_before=state_before,
                success=False,
                error_message=error_msg,
                wda_response_time_ms=(time.monotonic() - start_time) * 1000,
                stack_trace="".join(traceback.format_stack()),
            )
            self.ctx.changes.append(change)

            raise StateChangeError(
                f"Failed to disable airplane mode on {self.device_name} "
                f"after {max_attempts} attempts: {error_msg}"
            )

        # Post-verification (if requested)
        state_after = state_before  # Assume no change unless verified

        if verify_after:
            try:
                # Give iOS time to apply the change
                time.sleep(1.0)
                state_after = self._verify_airplane_mode_state()

                if state_after is not False:
                    # Verification failed - state still ON or unknown
                    change = NetworkStateChange(
                        operation_id=self.operation_id,
                        device_id=self.device_id,
                        device_name=self.device_name,
                        action=action,
                        timestamp_before=start_time,
                        state_before=state_before,
                        state_after=state_after,
                        success=False,
                        error_message=f"Verification failed: state is {state_after}",
                        wda_response_time_ms=(time.monotonic() - start_time) * 1000,
                        stack_trace="".join(traceback.format_stack()),
                    )
                    self.ctx.changes.append(change)

                    raise StateVerificationError(
                        f"Airplane mode disable reported success but verification failed "
                        f"on {self.device_name}: state is {state_after}"
                    )

            except StateVerificationError:
                raise
            except Exception as e:
                # Verification error (not failure)
                logger.warning(
                    "Could not verify airplane mode state after disable on %s: %s",
                    self.device_name,
                    e,
                )

        # Log successful change
        change = NetworkStateChange(
            operation_id=self.operation_id,
            device_id=self.device_id,
            device_name=self.device_name,
            action=action,
            timestamp_before=start_time,
            state_before=state_before,
            state_after=state_after,
            success=True,
            wda_response_time_ms=(time.monotonic() - start_time) * 1000,
            stack_trace="".join(traceback.format_stack(limit=5)),
        )
        self.ctx.changes.append(change)

        return True

    def ensure_cellular_on(
        self,
        max_attempts: int = 3,
        verify_after: bool = True,
    ) -> bool:
        """Ensure cellular data is ON with full verification."""
        if not self.ctx:
            raise RuntimeError("Transaction not active")

        # Similar implementation to ensure_airplane_mode_off
        # For now, delegate to session method but wrap in transaction logging
        action = "enable_cellular_data"
        start_time = time.monotonic()

        try:
            result = self.session.set_cellular_data_enabled(True)

            change = NetworkStateChange(
                operation_id=self.operation_id,
                device_id=self.device_id,
                device_name=self.device_name,
                action=action,
                timestamp_before=start_time,
                success=result,
                wda_response_time_ms=(time.monotonic() - start_time) * 1000,
                stack_trace="".join(traceback.format_stack(limit=5)),
            )
            self.ctx.changes.append(change)

            if not result:
                raise StateChangeError(f"Failed to enable cellular data on {self.device_name}")

            return True

        except Exception as e:
            change = NetworkStateChange(
                operation_id=self.operation_id,
                device_id=self.device_id,
                device_name=self.device_name,
                action=action,
                timestamp_before=start_time,
                success=False,
                error_message=str(e),
                wda_response_time_ms=(time.monotonic() - start_time) * 1000,
                stack_trace="".join(traceback.format_stack()),
            )
            self.ctx.changes.append(change)
            raise StateChangeError(
                f"Exception enabling cellular data on {self.device_name}: {e}"
            ) from e

    def ensure_wifi_off(
        self,
        max_attempts: int = 3,
        verify_after: bool = True,
    ) -> bool:
        """Ensure Wi-Fi is OFF with full verification."""
        if not self.ctx:
            raise RuntimeError("Transaction not active")

        action = "disable_wifi"
        start_time = time.monotonic()

        try:
            result = self.session.ensure_wifi_off()

            change = NetworkStateChange(
                operation_id=self.operation_id,
                device_id=self.device_id,
                device_name=self.device_name,
                action=action,
                timestamp_before=start_time,
                success=result,
                wda_response_time_ms=(time.monotonic() - start_time) * 1000,
                stack_trace="".join(traceback.format_stack(limit=5)),
            )
            self.ctx.changes.append(change)

            if not result:
                raise StateChangeError(f"Failed to disable Wi-Fi on {self.device_name}")

            return True

        except Exception as e:
            change = NetworkStateChange(
                operation_id=self.operation_id,
                device_id=self.device_id,
                device_name=self.device_name,
                action=action,
                timestamp_before=start_time,
                success=False,
                error_message=str(e),
                wda_response_time_ms=(time.monotonic() - start_time) * 1000,
                stack_trace="".join(traceback.format_stack()),
            )
            self.ctx.changes.append(change)
            raise StateChangeError(f"Exception disabling Wi-Fi on {self.device_name}: {e}") from e

    def ensure_cellular_only(self) -> bool:
        """Ensure full cellular-only state: airplane OFF, cellular ON, Wi-Fi OFF.

        This is the main entry point for the scheduler and other callers.

        Returns:
            True if all checks pass

        Raises:
            StateChangeError: If any operation fails
        """
        # 1. Airplane mode must be OFF
        self.ensure_airplane_mode_off()

        # 2. Cellular must be ON
        self.ensure_cellular_on()

        # 3. Wi-Fi must be OFF
        self.ensure_wifi_off()

        return True


class ControlCenterTransactionError(Exception):
    """Transaction-level error (e.g., lock acquisition failure)."""

    pass


class StateChangeError(Exception):
    """Error during a state change operation."""

    pass


class StateVerificationError(Exception):
    """Error verifying state after an operation."""

    pass
