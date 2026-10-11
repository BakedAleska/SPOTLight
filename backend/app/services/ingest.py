"""The ingest pipeline for one SPOT event (docs/backend-services-contract.md "Ingest pipeline"):

  1. append verbatim to raw.spot_event_log          (LOGS db; idempotent on (source, sha256))
  2. normalize into `events`                         (enriched with the action's reading/anomaly)
  3. build the canonical event                       (amd/system1/triage/events.py)
  4. call System 1 /v1/predict                       (Laya; logged to app.model_calls)
  5. store the answers in system1_predictions
  6. run the deterministic validator per TRIAGE_MODE (serial | parallel); both verdicts go in flags.reasons
  7. write a `flags` row when escalated

One request_id ties the event's rows together across raw.spot_event_log, app.model_calls and app.request_log.
Every step degrades instead of failing: DB down -> the verdict is still computed and returned
(`persisted: false`); System 1 down -> the rules-only verdict, marked `laya_unavailable`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional

import psycopg

from ..db.pool import DatabaseUnavailable
from . import validator as V
from .calllog import request_id_var
from .http import ServiceUnavailable, short_error
from .reference import EVENT_TYPES, Reference

log = logging.getLogger("backend.ingest")


def raw_sha256(raw: Dict[str, Any]) -> str:
    """sha256 of the event serialized like a replay.ndjson line (key order kept, default separators), so the
    same event hashes the same here and in `db/setup.py seed` (which hashes the file's line bytes)."""
    return hashlib.sha256(json.dumps(raw, default=str).encode("utf-8")).hexdigest()


class IngestPipeline:
    def __init__(self, repo, ref: Reference, system1, sink, default_mode: str = "serial"):
        self.repo = repo
        self.ref = ref
        self.system1 = system1
        self.sink = sink
        self.default_mode = default_mode
        self.stats = {"ingested": 0, "triaged": 0, "escalated": 0, "laya_unavailable": 0}

    async def ingest(self, raw: Dict[str, Any], *, source: str = "api", request_id: str,
                     mode: Optional[str] = None, laya: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """`laya` (live runs only, docs/backend-services-contract.md "Live simulation"): System 1's result computed
        by the caller instead of calling SYSTEM1_URL: {"status": "ok", "answers", "model", ...} is used as Laya's
        answer, {"status": "unavailable" | "skipped", "error"} means rules-only. None = call System 1 as usual."""
        token = request_id_var.set(request_id)
        try:
            return await self._ingest(raw, source, request_id, mode or self.default_mode, laya)
        finally:
            request_id_var.reset(token)

    async def _step(self, name: str, warnings: List[str], coro) -> Any:
        """Run one DB step; on failure record a warning and return the sentinel `_FAILED`.
        Once a step finds the database unavailable, later steps of the same event are skipped outright."""
        if any(w.startswith("db unavailable") for w in warnings):
            coro.close()
            return _FAILED
        try:
            return await coro
        except DatabaseUnavailable as exc:
            warnings.append("db unavailable: %s skipped (%s)" % (name, exc))
        except psycopg.Error as exc:
            warnings.append("%s failed: %s" % (name, short_error(exc)))
            log.warning("ingest step %s failed: %s", name, exc)
        return _FAILED

    async def _ingest(self, raw: Dict[str, Any], source: str, request_id: str, mode: str,
                      laya_given: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        t0 = time.perf_counter()
        warnings: List[str] = []
        etype = raw.get("event_type")
        if etype not in EVENT_TYPES:
            warnings.append("unknown event_type %r (stored raw only)" % etype)
        out: Dict[str, Any] = {"event_id": raw.get("event_id"), "request_id": request_id, "event_type": etype,
                               "mission_id": raw.get("mission_id"), "triage_mode": mode, "source": source}

        # 1. raw, verbatim
        log_id = await self._step("raw.spot_event_log", warnings, self.repo.append_raw(
            source, raw, raw_sha256(raw), _ts(raw), request_id))
        out["raw_log_id"] = None if log_id is _FAILED else log_id
        out["raw_duplicate"] = log_id is None

        # 2. normalize -> events
        norm = self.ref.normalize(raw)
        stored = await self._step("events", warnings, self.repo.upsert_event(norm))
        out["persisted"] = stored is not _FAILED
        out["action_id"] = norm["action_id"]
        self.stats["ingested"] += 1

        # 3. canonical event
        kind, ev, why = self.ref.canonical(raw, "replay" if source.startswith("replay") else source)
        out["kind"] = kind
        if kind is None:
            out["skipped"] = why
            out["latency_ms"] = int((time.perf_counter() - t0) * 1000)
            out["warnings"] = warnings
            return out
        out["triage_event_id"] = ev["event_id"]
        action_id = ev.get("action_id")
        mission_id = ev.get("mission_id")

        # 4. System 1
        answers: Optional[Dict[str, Any]] = None
        laya_error: Optional[str] = None
        model: Optional[str] = None
        s1: Dict[str, Any] = {}
        try:
            if laya_given is not None:
                resp = _given_system1(laya_given, ev["event_id"])
            else:
                resp = await self.system1.predict([{"id": ev["event_id"], "kind": kind, "event": ev}],
                                                  request_id=request_id, action_id=action_id, skip_if_busy=True)
            items = resp.get("items") or []
            item = next((i for i in items if i.get("id") == ev["event_id"]), items[0] if items else None)
            if item is None:
                raise ServiceUnavailable("system1", "response had no items")
            answers, model = item.get("answers") or {}, resp.get("model") or "unknown"
            s1 = {"status": "ok", "model": model, "fine_tuned": resp.get("fine_tuned"), "device": resp.get("device"),
                  "latency_ms": resp.get("latency_ms"), "answers": answers}
            if laya_given is not None:
                s1["via"] = "live_runner"
        except ServiceUnavailable as exc:
            laya_error = exc.reason
            s1 = {"status": "laya_unavailable", "error": exc.reason}
            self.stats["laya_unavailable"] += 1
        out["system1"] = s1

        # 5. predictions
        if answers:
            n = await self._step("system1_predictions", warnings, self.repo.store_predictions(
                kind, action_id, mission_id, model or "unknown", answers))
            s1["stored"] = 0 if n is _FAILED else n

        if kind == "pre_action":  # a forecast: nothing to validate or flag
            triage = {"kind": kind, "triage_event_id": ev["event_id"], "request_id": request_id,
                      "forecast": _forecast(answers), "laya": s1.get("status")}
            out["forecast"] = triage["forecast"]
            if out["persisted"]:
                await self._step("events.triage", warnings, self.repo.set_event_triage(norm["event_id"], triage))
            out["latency_ms"] = int((time.perf_counter() - t0) * 1000)
            out["warnings"] = warnings
            return out

        # 6. validator + TRIAGE_MODE
        rules = V.rules_verdict(kind, ev)
        laya = V.laya_verdict(answers)
        if answers is not None and laya is None:
            laya_error = "System 1 answered without an 'escalate' answer"
            s1["status"] = "laya_unavailable"
            s1["error"] = laya_error          # the reason detail is fixed text; the cause stays in the response
        decision = V.combine(mode, laya, rules, kind)
        reasons = V.build_reasons(mode, laya, laya_error, rules, decision, model)
        title = V.flag_title(kind, ev, decision, rules["rules"])
        self.stats["triaged"] += 1
        out["rules"] = {k: rules[k] for k in ("escalate", "priority", "category", "policy_version")}
        out["rules"]["fired"] = [r["rule"] for r in rules["rules"]]
        out["laya"] = laya
        out["decision"] = {**decision, "title": title}
        out["reasons"] = reasons

        # 7. flag
        out["flag"] = None
        if decision["escalated"]:
            self.stats["escalated"] += 1
            if out["persisted"]:
                res = await self._step("flags", warnings, self.repo.upsert_flag({
                    "event_id": norm["event_id"], "triage_event_id": ev["event_id"], "mission_id": mission_id,
                    "action_id": action_id, "location_id": norm["location_id"], "asset_id": norm["asset_id"],
                    "score": decision["score"], "severity": decision["severity"], "title": title,
                    "reasons": reasons, "escalated": True, "priority": decision["priority"],
                    "category": decision["category"]}))
                if res is not _FAILED:
                    out["flag"] = {"flag_id": res[0], "created": res[1]}
        elif out["persisted"]:
            fid = await self._step("flags", warnings, self.repo.unescalate_flag(ev["event_id"], reasons))
            if fid not in (None, _FAILED):
                out["flag"] = {"flag_id": fid, "created": False, "escalated": False}

        if out["persisted"]:
            triage = {"kind": kind, "triage_event_id": ev["event_id"], "request_id": request_id, "mode": mode,
                      "escalated": decision["escalated"], "priority": decision["priority"],
                      "category": decision["category"], "severity": decision["severity"],
                      "agreement": decision["agreement"], "summary": decision["summary"], "title": title,
                      "flag_id": (out["flag"] or {}).get("flag_id")}
            await self._step("events.triage", warnings, self.repo.set_event_triage(norm["event_id"], triage))
        out["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        out["warnings"] = warnings
        return out


class _Failed:
    def __repr__(self) -> str:
        return "<failed>"


_FAILED = _Failed()


def _given_system1(given: Dict[str, Any], event_id: str) -> Dict[str, Any]:
    """A caller-supplied System 1 result in /v1/predict's response shape; raises ServiceUnavailable for a
    rules-only result (status unavailable/skipped, or no answers)."""
    if given.get("status") != "ok" or not isinstance(given.get("answers"), dict) or not given["answers"]:
        raise ServiceUnavailable("system1", str(given.get("error") or given.get("status") or "no answers")[:300])
    return {"items": [{"id": event_id, "answers": given["answers"]}], "model": given.get("model"),
            "fine_tuned": given.get("fine_tuned"), "device": given.get("device"),
            "latency_ms": given.get("latency_ms")}


def _ts(raw: Dict[str, Any]):
    from .reference import parse_ts
    return parse_ts(raw.get("timestamp"))


def _forecast(answers: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not answers:
        return None
    wf = answers.get("will_fail") or {}
    fm = answers.get("failure_mode") or {}
    an = answers.get("anomaly_expected") or {}
    return {"p_fail": (wf.get("probs") or {}).get("A"), "failure_mode": fm.get("answer"),
            "failure_mode_confidence": fm.get("confidence"), "p_anomaly": (an.get("probs") or {}).get("A")}
