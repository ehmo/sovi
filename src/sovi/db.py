"""Database connection pool and helpers."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import psycopg
import psycopg.rows
import psycopg_pool

from sovi.config import settings

_pool: psycopg_pool.AsyncConnectionPool | None = None


async def init_pool(min_size: int = 2, max_size: int = 10) -> psycopg_pool.AsyncConnectionPool:
    """Create and open the async connection pool."""
    global _pool
    if _pool is not None:
        return _pool
    _pool = psycopg_pool.AsyncConnectionPool(
        conninfo=settings.database_url,
        min_size=min_size,
        max_size=max_size,
        kwargs={"row_factory": psycopg.rows.dict_row},
        open=False,
    )
    await _pool.open()
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@contextlib.asynccontextmanager
async def get_conn() -> AsyncIterator[psycopg.AsyncConnection[dict[str, Any]]]:
    """Borrow a connection from the pool."""
    if _pool is None:
        raise RuntimeError("Database pool not initialized. Call init_pool() first.")
    async with _pool.connection() as conn:
        yield conn


async def execute(query: str, params: tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
    """Execute a query and return all rows as dicts."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, params)
            if cur.description is None:
                return []
            return await cur.fetchall()


async def execute_one(query: str, params: tuple[Any, ...] | None = None) -> dict[str, Any] | None:
    """Execute a query and return a single row."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, params)
            if cur.description is None:
                return None
            return await cur.fetchone()


# ---------------------------------------------------------------------------
# Synchronous helpers (for warming and other threaded code)
# ---------------------------------------------------------------------------

def sync_conn() -> psycopg.Connection[dict[str, Any]]:
    """Open a synchronous connection using project settings."""
    return psycopg.connect(settings.database_url, row_factory=psycopg.rows.dict_row)


def sync_execute(query: str, params: tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
    """Execute query synchronously — opens and closes connection per call."""
    with sync_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            conn.commit()
            if cur.description is None:
                return []
            return cur.fetchall()


def sync_execute_one(query: str, params: tuple[Any, ...] | None = None) -> dict[str, Any] | None:
    """Execute query synchronously and return one row."""
    with sync_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            conn.commit()
            if cur.description is None:
                return None
            return cur.fetchone()
