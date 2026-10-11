"""Replay the kit's replay.ndjson through the ingest pipeline.

Two modes (REPLAY_MODE; docs/backend-services-contract.md "Serverless deployment and stepped replay"):
- `background` (Docker): `ReplayManager`, an asyncio task in this process.
- `stepped` (Vercel): `SteppedReplay`, the run lives in the `replay_runs` table and each `POST /replay/step`
  ingests the events that are due within a time budget, so no process has to outlive a request.

Event gaps are the robot's own timestamps divided by `speed` (60 = one minute of mission time per second),
capped at `max_gap_s` so the hours between missions don't stall the demo. speed=0 means "as fast as possible".
Each event gets its own request_id (`rp<run>-<event_id>`) and an app.request_log row, like an HTTP ingest.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .reference import parse_ts

log = logging.getLogger("backend.replay")
# The streamed replay is its own source; `db/setup.py seed` imports the file itself as 'replay.ndjson'.
SOURCE = "replay"


COUNT_KEYS = ("total", "sent", "escalated", "flags_created", "laya_unavailable", "skipped", "errors")


class ReplayConflict(RuntimeError):
    """A replay is already running (409)."""


def load_replay(path: Path) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def select_events(events: List[Dict[str, Any]], missions: Optional[List[str]], start_at: Optional[str],
                  limit: Optional[int]) -> List[Dict[str, Any]]:
    if missions:
        want = set(missions)
        events = [e for e in events if e.get("mission_id") in want]
    if start_at:
        idx = next((i for i, e in enumerate(events) if e.get("event_id") == start_at), None)
        if idx is None:
            raise ValueError("start_at event %r not in the replay" % start_at)
        events = events[idx:]
    if limit:
        events = events[:limit]
    return events


def paced_offsets(events: List[Dict[str, Any]], speed: float, max_gap_s: float) -> List[float]:
    """Wall-clock offset of each event from the replay's start: the background mode's sleeps, summed."""
    out, total, prev_ts = [], 0.0, None
    for i, ev in enumerate(events):
        ts = parse_ts(ev.get("timestamp"))
        if i and speed > 0 and ts and prev_ts:
            total += min(max(0.0, (ts - prev_ts).total_seconds()) / speed, max_gap_s)
        prev_ts = ts or prev_ts
        out.append(total)
    return out


async def ingest_replay_event(pipeline, sink, ev: Dict[str, Any], rid: str, mode: str, st: Dict[str, Any]) -> None:
    """Run one replay event through the pipeline and update the counters in `st` (both modes). Never raises:
    one bad event must not end the replay."""
    t0 = time.perf_counter()
    try:
        res = await pipeline.ingest(ev, source=SOURCE, request_id=rid, mode=mode)
    except Exception as exc:  # noqa: BLE001
        log.exception("replay event %s failed", ev.get("event_id"))
        st["errors"] += 1
        sink.request(request_id=rid, route="replay", method="REPLAY", status=500,
                     latency_ms=int((time.perf_counter() - t0) * 1000), error=str(exc))
        return
    sink.request(request_id=rid, route="replay", method="REPLAY", status=200,
                 latency_ms=int((time.perf_counter() - t0) * 1000),
                 error="; ".join(res.get("warnings") or []) or None)
    st["sent"] += 1
    if res.get("skipped"):
        st["skipped"] += 1
    if (res.get("decision") or {}).get("escalated"):
        st["escalated"] += 1
    if (res.get("flag") or {}).get("created"):
        st["flags_created"] += 1
    if (res.get("system1") or {}).get("status") == "laya_unavailable":
        st["laya_unavailable"] += 1
    st["last_event"] = {"event_id": ev.get("event_id"), "event_type": ev.get("event_type"),
                        "mission_id": ev.get("mission_id"), "timestamp": ev.get("timestamp"),
                        "flag_id": (res.get("flag") or {}).get("flag_id")}


class ReplayManager:
    def __init__(self, pipeline, sink, replay_path: Path, default_speed: float = 60.0, default_max_gap_s: float = 2.0):
        self.pipeline = pipeline
        self.sink = sink
        self.replay_path = replay_path
        self.default_speed = default_speed
        self.default_max_gap_s = default_max_gap_s
        self.task: Optional[asyncio.Task] = None
        self.run_seq = 0
        self._status: Dict[str, Any] = {"state": "idle", "mode": "background"}

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def status(self) -> Dict[str, Any]:
        s = dict(self._status)
        if s.get("state") == "running" and s.get("started_at"):
            s["elapsed_s"] = round(time.time() - s["_t0"], 1)
        s.pop("_t0", None)
        return s

    def select(self, events: List[Dict[str, Any]], missions: Optional[List[str]], start_at: Optional[str],
               limit: Optional[int]) -> List[Dict[str, Any]]:
        return select_events(events, missions, start_at, limit)

    async def is_running(self) -> bool:
        return self.running

    async def get_status(self) -> Dict[str, Any]:
        return self.status()

    async def start(self, *, speed: Optional[float] = None, max_gap_s: Optional[float] = None,
                    missions: Optional[List[str]] = None, start_at: Optional[str] = None, limit: Optional[int] = None,
                    triage_mode: Optional[str] = None) -> Dict[str, Any]:
        if self.running:
            raise ReplayConflict("a replay is already running; stop it first")
        events = self.select(load_replay(self.replay_path), missions, start_at, limit)
        if not events:
            raise ValueError("no replay events match the selection")
        self.run_seq += 1
        speed = self.default_speed if speed is None else speed
        max_gap_s = self.default_max_gap_s if max_gap_s is None else max_gap_s
        mode = triage_mode or self.pipeline.default_mode
        run_id = await self.sink.job_start("replay", {"speed": speed, "max_gap_s": max_gap_s, "missions": missions,
                                                      "start_at": start_at, "limit": limit, "triage_mode": mode,
                                                      "events": len(events)})
        self._status = {"state": "running", "mode": "background", "run": self.run_seq, "job_run_id": run_id,
                        "speed": speed, "max_gap_s": max_gap_s, "triage_mode": mode, "total": len(events), "sent": 0,
                        "escalated": 0, "flags_created": 0, "laya_unavailable": 0, "skipped": 0, "errors": 0,
                        "last_event": None, "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "finished_at": None, "error": None, "_t0": time.time()}
        self.task = asyncio.create_task(self._run(events, speed, max_gap_s, mode, run_id))
        return self.status()

    async def shutdown(self) -> None:
        """App shutdown: the task dies with this process."""
        await self.stop()

    async def stop(self) -> Dict[str, Any]:
        if self.running:
            self._status["state"] = "stopping"
            self.task.cancel()  # type: ignore[union-attr]
            try:
                await self.task  # type: ignore[misc]
            except asyncio.CancelledError:
                pass
        return self.status()

    async def _run(self, events: List[Dict[str, Any]], speed: float, max_gap_s: float, mode: str,
                   run_id: Optional[int]) -> None:
        st = self._status
        prev_ts = None
        state, error = "finished", None
        try:
            for i, ev in enumerate(events):
                ts = parse_ts(ev.get("timestamp"))
                if i and speed > 0 and ts and prev_ts:
                    gap = max(0.0, (ts - prev_ts).total_seconds()) / speed
                    await asyncio.sleep(min(gap, max_gap_s))
                prev_ts = ts or prev_ts
                rid = "rp%d-%s" % (self.run_seq, ev.get("event_id"))
                await ingest_replay_event(self.pipeline, self.sink, ev, rid, mode, st)
                if st["sent"] % 25 == 0:
                    await self.sink.job_progress(run_id, self._counts())
        except asyncio.CancelledError:
            state = "stopped"
            raise
        except Exception as exc:  # noqa: BLE001
            state, error = "error", str(exc)
            log.exception("replay failed")
        finally:
            st["state"] = state
            st["error"] = error
            st["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            await asyncio.shield(self.sink.job_finish(run_id, state, self._counts(), error))

    def _counts(self) -> Dict[str, Any]:
        return {k: self._status.get(k) for k in COUNT_KEYS}


# A step's lease covers its budget plus this much for the event in flight (an event is bounded by
# SYSTEM1_INGEST_TIMEOUT_S plus a few DB writes); a killed or frozen step blocks the run at most this long after.
LEASE_GRACE_S = 30.0


class SteppedReplay:
    """REPLAY_MODE=stepped (serverless): the run lives in `replay_runs` (repo.replay_*), never in this process.

    `start` stores the selected event ids; each `step` takes the run's lease, ingests the events whose paced offset
    has passed (the same gaps as the background mode), writes the cursor and counts after every event, and returns.
    Any instance can serve any call. The bundled replay file is read once per process (it never changes).
    """

    running = False   # nothing runs in this process between requests

    def __init__(self, repo, pipeline, sink, replay_path: Path, default_speed: float = 60.0,
                 default_max_gap_s: float = 2.0, step_budget_s: float = 10.0):
        self.repo = repo
        self.pipeline = pipeline
        self.sink = sink
        self.replay_path = replay_path
        self.default_speed = default_speed
        self.default_max_gap_s = default_max_gap_s
        self.step_budget_s = step_budget_s
        self._by_id: Optional[Dict[str, Dict[str, Any]]] = None
        self._offsets: Tuple[Optional[int], List[float]] = (None, [])
        self._last: Dict[str, Any] = {"state": "idle", "mode": "stepped"}

    def status(self) -> Dict[str, Any]:
        """The last status this instance saw (no DB call; /health). GET /replay/status is authoritative."""
        return dict(self._last)

    def _events(self) -> Dict[str, Dict[str, Any]]:
        if self._by_id is None:
            self._by_id = {e.get("event_id"): e for e in load_replay(self.replay_path)}
        return self._by_id

    def _run_offsets(self, row: Dict[str, Any]) -> List[float]:
        if self._offsets[0] != row["run_id"]:
            by_id, p = self._events(), row["params"] or {}
            events = [by_id.get(eid) or {} for eid in row["event_ids"]]
            self._offsets = (row["run_id"], paced_offsets(events, float(p.get("speed") or 0),
                                                          float(p.get("max_gap_s") or 0)))
        return self._offsets[1]

    @staticmethod
    def _counts(row: Dict[str, Any]) -> Dict[str, Any]:
        c = row.get("counts") or {}
        out = {k: int(c.get(k) or 0) for k in COUNT_KEYS}
        out["total"] = len(row["event_ids"])
        return out

    def _view(self, row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if row is None:
            self._last = {"state": "idle", "mode": "stepped"}
            return dict(self._last)
        p = row["params"] or {}
        elapsed = max(0.0, (row["db_now"] - row["started_at"]).total_seconds())
        cursor, total = row["next_index"], len(row["event_ids"])
        next_due = None
        if row["state"] == "running" and cursor < total:
            next_due = round(max(0.0, self._run_offsets(row)[cursor] - elapsed), 2)
        out = {"state": row["state"], "mode": "stepped", "run": row["run_id"], "job_run_id": row.get("job_run_id"),
               "speed": p.get("speed"), "max_gap_s": p.get("max_gap_s"), "triage_mode": p.get("triage_mode"),
               **self._counts(row), "cursor": cursor, "next_due_in_s": next_due, "last_event": row.get("last_event"),
               "started_at": row["started_at"].isoformat(),
               "finished_at": row["finished_at"].isoformat() if row.get("finished_at") else None,
               "error": row.get("error")}
        if row["state"] == "running":
            out["elapsed_s"] = round(elapsed, 1)
        self._last = dict(out)
        return out

    async def is_running(self) -> bool:
        row = await self.repo.replay_latest()
        return bool(row and row["state"] == "running")

    async def get_status(self) -> Dict[str, Any]:
        return self._view(await self.repo.replay_latest())

    async def start(self, *, speed: Optional[float] = None, max_gap_s: Optional[float] = None,
                    missions: Optional[List[str]] = None, start_at: Optional[str] = None, limit: Optional[int] = None,
                    triage_mode: Optional[str] = None) -> Dict[str, Any]:
        events = select_events(list(self._events().values()), missions, start_at, limit)
        if not events:
            raise ValueError("no replay events match the selection")
        speed = self.default_speed if speed is None else speed
        max_gap_s = self.default_max_gap_s if max_gap_s is None else max_gap_s
        params = {"speed": speed, "max_gap_s": max_gap_s, "missions": missions, "start_at": start_at, "limit": limit,
                  "triage_mode": triage_mode or self.pipeline.default_mode}
        job = await self.sink.job_start("replay", {**params, "events": len(events), "mode": "stepped"})
        row = await self.repo.replay_create(params, [e.get("event_id") for e in events], job)
        if row is None:
            await self.sink.job_finish(job, "error", {}, "a replay is already running")
            raise ReplayConflict("a replay is already running; stop it first")
        return self._view(row)

    async def stop(self) -> Dict[str, Any]:
        row = await self.repo.replay_stop()
        if row is None:
            return await self.get_status()
        await self.sink.job_finish(row.get("job_run_id"), "stopped", self._counts(row))
        return self._view(row)

    async def shutdown(self) -> None:
        """App shutdown: the run is shared by every instance, so one instance going away must not stop it."""

    async def step(self, *, run_id: Optional[int] = None, budget_s: Optional[float] = None,
                   max_events: int = 100) -> Dict[str, Any]:
        budget = self.step_budget_s if budget_s is None else budget_s
        t0 = time.monotonic()
        token = uuid.uuid4().hex
        row = await self.repo.replay_claim(token, budget + LEASE_GRACE_S, run_id)
        if row is None:   # nothing running (or not this run_id), or another step holds the lease
            latest = await self.repo.replay_latest()
            busy = bool(latest and latest["state"] == "running" and run_id in (None, latest["run_id"]))
            out = self._view(latest)
            out["step"] = {"ingested": 0, "elapsed_ms": int((time.monotonic() - t0) * 1000), "busy": busy,
                           "done": not busy}
            return out
        rid0 = row["run_id"]
        ids, i = row["event_ids"], row["next_index"]
        p = row["params"] or {}
        mode = p.get("triage_mode") or self.pipeline.default_mode
        offsets = self._run_offsets(row)
        elapsed0 = (row["db_now"] - row["started_at"]).total_seconds()
        by_id = self._events()
        st: Dict[str, Any] = {**self._counts(row), "last_event": row.get("last_event")}
        n, lost = 0, False
        while i < len(ids) and n < max_events and time.monotonic() - t0 < budget:
            if offsets[i] > elapsed0 + (time.monotonic() - t0):
                break   # not due yet: the client calls again after next_due_in_s
            ev = by_id.get(ids[i])
            if ev is None:   # the deployed replay file changed under a running run
                st["errors"] += 1
            else:
                await ingest_replay_event(self.pipeline, self.sink, ev, "rp%d-%s" % (rid0, ids[i]), mode, st)
            i, n = i + 1, n + 1
            counts = {k: st[k] for k in COUNT_KEYS}
            if not await self.repo.replay_advance(rid0, token, i, counts, st.get("last_event")):
                lost = True   # stopped, reset or lease expired meanwhile: never write over the new state
                break
        done = i >= len(ids)
        if lost:
            final = await self.repo.replay_latest()
        else:
            final = await self.repo.replay_release(rid0, token, "finished" if done else None)
            if final is None:
                final = await self.repo.replay_latest()
            elif done:
                await self.sink.job_finish(row.get("job_run_id"), "finished", self._counts(final))
            elif n:
                await self.sink.job_progress(row.get("job_run_id"), self._counts(final))
        out = self._view(final)
        out["step"] = {"ingested": n, "elapsed_ms": int((time.monotonic() - t0) * 1000), "busy": False,
                       "done": not final or final["state"] != "running"}
        return out
