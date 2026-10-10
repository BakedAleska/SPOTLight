"""Async Postgres access (psycopg 3 + psycopg_pool).

`Database` wraps one pool. It opens without waiting, so the API starts even when Neon (or the local pgvector
container) is down; queries then raise `DatabaseUnavailable`, which the app maps to a 503 (never a 500).
It also introspects which columns exist, so the backend can use additive columns (e.g. `flags.priority`,
`raw.spot_event_log.request_id`) when the migration has them and skip them when it doesn't.

Neon's pooled endpoint is PgBouncer in transaction mode: no prepared statements (`prepare_threshold=None`), no
session state (autocommit, no SET/LISTEN/advisory locks/temp tables; multi-statement work uses `transaction()`).
Serverless (Vercel): `min_size=0` and `check=True` (a connection idle across a frozen instance is tested on checkout
and replaced, instead of failing the request and tripping the breaker).
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence, Set, Tuple

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout

log = logging.getLogger("backend.db")


class DatabaseUnavailable(RuntimeError):
    """The database is not configured or not reachable."""


class Database:
    def __init__(self, url: Optional[str], *, name: str = "main", pool_max: int = 10,
                 connect_timeout_s: float = 3.0, pool_min: int = 1, check: bool = False):
        self.url = url
        self.name = name
        self.pool_max = pool_max
        self.pool_min = max(0, min(pool_min, pool_max))
        self.check = check
        self.connect_timeout_s = connect_timeout_s
        self.pool: Optional[AsyncConnectionPool] = None
        self._columns: Dict[Tuple[str, str], Set[str]] = {}
        self._columns_at = 0.0
        # Breaker: after a connect failure, fail fast for `cooldown_s` instead of waiting the pool timeout on
        # every query (a dead Neon must not stall the live replay).
        self.cooldown_s = 5.0
        self._down_until = 0.0
        self.last_error: Optional[str] = None

    @property
    def configured(self) -> bool:
        return bool(self.url)

    async def open(self) -> None:
        if not self.url or self.pool is not None:
            return
        # prepare_threshold=None: Neon's pooled endpoint (PgBouncer, transaction mode) rejects prepared statements.
        self.pool = AsyncConnectionPool(
            conninfo=self.url, min_size=self.pool_min, max_size=self.pool_max, open=False,
            timeout=self.connect_timeout_s,
            kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": None,
                    "connect_timeout": max(1, int(self.connect_timeout_s))},
            check=AsyncConnectionPool.check_connection if self.check else None,
            name="backend-%s" % self.name, reconnect_timeout=0)
        await self.pool.open(wait=False)

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[psycopg.AsyncConnection]:
        if self.pool is None:
            raise DatabaseUnavailable("%s database is not configured (set DATABASE_URL)" % self.name)
        if time.monotonic() < self._down_until:
            raise DatabaseUnavailable("%s database is not reachable (%s; retrying shortly)"
                                      % (self.name, self.last_error))
        try:
            async with self.pool.connection(timeout=self.connect_timeout_s) as conn:
                yield conn
        except PoolTimeout as exc:
            self._trip("pool timeout")
            raise DatabaseUnavailable("%s database is not reachable" % self.name) from exc
        except psycopg.OperationalError as exc:
            self._trip(str(exc).strip()[:200])
            raise DatabaseUnavailable("%s database error: %s" % (self.name, exc)) from exc
        else:
            self._down_until = 0.0

    def _trip(self, error: str) -> None:
        self.last_error = error
        self._down_until = time.monotonic() + self.cooldown_s

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[psycopg.AsyncCursor]:
        async with self.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    yield cur

    async def fetch(self, sql: str, params: Sequence[Any] | Dict[str, Any] = ()) -> List[Dict[str, Any]]:
        async with self.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                if cur.description is None:
                    return []
                return list(await cur.fetchall())

    async def fetchone(self, sql: str, params: Sequence[Any] | Dict[str, Any] = ()) -> Optional[Dict[str, Any]]:
        rows = await self.fetch(sql, params)
        return rows[0] if rows else None

    async def execute(self, sql: str, params: Sequence[Any] | Dict[str, Any] = ()) -> int:
        async with self.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                return cur.rowcount

    async def ping(self) -> bool:
        try:
            await self.fetch("SELECT 1 AS ok")
            return True
        except Exception:  # noqa: BLE001 - any failure means "down"
            return False

    # ── schema introspection ────────────────────────────────────────────────────────────────────
    async def columns(self, schema: str, table: str, ttl_s: float = 60.0) -> Set[str]:
        """Column names of schema.table (empty set if the table or the DB is missing). Cached for ttl_s."""
        now = time.monotonic()
        if not self._columns or now - self._columns_at > ttl_s:
            try:
                rows = await self.fetch(
                    "SELECT table_schema, table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema IN ('public', 'raw', 'app')")
            except DatabaseUnavailable:
                return set()
            cols: Dict[Tuple[str, str], Set[str]] = {}
            for r in rows:
                cols.setdefault((r["table_schema"], r["table_name"]), set()).add(r["column_name"])
            self._columns, self._columns_at = cols, now
        return self._columns.get((schema, table), set())

    def invalidate_columns(self) -> None:
        self._columns, self._columns_at = {}, 0.0
