"""Tests for identity_guard — pre-session checks, session lifecycle, dataclasses."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from sovi.device.identity_guard import (
    CheckResult,
    PreSessionReport,
    check_cooldown,
    check_daily_cap,
    check_no_concurrent_session,
    check_proxy_assigned,
    end_session,
    run_pre_session_checks,
    start_session,
    validate_device_account_affinity,
)

DEVICE_ID = "aaaa-bbbb-cccc-dddd"
ACCOUNT_ID = "1111-2222-3333-4444"

# Patch targets — identity_guard imports directly from sovi.db
_SYNC_EXEC = "sovi.device.identity_guard.sync_execute"
_SYNC_EXEC_ONE = "sovi.device.identity_guard.sync_execute_one"


@pytest.fixture
def mock_ig_db():
    """Patch sync_execute/sync_execute_one where identity_guard imports them."""
    mock = MagicMock()
    mock.execute.return_value = []
    mock.execute_one.return_value = None
    with (
        patch(_SYNC_EXEC, side_effect=mock.execute),
        patch(_SYNC_EXEC_ONE, side_effect=mock.execute_one),
    ):
        yield mock


# --- Dataclass tests ---


class TestCheckResult:
    def test_defaults(self):
        r = CheckResult("test_check", True)
        assert r.name == "test_check"
        assert r.passed is True
        assert r.detail == ""
        assert r.wait_seconds == 0

    def test_failed_with_wait(self):
        r = CheckResult("cooldown", False, "too fast", wait_seconds=120.5)
        assert r.passed is False
        assert r.wait_seconds == 120.5


class TestPreSessionReport:
    def test_passed_report(self):
        report = PreSessionReport(
            device_id=DEVICE_ID,
            account_id=ACCOUNT_ID,
            passed=True,
            checks=[CheckResult("c1", True, "ok")],
        )
        assert report.passed is True
        assert report.wait_seconds == 0

    def test_to_dict(self):
        report = PreSessionReport(
            device_id=DEVICE_ID,
            account_id=ACCOUNT_ID,
            passed=False,
            wait_seconds=30,
            checks=[
                CheckResult("proxy_assigned", True, "healthy"),
                CheckResult("cooldown", False, "wait 30s"),
            ],
        )
        d = report.to_dict()
        assert d["passed"] is False
        assert d["device_id"] == DEVICE_ID
        assert d["account_id"] == ACCOUNT_ID
        assert d["wait_seconds"] == 30
        assert d["checks"]["proxy_assigned"]["passed"] is True
        assert d["checks"]["cooldown"]["passed"] is False

    def test_to_dict_empty_checks(self):
        report = PreSessionReport(device_id="x", account_id=None, passed=True)
        d = report.to_dict()
        assert d["checks"] == {}
        assert d["account_id"] is None


# --- Individual checks (all DB interactions mocked) ---


class TestCheckNoConcurrentSession:
    def test_no_open_session(self, mock_ig_db):
        mock_ig_db.execute_one.return_value = None
        result = check_no_concurrent_session(DEVICE_ID)
        assert result.passed is True
        assert result.name == "no_concurrent_session"

    def test_active_recent_session_fails(self, mock_ig_db):
        recent = datetime.now(timezone.utc) - timedelta(minutes=30)
        mock_ig_db.execute_one.return_value = {"id": "sess-1", "started_at": recent}
        result = check_no_concurrent_session(DEVICE_ID)
        assert result.passed is False
        assert result.wait_seconds == 30
        assert "sess-1" in result.detail

    def test_stale_session_auto_closes(self, mock_ig_db):
        stale = datetime.now(timezone.utc) - timedelta(hours=3)
        mock_ig_db.execute_one.return_value = {"id": "sess-old", "started_at": stale}
        result = check_no_concurrent_session(DEVICE_ID)
        assert result.passed is True
        assert "Auto-closed" in result.detail
        mock_ig_db.execute.assert_called_once()

    def test_naive_datetime_gets_utc(self, mock_ig_db):
        naive = datetime.now() - timedelta(hours=3)
        mock_ig_db.execute_one.return_value = {"id": "sess-naive", "started_at": naive}
        result = check_no_concurrent_session(DEVICE_ID)
        assert result.passed is True


class TestCheckProxyAssigned:
    def test_no_proxy(self, mock_ig_db):
        mock_ig_db.execute_one.return_value = None
        result = check_proxy_assigned(DEVICE_ID)
        assert result.passed is False
        assert "No proxy" in result.detail

    def test_unhealthy_proxy(self, mock_ig_db):
        mock_ig_db.execute_one.return_value = {
            "id": "p1", "is_healthy": False, "host": "1.2.3.4", "port": 1080
        }
        result = check_proxy_assigned(DEVICE_ID)
        assert result.passed is False
        assert "unhealthy" in result.detail

    def test_healthy_proxy(self, mock_ig_db):
        mock_ig_db.execute_one.return_value = {
            "id": "p1", "is_healthy": True, "host": "1.2.3.4", "port": 1080
        }
        result = check_proxy_assigned(DEVICE_ID)
        assert result.passed is True
        assert "1.2.3.4:1080" in result.detail


class TestCheckDailyCap:
    def test_under_cap(self, mock_ig_db):
        mock_ig_db.execute_one.return_value = {"cnt": 5}
        with patch("sovi.device.identity_guard.settings") as mock_settings:
            mock_settings.max_sessions_per_device_day = 24
            result = check_daily_cap(DEVICE_ID)
        assert result.passed is True
        assert "5/24" in result.detail

    def test_at_cap(self, mock_ig_db):
        mock_ig_db.execute_one.return_value = {"cnt": 24}
        with patch("sovi.device.identity_guard.settings") as mock_settings:
            mock_settings.max_sessions_per_device_day = 24
            result = check_daily_cap(DEVICE_ID)
        assert result.passed is False
        assert result.wait_seconds == 3600

    def test_no_row_defaults_zero(self, mock_ig_db):
        mock_ig_db.execute_one.return_value = None
        with patch("sovi.device.identity_guard.settings") as mock_settings:
            mock_settings.max_sessions_per_device_day = 24
            result = check_daily_cap(DEVICE_ID)
        assert result.passed is True


class TestCheckCooldown:
    def test_no_previous_session(self, mock_ig_db):
        mock_ig_db.execute_one.return_value = None
        result = check_cooldown(DEVICE_ID)
        assert result.passed is True
        assert "No previous" in result.detail

    def test_null_ended_at(self, mock_ig_db):
        mock_ig_db.execute_one.return_value = {"last_session_ended_at": None}
        result = check_cooldown(DEVICE_ID)
        assert result.passed is True

    def test_cooldown_not_elapsed(self, mock_ig_db):
        just_now = datetime.now(timezone.utc) - timedelta(seconds=10)
        mock_ig_db.execute_one.return_value = {"last_session_ended_at": just_now}
        with patch("sovi.device.identity_guard.settings") as mock_settings:
            mock_settings.min_cooldown_seconds = 300
            mock_settings.max_cooldown_seconds = 900
            with patch("sovi.device.identity_guard.random.uniform", return_value=600):
                result = check_cooldown(DEVICE_ID)
        assert result.passed is False
        assert result.wait_seconds > 0

    def test_cooldown_satisfied(self, mock_ig_db):
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        mock_ig_db.execute_one.return_value = {"last_session_ended_at": long_ago}
        with patch("sovi.device.identity_guard.settings") as mock_settings:
            mock_settings.min_cooldown_seconds = 300
            mock_settings.max_cooldown_seconds = 900
            with patch("sovi.device.identity_guard.random.uniform", return_value=600):
                result = check_cooldown(DEVICE_ID)
        assert result.passed is True
        assert "satisfied" in result.detail

    def test_naive_ended_at_gets_utc(self, mock_ig_db):
        naive = datetime.now() - timedelta(hours=1)
        mock_ig_db.execute_one.return_value = {"last_session_ended_at": naive}
        with patch("sovi.device.identity_guard.settings") as mock_settings:
            mock_settings.min_cooldown_seconds = 300
            mock_settings.max_cooldown_seconds = 900
            with patch("sovi.device.identity_guard.random.uniform", return_value=600):
                result = check_cooldown(DEVICE_ID)
        assert result.passed is True


class TestDeviceAccountAffinity:
    def test_bound_to_correct_device(self, mock_ig_db):
        mock_ig_db.execute_one.return_value = {"device_id": DEVICE_ID}
        result = validate_device_account_affinity(DEVICE_ID, ACCOUNT_ID)
        assert result.passed is True

    def test_bound_to_different_device(self, mock_ig_db):
        mock_ig_db.execute_one.return_value = {"device_id": "other-device-id-1234"}
        result = validate_device_account_affinity(DEVICE_ID, ACCOUNT_ID)
        assert result.passed is False
        assert "different device" in result.detail

    def test_auto_bind_on_first_use(self, mock_ig_db):
        mock_ig_db.execute_one.return_value = None
        mock_ig_db.execute.return_value = []
        with patch("sovi.device.identity_guard.events.emit"):
            result = validate_device_account_affinity(DEVICE_ID, ACCOUNT_ID)
        assert result.passed is True
        assert "Auto-bound" in result.detail

    def test_auto_bind_failure(self, mock_ig_db):
        mock_ig_db.execute_one.return_value = None
        mock_ig_db.execute.side_effect = Exception("DB error")
        result = validate_device_account_affinity(DEVICE_ID, ACCOUNT_ID)
        assert result.passed is False
        assert "Auto-bind failed" in result.detail


# --- run_pre_session_checks ---


class TestRunPreSessionChecks:
    def test_kill_switch_disabled(self):
        with patch("sovi.device.identity_guard.settings") as mock_settings:
            mock_settings.identity_guard_enabled = False
            report = run_pre_session_checks(DEVICE_ID)
        assert report.passed is True
        assert report.checks[0].name == "kill_switch"

    def test_all_checks_pass(self, mock_ig_db):
        mock_ig_db.execute_one.side_effect = [
            None,                                              # no_concurrent_session
            {"id": "p1", "is_healthy": True, "host": "1.2.3.4", "port": 1080},  # proxy
            {"cnt": 2},                                        # daily_cap
            {"last_session_ended_at": None},                   # cooldown
        ]
        with patch("sovi.device.identity_guard.settings") as mock_settings:
            mock_settings.identity_guard_enabled = True
            mock_settings.max_sessions_per_device_day = 24
            with patch("sovi.device.identity_guard.events.emit"):
                report = run_pre_session_checks(DEVICE_ID)
        assert report.passed is True
        assert len(report.checks) == 4

    def test_includes_affinity_when_account_id(self, mock_ig_db):
        mock_ig_db.execute_one.side_effect = [
            None,
            {"id": "p1", "is_healthy": True, "host": "h", "port": 1},
            {"cnt": 0},
            {"last_session_ended_at": None},
            {"device_id": DEVICE_ID},
        ]
        with patch("sovi.device.identity_guard.settings") as mock_settings:
            mock_settings.identity_guard_enabled = True
            mock_settings.max_sessions_per_device_day = 24
            with patch("sovi.device.identity_guard.events.emit"):
                report = run_pre_session_checks(DEVICE_ID, account_id=ACCOUNT_ID)
        assert len(report.checks) == 5
        assert report.checks[4].name == "device_affinity"
        assert report.passed is True

    def test_failed_check_sets_passed_false(self, mock_ig_db):
        mock_ig_db.execute_one.side_effect = [
            None,
            None,  # proxy: no proxy -> fails
            {"cnt": 0},
            {"last_session_ended_at": None},
        ]
        with patch("sovi.device.identity_guard.settings") as mock_settings:
            mock_settings.identity_guard_enabled = True
            mock_settings.max_sessions_per_device_day = 24
            with patch("sovi.device.identity_guard.events.emit"):
                report = run_pre_session_checks(DEVICE_ID)
        assert report.passed is False


# --- Session lifecycle ---


class TestStartSession:
    def test_start_returns_session_id(self, mock_ig_db):
        mock_ig_db.execute_one.return_value = {"id": "sess-123"}
        sid = start_session(DEVICE_ID, ACCOUNT_ID, "warming", proxy_id="p1")
        assert sid == "sess-123"

    def test_start_returns_none_on_error(self, mock_ig_db):
        mock_ig_db.execute_one.side_effect = Exception("DB error")
        sid = start_session(DEVICE_ID, ACCOUNT_ID, "warming")
        assert sid is None

    def test_start_passes_identity_checks_as_json(self, mock_ig_db):
        mock_ig_db.execute_one.return_value = {"id": "s1"}
        checks = {"proxy": True}
        start_session(DEVICE_ID, ACCOUNT_ID, "warming", identity_checks=checks)
        # call_args[0] = (query, params_tuple); params_tuple[-1] = JSON string
        params_tuple = mock_ig_db.execute_one.call_args[0][1]
        assert '"proxy"' in params_tuple[-1]


class TestEndSession:
    def test_end_session_updates_device(self, mock_ig_db):
        mock_ig_db.execute_one.return_value = {"device_id": DEVICE_ID}
        end_session("sess-123", "success")
        mock_ig_db.execute_one.assert_called_once()
        mock_ig_db.execute.assert_called_once()

    def test_end_session_no_row(self, mock_ig_db):
        mock_ig_db.execute_one.return_value = None
        end_session("sess-missing", "success")
        mock_ig_db.execute.assert_not_called()

    def test_end_session_handles_exception(self, mock_ig_db):
        mock_ig_db.execute_one.side_effect = Exception("DB error")
        end_session("sess-err", "error")
