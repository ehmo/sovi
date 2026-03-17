"""Tests for scheduler runtime ownership and conflict detection."""

from __future__ import annotations

from unittest.mock import ANY, MagicMock, patch

from sovi.device.runtime_guard import (
    SchedulerInstanceLock,
    find_conflicting_scheduler_processes,
)


def _make_conn(acquired: bool) -> MagicMock:
    cur = MagicMock()
    cur.fetchone.return_value = {"acquired": acquired}
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    return conn


def test_find_conflicting_scheduler_processes_detects_legacy_wrapper():
    ps = MagicMock(
        returncode=0,
        stdout=(
            "  101   1 /opt/homebrew/bin/python /tmp/run_scheduler.py\n"
            "  202   1 /Users/test/.venv/bin/sovi scheduler start\n"
            "  303   1 /usr/bin/python other.py\n"
        ),
    )

    with patch("sovi.device.runtime_guard.subprocess.run", return_value=ps):
        conflicts = find_conflicting_scheduler_processes(current_pid=202)

    assert len(conflicts) == 1
    assert conflicts[0].pid == 101
    assert conflicts[0].kind == "legacy_wrapper"


def test_scheduler_instance_lock_acquires_and_releases():
    conn = _make_conn(acquired=True)

    with patch("sovi.device.runtime_guard.sync_conn", return_value=conn):
        lock = SchedulerInstanceLock()
        assert lock.acquire() is True
        assert lock.held is True
        lock.release()

    cur = conn.cursor.return_value.__enter__.return_value
    assert cur.execute.call_args_list[0].args == ("SELECT pg_try_advisory_lock(%s) AS acquired", (ANY,))
    assert cur.execute.call_args_list[1].args == ("SELECT pg_advisory_unlock(%s)", (ANY,))
    conn.close.assert_called_once_with()


def test_scheduler_instance_lock_returns_false_when_already_held_elsewhere():
    conn = _make_conn(acquired=False)

    with patch("sovi.device.runtime_guard.sync_conn", return_value=conn):
        lock = SchedulerInstanceLock()
        assert lock.acquire() is False
        assert lock.held is False

    conn.close.assert_called_once_with()
