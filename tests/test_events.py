"""Tests for the events module — parameter building, SQL, sync/async API."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from sovi.events import (
    _EVENT_COLUMNS,
    _INSERT_EVENT,
    _RESOLVE_EVENT,
    _emit_params,
    _unresolved_query,
)

_SYNC_EXEC = "sovi.events.sync_execute"
_ASYNC_EXEC = "sovi.events.execute"


@pytest.fixture
def mock_events_sync():
    mock = MagicMock()
    mock.return_value = []
    with patch(_SYNC_EXEC, side_effect=mock) as p:
        yield mock


@pytest.fixture
def mock_events_async():
    from unittest.mock import AsyncMock
    mock = AsyncMock(return_value=[])
    with patch(_ASYNC_EXEC, side_effect=mock) as p:
        yield mock


# --- Pure function tests (no DB) ---


def test_emit_params_basic():
    params = _emit_params("scheduler", "info", "warming_started", "msg", None, None, None)
    assert params == ("scheduler", "info", "warming_started", "msg", None, None, "{}")


def test_emit_params_with_ids():
    did = uuid4()
    aid = uuid4()
    params = _emit_params("device", "error", "crash", "boom", did, aid, {"key": "val"})
    assert params[4] == str(did)
    assert params[5] == str(aid)
    assert '"key"' in params[6]


def test_emit_params_context_none_becomes_empty_json():
    params = _emit_params("x", "y", "z", "m", None, None, None)
    assert params[6] == "{}"


def test_unresolved_query_no_filters():
    sql, params = _unresolved_query(None, None, 50)
    assert "resolved = false" in sql
    assert params == (50,)


def test_unresolved_query_with_severity():
    sql, params = _unresolved_query("error", None, 10)
    assert "severity = %s" in sql
    assert params == ("error", 10)


def test_unresolved_query_with_both_filters():
    sql, params = _unresolved_query("critical", "device", 25)
    assert "severity = %s" in sql
    assert "category = %s" in sql
    assert params == ("critical", "device", 25)


def test_event_columns_include_resolved_fields():
    assert "resolved" in _EVENT_COLUMNS
    assert "resolved_by" in _EVENT_COLUMNS
    assert "resolved_at" in _EVENT_COLUMNS


def test_sql_constants_are_valid():
    assert "INSERT INTO system_events" in _INSERT_EVENT
    assert "RETURNING id" in _INSERT_EVENT
    assert "UPDATE system_events" in _RESOLVE_EVENT
    assert "resolved = true" in _RESOLVE_EVENT


# --- Sync API ---


class TestSyncEmit:
    def test_emit_returns_event_id(self, mock_events_sync):
        mock_events_sync.return_value = [{"id": 42}]
        from sovi.events import emit
        result = emit("scheduler", "info", "warming_started", "Starting warming")
        assert result == 42

    def test_emit_returns_none_on_empty(self, mock_events_sync):
        mock_events_sync.return_value = []
        from sovi.events import emit
        result = emit("scheduler", "info", "test", "msg")
        assert result is None

    def test_emit_returns_none_on_exception(self, mock_events_sync):
        mock_events_sync.side_effect = Exception("DB error")
        from sovi.events import emit
        result = emit("scheduler", "error", "crash", "boom")
        assert result is None

    def test_emit_passes_string_ids(self, mock_events_sync):
        mock_events_sync.return_value = [{"id": 1}]
        from sovi.events import emit
        did = uuid4()
        aid = uuid4()
        emit("device", "info", "test", "msg", device_id=did, account_id=aid,
             context={"key": "val"})
        call_args = mock_events_sync.call_args[0]
        # params tuple is the second positional arg
        params = call_args[1]
        assert str(did) in params
        assert str(aid) in params


class TestSyncGetUnresolved:
    def test_returns_rows(self, mock_events_sync):
        mock_events_sync.return_value = [{"id": 1, "severity": "error"}]
        from sovi.events import get_unresolved
        result = get_unresolved(severity="error")
        assert len(result) == 1


class TestSyncResolve:
    def test_resolve_returns_true(self, mock_events_sync):
        from sovi.events import resolve
        result = resolve(42, resolved_by="admin")
        assert result is True

    def test_resolve_returns_false_on_error(self, mock_events_sync):
        mock_events_sync.side_effect = Exception("DB error")
        from sovi.events import resolve
        result = resolve(999)
        assert result is False


# --- Async API ---


class TestAsyncEmit:
    @pytest.mark.asyncio
    async def test_async_emit_returns_id(self, mock_events_async):
        mock_events_async.return_value = [{"id": 99}]
        from sovi.events import async_emit
        result = await async_emit("dashboard", "info", "view", "Page loaded")
        assert result == 99

    @pytest.mark.asyncio
    async def test_async_emit_returns_none_on_error(self, mock_events_async):
        mock_events_async.side_effect = Exception("pool error")
        from sovi.events import async_emit
        result = await async_emit("x", "y", "z", "m")
        assert result is None


class TestAsyncGetUnresolved:
    @pytest.mark.asyncio
    async def test_returns_rows(self, mock_events_async):
        mock_events_async.return_value = [{"id": 1}]
        from sovi.events import async_get_unresolved
        result = await async_get_unresolved()
        assert result == [{"id": 1}]


class TestAsyncResolve:
    @pytest.mark.asyncio
    async def test_resolve_returns_true(self, mock_events_async):
        from sovi.events import async_resolve
        result = await async_resolve(1)
        assert result is True

    @pytest.mark.asyncio
    async def test_resolve_returns_false_on_error(self, mock_events_async):
        mock_events_async.side_effect = Exception("err")
        from sovi.events import async_resolve
        result = await async_resolve(1)
        assert result is False


class TestAsyncGetEvents:
    @pytest.mark.asyncio
    async def test_no_filters(self, mock_events_async):
        mock_events_async.return_value = []
        from sovi.events import async_get_events
        result = await async_get_events()
        assert result == []

    @pytest.mark.asyncio
    async def test_all_filters(self, mock_events_async):
        mock_events_async.return_value = [{"id": 5}]
        from sovi.events import async_get_events
        result = await async_get_events(
            severity="error", category="device", event_type="crash",
            device_id="d1", account_id="a1", resolved=False,
            limit=10, after_id=3,
        )
        assert result == [{"id": 5}]
