"""Tests for roles module — role assignment, seeder task queue, claiming."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from sovi.device.roles import (
    IDLE,
    SEEDER,
    SEEDER_COOLDOWN_MINUTES,
    SEEDER_COUNT,
    WARMER,
    bootstrap_roles,
    claim_seeder_task,
    complete_seeder_task,
    fail_seeder_task,
    get_current_role,
    dedupe_open_seeder_tasks,
    get_seeders,
    get_warmer_with_fewest_bindings,
    get_warmers,
    is_in_cooldown,
    populate_seeder_tasks,
    recover_interrupted_seeder_tasks,
)


# Patch targets
_SYNC_CONN = "sovi.device.roles.sync_conn"
_SYNC_EXEC = "sovi.device.roles.sync_execute"
_SYNC_EXEC_ONE = "sovi.device.roles.sync_execute_one"
_EVENTS_EMIT = "sovi.device.roles.events.emit"


# --- Constants ---


class TestRoleConstants:
    def test_role_values(self):
        assert SEEDER == "seeder"
        assert WARMER == "warmer"
        assert IDLE == "idle"

    def test_seeder_count(self):
        assert SEEDER_COUNT == 2

    def test_cooldown_minutes(self):
        assert SEEDER_COOLDOWN_MINUTES == 30


# --- get_current_role ---


class TestGetCurrentRole:
    def test_returns_role_from_db(self):
        with patch(_SYNC_EXEC_ONE, return_value={"current_role": "seeder"}):
            assert get_current_role("dev-1") == "seeder"

    def test_returns_warmer(self):
        with patch(_SYNC_EXEC_ONE, return_value={"current_role": "warmer"}):
            assert get_current_role("dev-1") == "warmer"

    def test_returns_idle_when_null(self):
        with patch(_SYNC_EXEC_ONE, return_value={"current_role": None}):
            assert get_current_role("dev-1") == IDLE

    def test_returns_idle_when_no_row(self):
        with patch(_SYNC_EXEC_ONE, return_value=None):
            assert get_current_role("dev-1") == IDLE


# --- is_in_cooldown ---


class TestIsInCooldown:
    def test_no_cooldown_set(self):
        with patch(_SYNC_EXEC_ONE, return_value={"seeder_cooldown_until": None}):
            assert is_in_cooldown("dev-1") is False

    def test_no_row(self):
        with patch(_SYNC_EXEC_ONE, return_value=None):
            assert is_in_cooldown("dev-1") is False

    def test_cooldown_in_future(self):
        future = datetime.now(timezone.utc) + timedelta(minutes=20)
        with patch(_SYNC_EXEC_ONE, return_value={"seeder_cooldown_until": future}):
            assert is_in_cooldown("dev-1") is True

    def test_cooldown_in_past(self):
        past = datetime.now(timezone.utc) - timedelta(minutes=10)
        with patch(_SYNC_EXEC_ONE, return_value={"seeder_cooldown_until": past}):
            assert is_in_cooldown("dev-1") is False

    def test_naive_datetime_treated_as_utc(self):
        """Naive datetimes get UTC tzinfo stamped before comparison."""
        # Use utcnow() equivalent to match what the code assumes (naive = UTC)
        future_naive = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=20)
        with patch(_SYNC_EXEC_ONE, return_value={"seeder_cooldown_until": future_naive}):
            assert is_in_cooldown("dev-1") is True


# --- bootstrap_roles ---


class TestBootstrapRoles:
    def test_skips_when_no_active_devices(self):
        with patch(_SYNC_EXEC, return_value=[]):
            bootstrap_roles()  # Should not raise

    def test_skips_when_roles_already_assigned(self):
        """If roles are already assigned, bootstrap is a no-op."""
        devices = [
            {"id": "d1", "name": "phone-1"},
            {"id": "d2", "name": "phone-2"},
            {"id": "d3", "name": "phone-3"},
        ]
        assigned = [{"id": "d1"}]

        call_count = [0]

        def exec_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return devices  # active devices
            if call_count[0] == 2:
                return assigned  # already assigned
            return []

        with patch(_SYNC_EXEC, side_effect=exec_side_effect):
            bootstrap_roles()  # Should return early

    def test_assigns_seeders_and_warmers(self):
        """First SEEDER_COUNT devices become seeders, rest become warmers."""
        devices = [
            {"id": f"d{i}", "name": f"phone-{i}"} for i in range(5)
        ]

        call_count = [0]

        def exec_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return devices  # active devices
            if call_count[0] == 2:
                return []  # no roles assigned yet
            return []

        mock_cur = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with (
            patch(_SYNC_EXEC, side_effect=exec_side_effect),
            patch(_SYNC_CONN, return_value=mock_conn),
            patch(_EVENTS_EMIT),
        ):
            bootstrap_roles()

        # Should have executed cursor commands for seeders + warmers
        assert mock_cur.execute.call_count > 0


# --- populate_seeder_tasks ---


class TestPopulateSeederTasks:
    def test_creates_email_tasks(self):
        email_rows = [{"persona_id": "p1"}, {"persona_id": "p2"}]

        call_count = [0]

        def exec_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return email_rows  # personas needing emails
            return []  # all platform queries return empty

        with patch(_SYNC_EXEC, side_effect=exec_side_effect):
            created = populate_seeder_tasks()

        assert created >= 2  # At least 2 email tasks

    def test_returns_zero_when_nothing_to_do(self):
        with patch(_SYNC_EXEC, return_value=[]):
            created = populate_seeder_tasks()
        assert created == 0

    def test_deduplicates_duplicate_persona_rows(self):
        select_calls = 0
        inserts: list[tuple] = []

        def exec_side_effect(query, params=None):
            nonlocal select_calls
            if query.lstrip().startswith("SELECT"):
                select_calls += 1
                if select_calls == 1:
                    return []
                if select_calls == 2:
                    return [{"persona_id": "p1"}, {"persona_id": "p1"}]
                return []
            if "INSERT INTO seeder_tasks" in query:
                inserts.append(params)
            return []

        with patch(_SYNC_EXEC, side_effect=exec_side_effect):
            created = populate_seeder_tasks()

        assert created == 1
        assert inserts == [("p1", "tiktok")]


# --- claim_seeder_task ---


class TestClaimSeederTask:
    def test_claims_pending_task(self):
        """Claiming a task should return a dict with persona info."""
        task_row = {
            "id": "t1",
            "persona_id": "p1",
            "platform": "tiktok",
            "task_type": "create_email",
            "first_name": "Jane",
            "last_name": "Doe",
            "display_name": "janedoe",
            "username_base": "janedoe",
            "gender": "female",
            "date_of_birth": "1998-05-20",
            "age": 27,
            "niche_id": "n1",
            "bio_short": "Bio",
            "occupation": "trainer",
            "interests": ["fitness"],
        }

        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = task_row
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch(_SYNC_CONN, return_value=mock_conn):
            result = claim_seeder_task("dev-1")

        assert result is not None
        assert isinstance(result, dict)
        assert result["id"] == "t1"
        assert result["task_type"] == "create_email"
        assert result["first_name"] == "Jane"

    def test_returns_none_when_no_tasks(self):
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = None
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch(_SYNC_CONN, return_value=mock_conn):
            result = claim_seeder_task("dev-1")

        assert result is None

    def test_claim_updates_status(self):
        """Claiming should UPDATE the task to 'claimed' status."""
        task_row = {
            "id": "t1",
            "persona_id": "p1",
            "platform": "tiktok",
            "task_type": "create_email",
            "first_name": "A",
            "last_name": "B",
            "display_name": "ab",
            "username_base": "ab",
            "gender": "male",
            "date_of_birth": "2000-01-01",
            "age": 25,
            "niche_id": "n1",
            "bio_short": "",
            "occupation": "",
            "interests": [],
        }

        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = task_row
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch(_SYNC_CONN, return_value=mock_conn):
            claim_seeder_task("dev-1")

        # The second execute call should be the UPDATE for claiming
        update_call = mock_cur.execute.call_args_list[1]
        update_query = update_call[0][0]
        assert "claimed" in update_query
        assert "attempts" in update_query


# --- complete_seeder_task / fail_seeder_task ---


class TestSeederTaskLifecycle:
    def test_complete_task(self):
        with patch(_SYNC_EXEC) as mock_exec:
            complete_seeder_task("t1", result_id="result-abc")
        query = mock_exec.call_args[0][0]
        assert "completed" in query
        params = mock_exec.call_args[0][1]
        assert "result-abc" in params

    def test_fail_task_retryable(self):
        """When attempts < max_attempts, task resets to pending."""
        with (
            patch(_SYNC_EXEC_ONE, return_value={"attempts": 1, "max_attempts": 3}),
            patch(_SYNC_EXEC) as mock_exec,
        ):
            fail_seeder_task("t1", "some error")
        query = mock_exec.call_args[0][0]
        assert "claimed_by = NULL" in query
        params = mock_exec.call_args[0][1]
        assert "pending" in params  # reset for retry

    def test_fail_task_permanently(self):
        """When attempts >= max_attempts, task is permanently failed."""
        with (
            patch(_SYNC_EXEC_ONE, return_value={"attempts": 3, "max_attempts": 3}),
            patch(_SYNC_EXEC) as mock_exec,
        ):
            fail_seeder_task("t1", "repeated failure")
        params = mock_exec.call_args[0][1]
        assert "failed" in params

    def test_recovers_interrupted_tasks(self):
        interrupted_rows = [
            {"id": "t1", "attempts": 1, "max_attempts": 3},
            {"id": "t2", "attempts": 3, "max_attempts": 3},
        ]

        with (
            patch(_SYNC_EXEC, side_effect=[interrupted_rows, None, None]) as mock_exec,
            patch(_EVENTS_EMIT) as mock_emit,
        ):
            recovered = recover_interrupted_seeder_tasks()

        assert recovered == 2
        assert mock_exec.call_args_list[1][0][1][0] == "pending"
        assert mock_exec.call_args_list[2][0][1][0] == "failed"
        mock_emit.assert_called_once()

    def test_dedupes_open_tasks(self):
        open_rows = [
            {"id": "t1", "persona_id": "p1", "platform": "instagram", "task_type": "create_account"},
            {"id": "t2", "persona_id": "p1", "platform": "instagram", "task_type": "create_account"},
            {"id": "t3", "persona_id": "p2", "platform": "reddit", "task_type": "create_account"},
        ]

        with (
            patch(_SYNC_EXEC, side_effect=[open_rows, None]) as mock_exec,
            patch(_EVENTS_EMIT) as mock_emit,
        ):
            cancelled = dedupe_open_seeder_tasks()

        assert cancelled == 1
        assert mock_exec.call_args_list[1][0][1] == ("Cancelled duplicate seeder task", "t2")
        mock_emit.assert_called_once()


# --- get_warmer_with_fewest_bindings ---


class TestGetWarmerWithFewestBindings:
    def test_returns_device_id(self):
        with patch(_SYNC_EXEC_ONE, return_value={"id": "dev-3"}):
            result = get_warmer_with_fewest_bindings()
        assert result == "dev-3"

    def test_returns_none_when_no_warmers(self):
        with patch(_SYNC_EXEC_ONE, return_value=None):
            result = get_warmer_with_fewest_bindings()
        assert result is None
