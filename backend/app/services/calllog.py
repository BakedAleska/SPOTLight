"""Append-only logging to the `app` schema (LOGS_DATABASE_URL): model calls, API requests, job runs.

Writes are fire-and-forget background tasks, so a slow or missing logs DB never slows or breaks a request.
`drain()` waits for pending writes (tests, shutdown, and on serverless before each response: a frozen instance
would otherwise hold them until its next request, or lose them).
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Any, Dict, Optional, Set

from psycopg.types.json import Jsonb

from ..db.pool import Database

log = logging.getLogger("backend.calllog")

# The request_id of the current request / ingested event (set by the middleware and the ingest pipeline).
request_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("request_id", default=None)

MODEL_CALL_COLS = ("request_id", "system", "model", "endpoint", "flag_id", "action_id", "input_tokens",
                   "output_tokens", "latency_ms", "tokens_per_sec", "device", "ok", "error", "metadata")


class LogSink:
    def __init__(self, db: Optional[Database], service: str = "backend"):
        self.db = db
        self.service = service
        self._tasks: Set[asyncio.Task] = set()

    @property
    def enabled(self) -> bool:
        return self.db is not None and self.db.configured

    def spawn(self, coro) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self, timeout_s: Optional[float] = None) -> None:
        """Wait for pending writes; with `timeout_s`, at most that long (the rest keep running)."""
        if timeout_s is not None:
            if self._tasks:
                await asyncio.wait(list(self._tasks), timeout=timeout_s)
            return
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _insert(self, sql: str, params: tuple) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        try:
            return await self.db.fetchone(sql, params)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - logging must never break the caller
            log.debug("log write failed: %s", exc)
            return None

    # ── app.model_calls ─────────────────────────────────────────────────────────────────────────
    def model_call(self, *, system: str, ok: bool, **fields: Any) -> None:
        row = {c: fields.get(c) for c in MODEL_CALL_COLS}
        row.update(system=system, ok=ok, request_id=fields.get("request_id") or request_id_var.get())
        row["metadata"] = Jsonb({"caller": self.service, **(fields.get("metadata") or {})})
        if row["latency_ms"] is not None:
            row["latency_ms"] = int(row["latency_ms"])
        if row["error"]:
            row["error"] = str(row["error"])[:500]
        sql = "INSERT INTO app.model_calls (%s) VALUES (%s)" % (", ".join(MODEL_CALL_COLS),
                                                               ", ".join(["%s"] * len(MODEL_CALL_COLS)))
        self.spawn(self._insert(sql, tuple(row[c] for c in MODEL_CALL_COLS)))

    # ── app.request_log ─────────────────────────────────────────────────────────────────────────
    def request(self, *, request_id: Optional[str], route: str, method: str, status: int, latency_ms: int,
                error: Optional[str] = None) -> None:
        self.spawn(self._insert(
            "INSERT INTO app.request_log (request_id, service, route, method, status, latency_ms, error) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (request_id, self.service, route, method, status, latency_ms, (error or None) and error[:500])))

    # ── app.job_runs ────────────────────────────────────────────────────────────────────────────
    async def job_start(self, job: str, metadata: Optional[Dict[str, Any]] = None) -> Optional[int]:
        row = await self._insert("INSERT INTO app.job_runs (job, metadata) VALUES (%s, %s) RETURNING run_id",
                                 (job, Jsonb(metadata or {})))
        return row["run_id"] if row else None

    async def job_finish(self, run_id: Optional[int], status: str, counts: Dict[str, Any],
                         error: Optional[str] = None) -> None:
        if run_id is None:
            return
        await self._insert(
            "UPDATE app.job_runs SET finished_at = now(), status = %s, counts = %s, error = %s "
            "WHERE run_id = %s RETURNING run_id", (status, Jsonb(counts), error, run_id))

    async def job_progress(self, run_id: Optional[int], counts: Dict[str, Any]) -> None:
        if run_id is None:
            return
        await self._insert("UPDATE app.job_runs SET counts = %s WHERE run_id = %s RETURNING run_id",
                           (Jsonb(counts), run_id))
