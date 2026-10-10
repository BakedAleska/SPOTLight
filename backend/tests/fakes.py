"""In-memory stand-ins for the database (Repo) and the app.* log sink, for unit tests."""
from __future__ import annotations

import datetime as dt
import itertools
from typing import Any, Dict, List

from app.db.pool import DatabaseUnavailable
from app.services.calllog import LogSink


class RecordingSink(LogSink):
    """Captures model_call / request / job writes instead of inserting them."""

    def __init__(self):
        super().__init__(None)
        self.model_calls: List[Dict[str, Any]] = []
        self.requests: List[Dict[str, Any]] = []
        self.jobs: List[Dict[str, Any]] = []

    def model_call(self, *, system: str, ok: bool, **fields: Any) -> None:
        from app.services.calllog import request_id_var
        self.model_calls.append({"system": system, "ok": ok, **fields,
                                 "request_id": fields.get("request_id") or request_id_var.get()})

    def request(self, **kw: Any) -> None:
        self.requests.append(kw)

    async def job_start(self, job, metadata=None):
        self.jobs.append({"job": job, "metadata": metadata, "status": "running"})
        return len(self.jobs)

    async def job_finish(self, run_id, status, counts, error=None):
        if run_id:
            self.jobs[run_id - 1].update(status=status, counts=counts, error=error)

    async def job_progress(self, run_id, counts):
        pass


def _guard(fn):
    async def wrapper(self, *a, **kw):
        if self.down:
            raise DatabaseUnavailable("main database is not reachable")
        return await fn(self, *a, **kw)
    wrapper.__name__ = fn.__name__
    return wrapper


