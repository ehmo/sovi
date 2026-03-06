"""Shared test fixtures for SOVI tests.

Provides mock DB fixtures so tests can run without PostgreSQL.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_db():
    """Patch sovi.db sync helpers to return canned data.

    Usage:
        def test_something(mock_db):
            mock_db.execute.return_value = [{"id": 1, "name": "test"}]
            mock_db.execute_one.return_value = {"id": 1}
            # ... call code that uses sync_execute / sync_execute_one
    """
    mock = MagicMock()
    mock.execute.return_value = []
    mock.execute_one.return_value = None

    with (
        patch("sovi.db.sync_execute", side_effect=mock.execute),
        patch("sovi.db.sync_execute_one", side_effect=mock.execute_one),
    ):
        yield mock


@pytest.fixture
def mock_async_db():
    """Patch sovi.db async helpers for async tests.

    Usage:
        async def test_something(mock_async_db):
            mock_async_db.execute.return_value = [{"cnt": 5}]
            # ... call code that uses execute / execute_one
    """
    from unittest.mock import AsyncMock

    mock = MagicMock()
    mock.execute = AsyncMock(return_value=[])
    mock.execute_one = AsyncMock(return_value=None)

    with (
        patch("sovi.db.execute", side_effect=mock.execute),
        patch("sovi.db.execute_one", side_effect=mock.execute_one),
    ):
        yield mock
