"""Postgres access: a read-only data pool and an insert-only log sink (psycopg 3, async).

Every data query runs inside `BEGIN; SET TRANSACTION READ ONLY; set_config('statement_timeout', ...)`, which works on
Neon's pooled (PgBouncer) endpoint as well as the direct one. Prepared statements are off for the same reason.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Protocol, Sequence

log = logging.getLogger(__name__)


class Database(Protocol):
    async def fetch(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]: ...
    async def close(self) -> None: ...


class PgDatabase:
    """Small lazily-opened async pool; every query is a read-only transaction."""

    def __init__(self, url: str, max_size: int = 4, statement_timeout_ms: int = 5000):
        self._url = url
        self._max = max_size
        self._timeout = str(int(statement_timeout_ms))
        self._pool = None
        self._lock = asyncio.Lock()

    async def _get_pool(self):
        if self._pool is None:
            async with self._lock:
                if self._pool is None:
                    from psycopg.rows import dict_row
                    from psycopg_pool import AsyncConnectionPool

                    pool = AsyncConnectionPool(
                        self._url, min_size=1, max_size=self._max, open=False, timeout=15,
                        kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": dict_row,
                                "application_name": "spot-mcp"},
                        check=AsyncConnectionPool.check_connection,  # Neon scales to zero; drop dead conns
                    )
                    await pool.open(wait=True, timeout=20)
                    self._pool = pool
        return self._pool

    async def fetch(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("SET TRANSACTION READ ONLY")
                await conn.execute("SELECT set_config('statement_timeout', %s, true)", (self._timeout,))
                cur = await conn.execute(sql, params)
                return list(await cur.fetchall())

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


# ── logging (app.* schema, INSERT only, never breaks a tool call) ───────────────────────────────
class LogSink(Protocol):
    def tool_call(self, row: Dict[str, Any]) -> None: ...
    def model_call(self, row: Dict[str, Any]) -> None: ...
    async def drain(self, timeout_s: Optional[float] = None) -> None: ...
    async def close(self) -> None: ...


class NullLogSink:
    def tool_call(self, row: Dict[str, Any]) -> None:
        pass

    def model_call(self, row: Dict[str, Any]) -> None:
        pass

    async def drain(self, timeout_s: Optional[float] = None) -> None:
        pass

    async def close(self) -> None:
        pass


_TOOL_COLS = ("request_id", "flag_id", "tool", "args", "result_count", "result_bytes", "top_similarity",
              "latency_ms", "ok", "error")
_MODEL_COLS = ("request_id", "system", "model", "endpoint", "flag_id", "action_id", "input_tokens",
               "output_tokens", "latency_ms", "tokens_per_sec", "device", "ok", "error", "metadata")


class PgLogSink:
    """Fire-and-forget INSERTs into app.mcp_tool_calls / app.model_calls on its own connection pool.
    `drain()` waits for the pending ones (shutdown, tests, and after each request on serverless)."""

    def __init__(self, url: str):
        self._url = url
        self._pool = None
        self._lock = asyncio.Lock()
        self._tasks: set = set()
        self._warned = 0

    def tool_call(self, row: Dict[str, Any]) -> None:
        self._spawn("app.mcp_tool_calls", _TOOL_COLS, row)

    def model_call(self, row: Dict[str, Any]) -> None:
        self._spawn("app.model_calls", _MODEL_COLS, row)

    def _spawn(self, table: str, cols, row: Dict[str, Any]) -> None:
        try:
            task = asyncio.get_running_loop().create_task(self._insert(table, cols, row))
        except RuntimeError:
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _insert(self, table: str, cols, row: Dict[str, Any]) -> None:
        try:
            if self._pool is None:
                async with self._lock:
                    if self._pool is None:
                        from psycopg_pool import AsyncConnectionPool
                        pool = AsyncConnectionPool(self._url, min_size=0, max_size=2, open=False, timeout=10,
                                                   kwargs={"autocommit": True, "prepare_threshold": None,
                                                           "application_name": "spot-mcp-logs"})
                        await pool.open(wait=False)
                        self._pool = pool
            vals = []
            for c in cols:
                v = row.get(c)
                if c in ("args", "metadata"):
                    v = json.dumps(v if v is not None else {}, default=str)
                vals.append(v)
            # Table and column names are constants above; values are bound parameters.
            sql = "INSERT INTO %s (%s) VALUES (%s)" % (table, ", ".join(cols), ", ".join(["%s"] * len(cols)))
            async with self._pool.connection() as conn:
                await conn.execute(sql, vals)
        except Exception as exc:  # noqa: BLE001 - logging must never break a tool call
            self._warned += 1
            if self._warned <= 3 or self._warned % 100 == 0:
                log.warning("log insert into %s failed (%d so far): %s", table, self._warned, exc)

    async def drain(self, timeout_s: Optional[float] = None) -> None:
        """Wait for pending inserts; with `timeout_s`, at most that long (the rest keep running). On serverless
        (asgi.py) this runs before each response completes: a frozen instance would hold the inserts or lose them."""
        if timeout_s is not None:
            if self._tasks:
                await asyncio.wait(list(self._tasks), timeout=timeout_s)
            return
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def close(self) -> None:
        await self.drain()
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