class FakeRepo:
    def __init__(self, ref=None):
        self.down = False
        self.ref = ref
        self.raw: List[Dict[str, Any]] = []
        self.events: Dict[str, Dict[str, Any]] = {}
        self.predictions_rows: List[Dict[str, Any]] = []
        self.flags: Dict[int, Dict[str, Any]] = {}
        self.acks: List[Dict[str, Any]] = []
        self.assessments: Dict[int, Dict[str, Any]] = {}
        self.documents: Dict[str, Dict[str, Any]] = {}
        self._ids = itertools.count(1)
        self._ack_ids = itertools.count(1)
        self._pred_ids = itertools.count(1)
        self.replay_runs: List[Dict[str, Any]] = []
        self.replay_advances = 0
        self.replay_clock_skew = dt.timedelta(0)    # tests move "now" forward to make paced events due

    # ingest
    @_guard
    async def append_raw(self, source, raw, sha256, ts, request_id):
        if any(r["source"] == source and r["sha256"] == sha256 for r in self.raw):
            return None
        self.raw.append({"log_id": len(self.raw) + 1, "source": source, "payload": raw, "sha256": sha256,
                         "request_id": request_id, "event_id": raw.get("event_id")})
        return len(self.raw)

    @_guard
    async def upsert_event(self, n):
        self.events[n["event_id"]] = {**n, "ingested_at": dt.datetime.now(dt.timezone.utc)}

    @_guard
    async def set_event_triage(self, event_id, triage):
        self.events[event_id]["payload"] = {**self.events[event_id]["payload"], "triage": triage}

    @_guard
    async def store_predictions(self, kind, action_id, mission_id, model, answers):
        self.predictions_rows = [p for p in self.predictions_rows if not (
            p["event_kind"] == kind and p["action_id"] == action_id and p["mission_id"] == mission_id
            and p["model"] == model and p["question"] in answers)]
        for q, a in answers.items():
            self.predictions_rows.append({"prediction_id": next(self._pred_ids), "event_kind": kind,
                                          "action_id": action_id, "mission_id": mission_id, "question": q,
                                          "answer": a["answer"], "probs": a.get("probs") or {},
                                          "confidence": a.get("confidence"), "model": model,
                                          "created_at": dt.datetime.now(dt.timezone.utc)})
        return len(answers)

    @_guard
    async def upsert_flag(self, f):
        for fid, row in self.flags.items():
            if row.get("triage_event_id") == f["triage_event_id"]:
                row.update({k: v for k, v in f.items()})
                return fid, False
        fid = next(self._ids)
        self.flags[fid] = {**f, "flag_id": fid, "status": "OPEN", "created_at": dt.datetime.now(dt.timezone.utc)}
        return fid, True

    @_guard
    async def unescalate_flag(self, triage_event_id, reasons):
        for fid, row in self.flags.items():
            if row.get("triage_event_id") == triage_event_id:
                row.update(escalated=False, reasons=reasons)
                return fid
        return None

    # flags
    @_guard
    async def get_flag(self, flag_id):
        f = self.flags.get(flag_id)
        if f is None:
            return None
        ev = self.events.get(f.get("event_id"))
        return {"flag": dict(f), "event": dict(ev) if ev else None,
                "acks": [a for a in self.acks if a["flag_id"] == flag_id],
                "assessment": self.assessments.get(flag_id)}

    @_guard
    async def list_flags(self, **kw):
        rows = list(self.flags.values())
        return rows[kw.get("offset", 0): kw.get("offset", 0) + kw.get("limit", 50)], len(rows)

    @_guard
    async def predictions(self, action_id, mission_id, kind=None):
        return [p for p in self.predictions_rows if (p["action_id"] == action_id if action_id else
                                                    (p["mission_id"] == mission_id and p["action_id"] is None))
                and (kind is None or p["event_kind"] == kind)]

    @_guard
    async def predictions_for_actions(self, action_ids, kind):
        return [p for p in self.predictions_rows if p["action_id"] in action_ids and p["event_kind"] == kind]

    @_guard
    async def insert_ack(self, flag_id, action, note, operator):
        ack = {"ack_id": next(self._ack_ids), "flag_id": flag_id, "action": action, "note": note,
               "operator": operator, "created_at": dt.datetime.now(dt.timezone.utc)}
        self.acks.append(ack)
        st = {"ACKNOWLEDGE": "ACKNOWLEDGED", "RESOLVE": "RESOLVED", "ESCALATE": "OPEN"}.get(action)
        if st:
            self.flags[flag_id]["status"] = st
        return ack

    @_guard
    async def upsert_assessment(self, flag_id, a, state="DONE"):
        row = {"assessment_id": flag_id, "flag_id": flag_id, "state": state,
               **{k: a.get(k) for k in ("question", "answer", "reasoning", "recommended_action",
                                        "severity_assessment", "confidence", "model", "latency_ms",
                                        "tokens_per_sec", "error")}}
        self.assessments[flag_id] = row
        return row

    @_guard
    async def upsert_document(self, d, embedding, embed_model):
        self.documents[d["doc_id"]] = {**d, "embedding": embedding, "embed_model": embed_model}

    @_guard
    async def action_bundle(self, action_id):
        a = self.ref.actions.get(action_id) if self.ref else None
        if not a:
            return None
        return {"action": a, "asset": self.ref.assets.get(a["asset_id"]),
                "location": self.ref.locations.get(a["location_id"]),
                "readings": [self.ref.reading_of(action_id)] if self.ref.readings.get(action_id) else [],
                "anomalies": [self.ref.anomaly_of(action_id)] if self.ref.anomalies.get(action_id) else []}

    @_guard
    async def asset_history(self, asset_id, exclude_flag_id):
        return {"anomalies": [], "prior_flags": []}

    @_guard
    async def reliability(self, group_by, filters):
        return [{"grp": "all", "n": 10, "n_success": 7, "success_rate_pct": 70.0, "ci95_pct": [39.7, 89.2]}]

    @_guard
    async def failure_baseline(self):
        return [{"location_id": "WP05", "action_type": "NAVIGATION", "n": 10, "failed": 3}]

    @_guard
    async def mission(self, mission_id):
        return self.ref.missions.get(mission_id) if self.ref else None

    @_guard
    async def failure_type_counts(self, mission_id):
        return {}

    @_guard
    async def mission_health(self):
        out = []
        for mid, m in self.ref.missions.items():
            acts = self.ref.mission_actions(mid)
            out.append({**m, "actions_total": len(acts),
                        "actions_failed": sum(1 for a in acts if a["status"] == "FAILED"),
                        "serious_anomalies": sum(1 for a in acts if (self.ref.anomalies.get(a["action_id"]) or {})
                                                 .get("severity") in ("HIGH", "CRITICAL")),
                        "kpi_true_health": None, "flags_open": 0})
        return out

    @_guard
    async def mission_actions(self, mission_id):
        return [{**a, "criticality": self.ref.assets[a["asset_id"]]["criticality"],
                 "anomaly": self.ref.anomaly_of(a["action_id"]), "reading": self.ref.reading_of(a["action_id"])}
                for a in self.ref.mission_actions(mission_id)]

    # search
    @_guard
    async def search_hybrid(self, vec, q, kinds, asset, location, mission, k):
        self.last_search = ("hybrid", vec, q)
        return [{"doc_id": "incident:AN002", "kind": "incident", "title": "gauge", "content": "HX-217 gauge HIGH",
                 "score": 0.03, "similarity": 0.71, "text_match": True}]

    @_guard
    async def search_fulltext(self, q, kinds, asset, location, mission, k):
        self.last_search = ("fulltext", None, q)
        return [{"doc_id": "policy:battery_low", "kind": "policy", "title": "Low battery", "content": "battery",
                 "score": 0.1}]

    # stepped replay (replay_runs): same semantics as the SQL in app/db/queries.py
    def _run_row(self, r):
        return {**r, "event_ids": list(r["event_ids"]), "counts": dict(r["counts"]),
                "db_now": dt.datetime.now(dt.timezone.utc) + self.replay_clock_skew}

    @_guard
    async def replay_create(self, params, event_ids, job_run_id):
        if any(r["state"] == "running" for r in self.replay_runs):
            return None
        r = {"run_id": len(self.replay_runs) + 1, "state": "running", "params": dict(params),
             "event_ids": list(event_ids), "next_index": 0, "counts": {}, "last_event": None,
             "job_run_id": job_run_id, "lease_token": None, "lease_until": None, "error": None,
             "started_at": dt.datetime.now(dt.timezone.utc), "finished_at": None}
        self.replay_runs.append(r)
        return self._run_row(r)

    @_guard
    async def replay_latest(self):
        return self._run_row(self.replay_runs[-1]) if self.replay_runs else None

    @_guard
    async def replay_claim(self, token, lease_s, run_id=None):
        now = dt.datetime.now(dt.timezone.utc)
        for r in self.replay_runs:
            if r["state"] == "running" and (r["lease_until"] is None or r["lease_until"] < now) \
                    and run_id in (None, r["run_id"]):
                r["lease_token"], r["lease_until"] = token, now + dt.timedelta(seconds=lease_s)
                return self._run_row(r)
        return None

    @_guard
    async def replay_advance(self, run_id, token, next_index, counts, last_event):
        self.replay_advances += 1
        r = self.replay_runs[run_id - 1]
        if r["lease_token"] != token or r["state"] != "running":
            return False
        r.update(next_index=next_index, counts=dict(counts), last_event=last_event)
        return True

    @_guard
    async def replay_release(self, run_id, token, state=None, error=None):
        r = self.replay_runs[run_id - 1]
        if r["lease_token"] != token or r["state"] != "running":
            return None
        r.update(lease_token=None, lease_until=None)
        if state:
            r.update(state=state, error=error, finished_at=dt.datetime.now(dt.timezone.utc))
        return self._run_row(r)

    @_guard
    async def replay_stop(self):
        for r in self.replay_runs:
            if r["state"] == "running":
                r.update(state="stopped", lease_token=None, lease_until=None,
                         finished_at=dt.datetime.now(dt.timezone.utc))
                return self._run_row(r)
        return None

    @_guard
    async def reset_demo(self, clear_predictions):
        n = len(self.flags)
        self.flags.clear()
        self.events.clear()
        return {"flags": n}

    @_guard
    async def list_events(self, **kw):
        return []
