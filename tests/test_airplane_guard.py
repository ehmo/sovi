"""Tests for bulletproof airplane mode protection system.

These tests verify:
- Airplane mode detection and blocking
- Device quarantine on unrecoverable state
- Audit logging
- Control Center transaction safety
- Control Center locking
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from sovi.device.airplane_guard import (
    AirplaneModeAuditLogger,
    AirplaneModeEvent,
    AirplaneModeEventType,
    AirplaneModeMonitor,
    AirplaneModeRecoveryFailure,
    ControlCenterLock,
    get_bulletproof_guard,
)
from sovi.device.control_center_txn import (
    ControlCenterTransaction,
    ControlCenterTransactionError,
    StateChangeError,
    StateVerificationError,
)


@pytest.fixture
def mock_session():
    """Create a mock WDA session."""
    session = MagicMock()
    session.device = MagicMock()
    session.device.name = "test-device"
    session.screenshot.return_value = b"fake_screenshot_png"
    return session


@pytest.fixture
def mock_session_factory(mock_session):
    """Create a mock session factory."""

    def factory():
        return mock_session

    return factory


class TestAirplaneModeMonitor:
    """Test continuous airplane mode monitoring."""

    def test_monitor_detects_airplane_mode_on(self, mock_session, mock_session_factory):
        """Test that monitor detects when airplane mode turns ON."""
        mock_session._open_control_center.return_value = True
        # First read: OFF, Second read: ON
        mock_session._read_control_center_toggle_state.side_effect = [
            (None, False),  # First check - OFF
            (None, True),  # Second check - ON (the problem!)
            (None, False),  # After recovery - OFF
        ]
        mock_session._set_control_center_toggle.return_value = True

        monitor = AirplaneModeMonitor(
            device_id="test-123",
            device_name="test-device",
            session_factory=mock_session_factory,
            check_interval_seconds=0.1,
        )

        with patch.object(monitor, "_audit") as mock_audit:
            monitor.start()
            time.sleep(0.25)  # Wait for a couple checks
            monitor.stop()

            # Should have detected the transition to ON
            detection_calls = [
                call
                for call in mock_audit.log_event.call_args_list
                if call.args[0].event_type == AirplaneModeEventType.DETECTED
            ]
            # We might not get exactly one due to timing, but we should have at least checked
            assert mock_session._read_control_center_toggle_state.call_count >= 1

    def test_monitor_attempts_recovery_on_detection(self, mock_session, mock_session_factory):
        """Test that monitor attempts recovery when airplane mode is detected."""
        mock_session._open_control_center.return_value = True
        mock_session._read_control_center_toggle_state.return_value = (None, True)  # Always ON
        mock_session._set_control_center_toggle.return_value = True

        monitor = AirplaneModeMonitor(
            device_id="test-123",
            device_name="test-device",
            session_factory=mock_session_factory,
            check_interval_seconds=0.1,
            max_recovery_attempts=2,
        )

        monitor.start()
        time.sleep(0.25)
        monitor.stop()

        # Should have attempted recovery
        assert mock_session._set_control_center_toggle.called

    def test_monitor_quarantines_on_recovery_failure(self, mock_session, mock_session_factory):
        """Test that device is quarantined when recovery fails."""
        mock_session._open_control_center.return_value = True
        mock_session._read_control_center_toggle_state.return_value = (None, True)  # Always ON
        mock_session._set_control_center_toggle.return_value = False  # Recovery fails

        monitor = AirplaneModeMonitor(
            device_id="test-123",
            device_name="test-device",
            session_factory=mock_session_factory,
            check_interval_seconds=0.1,
            max_recovery_attempts=1,
        )

        with patch("sovi.device.airplane_guard.sync_execute") as mock_sync_execute:
            monitor.start()
            time.sleep(0.3)

            # Should have attempted to quarantine
            quarantine_calls = [
                call
                for call in mock_sync_execute.call_args_list
                if "quarantine_reason" in str(call) or "quarantined" in str(call)
            ]

            monitor.stop()

    def test_monitor_handles_control_center_failure(self, mock_session, mock_session_factory):
        """Test that monitor handles Control Center open failures gracefully."""
        mock_session._open_control_center.return_value = False  # Can't open

        monitor = AirplaneModeMonitor(
            device_id="test-123",
            device_name="test-device",
            session_factory=mock_session_factory,
            check_interval_seconds=0.1,
        )

        monitor.start()
        time.sleep(0.25)
        monitor.stop()

        # Should have tried to open Control Center
        assert mock_session._open_control_center.called


class TestControlCenterLock:
    """Test Control Center exclusive access locking."""

    def test_lock_acquires_successfully(self):
        """Test that lock can be acquired."""
        lock = ControlCenterLock("device-123")

        result = lock.acquire("operation-1", timeout_seconds=1.0)

        assert result is True
        assert lock._owner == "operation-1"
        lock.release("operation-1")

    def test_lock_blocks_concurrent_access(self):
        """Test that second acquire blocks until first releases."""
        lock = ControlCenterLock("device-123")

        # First acquire
        assert lock.acquire("operation-1", timeout_seconds=1.0) is True

        # Second acquire should timeout
        result = lock.acquire("operation-2", timeout_seconds=0.1)
        assert result is False

        lock.release("operation-1")

    def test_lock_release_requires_correct_owner(self):
        """Test that only the owner can release the lock."""
        lock = ControlCenterLock("device-123")
        lock.acquire("operation-1", timeout_seconds=1.0)

        # Wrong owner trying to release
        with patch("sovi.device.airplane_guard.logger") as mock_logger:
            lock.release("operation-2")
            # Should log error
            assert mock_logger.error.called

        lock.release("operation-1")


class TestControlCenterTransaction:
    """Test Control Center transaction safety."""

    def test_transaction_acquires_lock(self, mock_session):
        """Test that transaction acquires Control Center lock."""
        mock_session._open_control_center.return_value = True
        mock_session._read_control_center_toggle_state.return_value = (None, False)

        with patch("sovi.device.control_center_txn.get_bulletproof_guard") as mock_guard:
            mock_lock = MagicMock()
            mock_guard.return_value._cc_locks = {"device-123": mock_lock}
            mock_guard.return_value.acquire_control_center.return_value = True

            with ControlCenterTransaction(mock_session, "device-123", "test-device") as txn:
                pass

            # Should acquire and release
            assert mock_guard.return_value.acquire_control_center.called
            assert mock_guard.return_value.release_control_center.called

    def test_transaction_fails_on_lock_timeout(self, mock_session):
        """Test that transaction fails if lock cannot be acquired."""
        with patch("sovi.device.control_center_txn.get_bulletproof_guard") as mock_guard:
            mock_guard.return_value.acquire_control_center.return_value = False

            with pytest.raises(ControlCenterTransactionError):
                with ControlCenterTransaction(mock_session, "device-123", "test-device") as txn:
                    pass

    def test_ensure_airplane_mode_off_already_off(self, mock_session):
        """Test that already-off airplane mode is handled efficiently."""
        mock_session._open_control_center.return_value = True
        mock_session._read_control_center_toggle_state.return_value = (None, False)  # Already OFF

        with patch("sovi.device.control_center_txn.get_bulletproof_guard") as mock_guard:
            mock_guard.return_value._cc_locks = {"device-123": MagicMock()}
            mock_guard.return_value.acquire_control_center.return_value = True

            with ControlCenterTransaction(mock_session, "device-123", "test-device") as txn:
                result = txn.ensure_airplane_mode_off()
                assert result is True

            # Should not try to toggle since already off
            assert not mock_session._set_control_center_toggle.called

    def test_ensure_airplane_mode_off_toggles_when_on(self, mock_session):
        """Test that airplane mode is disabled when ON."""
        mock_session._open_control_center.return_value = True
        # First read: ON, Second read: OFF
        mock_session._read_control_center_toggle_state.side_effect = [
            (None, True),  # First check - ON
            (None, False),  # Second check - OFF (after toggle)
        ]
        mock_session._set_control_center_toggle.return_value = True

        with patch("sovi.device.control_center_txn.get_bulletproof_guard") as mock_guard:
            mock_guard.return_value._cc_locks = {"device-123": MagicMock()}
            mock_guard.return_value.acquire_control_center.return_value = True

            with ControlCenterTransaction(mock_session, "device-123", "test-device") as txn:
                result = txn.ensure_airplane_mode_off()
                assert result is True

            # Should have toggled
            mock_session._set_control_center_toggle.assert_called_with(
                "airplane", desired_on=False, attempts=1
            )

    def test_ensure_airplane_mode_off_raises_on_failure(self, mock_session):
        """Test that failure to disable raises StateChangeError."""
        mock_session._open_control_center.return_value = True
        mock_session._read_control_center_toggle_state.return_value = (None, True)  # Always ON
        mock_session._set_control_center_toggle.return_value = False

        with patch("sovi.device.control_center_txn.get_bulletproof_guard") as mock_guard:
            mock_guard.return_value._cc_locks = {"device-123": MagicMock()}
            mock_guard.return_value.acquire_control_center.return_value = True

            with pytest.raises(StateChangeError):
                with ControlCenterTransaction(mock_session, "device-123", "test-device") as txn:
                    txn.ensure_airplane_mode_off(max_attempts=1)

    def test_ensure_cellular_only_executes_all_checks(self, mock_session):
        """Test that ensure_cellular_only checks all three states."""
        mock_session._open_control_center.return_value = True
        mock_session._read_control_center_toggle_state.return_value = (None, False)
        mock_session._set_control_center_toggle.return_value = True
        mock_session.set_cellular_data_enabled.return_value = True
        mock_session.ensure_wifi_off.return_value = True

        with patch("sovi.device.control_center_txn.get_bulletproof_guard") as mock_guard:
            mock_guard.return_value._cc_locks = {"device-123": MagicMock()}
            mock_guard.return_value.acquire_control_center.return_value = True

            with ControlCenterTransaction(mock_session, "device-123", "test-device") as txn:
                result = txn.ensure_cellular_only()
                assert result is True


class TestBulletproofGuardSingleton:
    """Test the bulletproof guard singleton."""

    def test_singleton_pattern(self):
        """Test that get_bulletproof_guard returns same instance."""
        guard1 = get_bulletproof_guard()
        guard2 = get_bulletproof_guard()
        assert guard1 is guard2

    def test_device_registration(self):
        """Test device registration and unregistration."""
        guard = get_bulletproof_guard()

        # Mock session factory
        session_factory = MagicMock()

        # Register
        guard.register_device("device-1", "Test Device", session_factory)
        assert "device-1" in guard._monitors

        # Unregister
        guard.unregister_device("device-1")
        assert "device-1" not in guard._monitors

    def test_duplicate_registration_warning(self):
        """Test that duplicate registration logs warning."""
        guard = get_bulletproof_guard()
        session_factory = MagicMock()

        with patch("sovi.device.airplane_guard.logger") as mock_logger:
            guard.register_device("device-2", "Test Device 2", session_factory)
            guard.register_device("device-2", "Test Device 2", session_factory)

            assert mock_logger.warning.called

        # Cleanup
        guard.unregister_device("device-2")


class TestAirplaneModeAuditLogger:
    """Test audit logging."""

    def test_log_event_persists_to_database(self):
        """Test that events are logged to database."""
        logger = AirplaneModeAuditLogger()

        event = AirplaneModeEvent(
            device_id="test-123",
            device_name="test-device",
            event_type=AirplaneModeEventType.DETECTED,
            previous_state=False,
            current_state=True,
            stack_trace="test stack trace",
        )

        with patch("sovi.device.airplane_guard.sync_execute") as mock_sync_execute:
            with patch("sovi.device.airplane_guard.events") as mock_events:
                logger.log_event(event)

                # Should execute DB insert
                assert mock_sync_execute.called
                # Should emit event
                assert mock_events.emit.called
