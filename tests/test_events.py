"""Tests for the events module — parameter building and SQL consistency."""

from __future__ import annotations

from uuid import uuid4

from sovi.events import (
    _EVENT_COLUMNS,
    _INSERT_EVENT,
    _RESOLVE_EVENT,
    _emit_params,
    _unresolved_query,
)


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
    """Ensure sync and async paths both get resolution columns."""
    assert "resolved" in _EVENT_COLUMNS
    assert "resolved_by" in _EVENT_COLUMNS
    assert "resolved_at" in _EVENT_COLUMNS


def test_sql_constants_are_valid():
    """Smoke test that SQL strings are well-formed (no unclosed quotes, etc.)."""
    assert "INSERT INTO system_events" in _INSERT_EVENT
    assert "RETURNING id" in _INSERT_EVENT
    assert "UPDATE system_events" in _RESOLVE_EVENT
    assert "resolved = true" in _RESOLVE_EVENT
