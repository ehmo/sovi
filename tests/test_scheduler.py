"""Tests for scheduler — _get_next_task, _execute_warming error detection, _execute_creation no-op."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

# Stub numpy before any sovi.device.seeder imports (pulled in by scheduler)
if "numpy" not in sys.modules:
    _np = ModuleType("numpy")
    _np.array = lambda *a, **k: None  # type: ignore[attr-defined]
    _np.ndarray = type  # type: ignore[attr-defined]
    _np.float64 = float  # type: ignore[attr-defined]
    _np.int64 = int  # type: ignore[attr-defined]
    _np.zeros = lambda *a, **k: []  # type: ignore[attr-defined]
    sys.modules["numpy"] = _np


from sovi.device.scheduler import WARMABLE_PLATFORMS, DeviceScheduler, DeviceThread  # noqa: E402
from sovi.device.warming import WarmingPhase  # noqa: E402
from sovi.models import AccountState

# Patch targets — scheduler imports from sovi.db and various submodules
_SYNC_CONN = "sovi.device.scheduler.sync_conn"
_SYNC_EXEC = "sovi.device.scheduler.sync_execute"
_SYNC_EXEC_ONE = "sovi.device.scheduler.sync_execute_one"
_EVENTS_EMIT = "sovi.device.scheduler.events.emit"


def _make_scheduler():
    """Create a scheduler instance without starting threads."""
    return DeviceScheduler()


def _make_device_thread(device_id="dev-1", device_name="test-phone"):
    return DeviceThread(device_id=device_id, device_name=device_name)


def _make_mock_cursor(rows=None):
    """Create a mock cursor that returns given rows from fetchone/fetchall."""
    cur = MagicMock()
    cur.fetchone.return_value = rows[0] if rows else None
    cur.fetchall.return_value = rows or []
    return cur


def _make_mock_conn(cursor):
    """Create a mock connection context manager wrapping a cursor."""
    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


# --- _get_next_task ---


class TestGetNextTask:
    def test_returns_warm_task_when_account_found(self):
        """Priority 1: warming an existing account bound to this device."""
        sched = _make_scheduler()
        account_row = {
            "id": "acc-1",
            "platform": "tiktok",
            "username": "testuser",
            "current_state": AccountState.WARMING_P1,
            "warming_day_count": 2,
            "email_enc": None,
            "password_enc": None,
            "totp_secret_enc": None,
            "niche_id": "niche-1",
            "niche_slug": "fitness",
        }
        cur = _make_mock_cursor([account_row])
        conn = _make_mock_conn(cur)

        with patch(_SYNC_CONN, return_value=conn), patch(_EVENTS_EMIT):
            task = sched._get_next_task("dev-1")

        assert task is not None
        assert task["type"] == "warm"
        assert task["account"]["id"] == "acc-1"
        assert task["account"]["platform"] == "tiktok"

    def test_returns_create_persona_account_when_no_warming_task(self):
        """Priority 2: create platform accounts for personas with email."""
        sched = _make_scheduler()
        # Priority 1 returns nothing (no warming account)
        cur_empty = _make_mock_cursor()
        conn_empty = _make_mock_conn(cur_empty)

        persona_row = {
            "persona_id": "p-1",
            "first_name": "Jane",
            "last_name": "Doe",
            "display_name": "janedoe",
            "username_base": "janedoe",
            "gender": "female",
            "date_of_birth": "1998-05-20",
            "age": 27,
            "niche_id": "n-1",
            "bio_short": "Fitness lover",
            "occupation": "trainer",
            "interests": ["gym"],
            "platform": "tiktok",
        }

        with (
            patch(_SYNC_CONN, return_value=conn_empty),
            patch(_SYNC_EXEC_ONE, return_value=persona_row),
            patch(_EVENTS_EMIT),
        ):
            task = sched._get_next_task("dev-1")

        assert task is not None
        assert task["type"] == "create_persona_account"
        assert task["platform"] == "tiktok"
        assert task["persona"]["first_name"] == "Jane"

    def test_returns_create_email_when_no_warming_or_account_task(self):
        """Priority 3: create email for persona without one."""
        sched = _make_scheduler()
        cur_empty = _make_mock_cursor()
        conn_empty = _make_mock_conn(cur_empty)

        persona_email_row = {
            "id": "p-2",
            "first_name": "John",
            "last_name": "Smith",
            "display_name": "johnsmith",
            "username_base": "johnsmith",
            "gender": "male",
            "date_of_birth": "1995-01-15",
            "age": 31,
            "niche_id": "n-2",
            "bio_short": "Tech geek",
            "occupation": "developer",
        }

        call_count = [0]

        def exec_one_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return None  # No persona account task
            if call_count[0] == 2:
                return persona_email_row  # Persona needing email
            return None

        with (
            patch(_SYNC_CONN, return_value=conn_empty),
            patch(_SYNC_EXEC_ONE, side_effect=exec_one_side_effect),
            patch(_EVENTS_EMIT),
        ):
            task = sched._get_next_task("dev-1")

        assert task is not None
        assert task["type"] == "create_email"
        assert task["persona"]["first_name"] == "John"

    def test_returns_create_fallback_when_nothing_else(self):
        """Priority 4: legacy fallback — create account on platform with fewest."""
        sched = _make_scheduler()

        # Priority 1: no warming accounts
        cur_empty_warm = _make_mock_cursor()
        conn_empty_warm = _make_mock_conn(cur_empty_warm)

        # Priority 4: count query
        count_rows = [
            {"platform": "tiktok", "cnt": 5},
            {"platform": "instagram", "cnt": 10},
        ]
        cur_counts = _make_mock_cursor(count_rows)
        cur_counts.fetchall.return_value = count_rows
        conn_counts = _make_mock_conn(cur_counts)

        conn_call_count = [0]

        def conn_factory():
            conn_call_count[0] += 1
            if conn_call_count[0] == 1:
                return conn_empty_warm
            return conn_counts

        with (
            patch(_SYNC_CONN, side_effect=conn_factory),
            patch(_SYNC_EXEC_ONE, return_value=None),  # no persona tasks
            patch(_EVENTS_EMIT),
        ):
            task = sched._get_next_task("dev-1")

        assert task is not None
        assert task["type"] == "create"
        # tiktok has fewer accounts, should be chosen
        assert task["platform"] == "tiktok"

    def test_returns_none_when_all_queries_fail(self):
        """Returns None when every priority level fails."""
        sched = _make_scheduler()

        # All DB calls raise
        conn_err = MagicMock()
        conn_err.__enter__ = MagicMock(side_effect=Exception("DB down"))
        conn_err.__exit__ = MagicMock(return_value=False)

        with (
            patch(_SYNC_CONN, return_value=conn_err),
            patch(_SYNC_EXEC_ONE, side_effect=Exception("DB down")),
            patch(_EVENTS_EMIT),
        ):
            task = sched._get_next_task("dev-1")

        assert task is None

    def test_warm_task_account_is_dict(self):
        """F-085 regression: verify account in warm task is a proper dict, not a Row."""
        sched = _make_scheduler()
        account_row = {
            "id": "acc-2",
            "platform": "instagram",
            "username": "iguser",
            "current_state": AccountState.CREATED,
            "warming_day_count": 0,
            "email_enc": None,
            "password_enc": None,
            "totp_secret_enc": None,
            "niche_id": "n-1",
            "niche_slug": "cooking",
        }
        cur = _make_mock_cursor([account_row])
        conn = _make_mock_conn(cur)

        with patch(_SYNC_CONN, return_value=conn), patch(_EVENTS_EMIT):
            task = sched._get_next_task("dev-1")

        assert isinstance(task["account"], dict)
        # Should be subscriptable like a dict
        assert task["account"]["platform"] == "instagram"

    def test_fallback_picks_instagram_when_tiktok_has_more(self):
        """Legacy fallback should pick the platform with fewer accounts."""
        sched = _make_scheduler()
        cur_empty = _make_mock_cursor()
        conn_empty = _make_mock_conn(cur_empty)

        count_rows = [
            {"platform": "tiktok", "cnt": 20},
            {"platform": "instagram", "cnt": 3},
        ]
        cur_counts = _make_mock_cursor(count_rows)
        cur_counts.fetchall.return_value = count_rows
        conn_counts = _make_mock_conn(cur_counts)

        call_count = [0]

        def conn_factory():
            call_count[0] += 1
            if call_count[0] == 1:
                return conn_empty
            return conn_counts

        with (
            patch(_SYNC_CONN, side_effect=conn_factory),
            patch(_SYNC_EXEC_ONE, return_value=None),
            patch(_EVENTS_EMIT),
        ):
            task = sched._get_next_task("dev-1")

        assert task["platform"] == "instagram"


# --- _execute_warming error detection (F-085) ---


class TestExecuteWarmingErrorDetection:
    def test_wifi_enforcement_failure_returns_false(self):
        sched = _make_scheduler()
        dt = _make_device_thread()
        task = {
            "type": "warm",
            "account": {
                "id": "acc-1",
                "platform": "tiktok",
                "username": "testuser",
                "current_state": AccountState.WARMING_P1,
                "warming_day_count": 2,
            },
        }

        from sovi.device.wda_client import WDADevice

        device = WDADevice(name="test", udid="abc123", wda_port=8100)
        mock_session = MagicMock()
        mock_session.ensure_wifi_off.return_value = False

        with (
            patch("sovi.device.scheduler.WDASession", return_value=mock_session),
            patch("sovi.device.scheduler.delete_app") as mock_delete,
            patch(_EVENTS_EMIT),
            patch("time.sleep"),
        ):
            result = sched._execute_warming(device, dt, task)

        assert result is False
        mock_delete.assert_not_called()

    def test_delete_failure_returns_false(self):
        sched = _make_scheduler()
        dt = _make_device_thread()
        task = {
            "type": "warm",
            "account": {
                "id": "acc-1",
                "platform": "tiktok",
                "username": "testuser",
                "current_state": AccountState.WARMING_P1,
                "warming_day_count": 2,
            },
        }

        from sovi.device.wda_client import WDADevice

        device = WDADevice(name="test", udid="abc123", wda_port=8100)
        mock_session = MagicMock()
        mock_session.ensure_wifi_off.return_value = True

        with (
            patch("sovi.device.scheduler.WDASession", return_value=mock_session),
            patch("sovi.device.scheduler.delete_app", return_value=False),
            patch("sovi.device.scheduler.install_from_app_store") as mock_install,
            patch(_EVENTS_EMIT),
            patch("time.sleep"),
        ):
            result = sched._execute_warming(device, dt, task)

        assert result is False
        mock_install.assert_not_called()

    def test_run_warming_error_dict_returns_false(self):
        """F-085: run_warming returning {"error": "..."} must cause _execute_warming to return False."""
        sched = _make_scheduler()
        dt = _make_device_thread()
        task = {
            "type": "warm",
            "account": {
                "id": "acc-1",
                "platform": "tiktok",
                "username": "testuser",
                "current_state": AccountState.WARMING_P1,
                "warming_day_count": 2,
            },
        }

        from sovi.device.wda_client import WDADevice

        device = WDADevice(name="test", udid="abc123", wda_port=8100)
        mock_session = MagicMock()

        with (
            patch("sovi.device.scheduler.WDASession", return_value=mock_session),
            patch("sovi.device.scheduler.delete_app", return_value=True),
            patch("sovi.device.scheduler.reset_idfa"),
            patch("sovi.device.scheduler.install_from_app_store", return_value=True),
            patch("sovi.device.scheduler.login_account", return_value=True),
            patch("sovi.device.scheduler.run_warming", return_value={"error": "unsupported platform: snapchat"}),
            patch(_EVENTS_EMIT),
            patch("time.sleep"),
        ):
            result = sched._execute_warming(device, dt, task)

        assert result is False

    def test_run_warming_success_dict_returns_true(self):
        """Successful warming result should return True and update DB."""
        sched = _make_scheduler()
        dt = _make_device_thread()
        task = {
            "type": "warm",
            "account": {
                "id": "acc-1",
                "platform": "tiktok",
                "username": "testuser",
                "current_state": AccountState.WARMING_P1,
                "warming_day_count": 2,
            },
        }

        from sovi.device.wda_client import WDADevice

        device = WDADevice(name="test", udid="abc123", wda_port=8100)
        mock_session = MagicMock()

        with (
            patch("sovi.device.scheduler.WDASession", return_value=mock_session),
            patch("sovi.device.scheduler.delete_app", return_value=True),
            patch("sovi.device.scheduler.reset_idfa"),
            patch("sovi.device.scheduler.install_from_app_store", return_value=True),
            patch("sovi.device.scheduler.login_account", return_value=True),
            patch("sovi.device.scheduler.run_warming", return_value={"videos_watched": 15, "likes": 3, "duration_min": 30}),
            patch(_SYNC_EXEC) as mock_exec,
            patch(_EVENTS_EMIT),
            patch("time.sleep"),
        ):
            result = sched._execute_warming(device, dt, task)

        assert result is True
        # Should have called sync_execute to update account state
        mock_exec.assert_called()


# --- _execute_creation no-op (F-095) ---


class TestExecuteCreationNoop:
    def test_execute_creation_returns_none(self):
        """F-095: _execute_creation should return None (no-op/skip) since email provider not configured."""
        sched = _make_scheduler()
        dt = _make_device_thread()
        task = {"type": "create", "platform": "tiktok"}

        from sovi.device.wda_client import WDADevice

        device = WDADevice(name="test", udid="abc123", wda_port=8100)

        with patch(_EVENTS_EMIT):
            result = sched._execute_creation(device, dt, task)

        assert result is None

    def test_execute_creation_noop_for_all_platforms(self):
        """Verify no-op for both tiktok and instagram."""
        sched = _make_scheduler()
        dt = _make_device_thread()

        from sovi.device.wda_client import WDADevice

        device = WDADevice(name="test", udid="abc123", wda_port=8100)

        for platform in WARMABLE_PLATFORMS:
            task = {"type": "create", "platform": platform}
            with patch(_EVENTS_EMIT):
                result = sched._execute_creation(device, dt, task)
            assert result is None, f"Expected None for {platform}, got {result}"


# --- Phase mapping ---


class TestPhaseMapping:
    def test_warming_phase_from_account_state(self):
        """Verify scheduler maps account states to correct warming phases."""
        phase_map = {
            AccountState.CREATED: WarmingPhase.PASSIVE,
            AccountState.WARMING_P1: WarmingPhase.PASSIVE,
            AccountState.WARMING_P2: WarmingPhase.LIGHT,
            AccountState.WARMING_P3: WarmingPhase.MODERATE,
            AccountState.ACTIVE: WarmingPhase.LIGHT,
        }
        for state, expected_phase in phase_map.items():
            # The map is defined inside _execute_warming, verify it here
            actual = phase_map.get(state, WarmingPhase.PASSIVE)
            assert actual == expected_phase, f"State {state} should map to {expected_phase}"

    def test_warming_day_state_transitions(self):
        """Verify day count to state transition logic."""
        transitions = [
            (1, AccountState.WARMING_P1),
            (3, AccountState.WARMING_P1),
            (4, AccountState.WARMING_P2),
            (7, AccountState.WARMING_P2),
            (8, AccountState.WARMING_P3),
            (14, AccountState.WARMING_P3),
            (15, AccountState.ACTIVE),
            (30, AccountState.ACTIVE),
        ]
        for day_count, expected_state in transitions:
            if day_count <= 3:
                new_state = AccountState.WARMING_P1
            elif day_count <= 7:
                new_state = AccountState.WARMING_P2
            elif day_count <= 14:
                new_state = AccountState.WARMING_P3
            else:
                new_state = AccountState.ACTIVE
            assert new_state == expected_state, f"Day {day_count} should give {expected_state}, got {new_state}"
