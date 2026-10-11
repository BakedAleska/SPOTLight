"""Live simulation (docs/backend-services-contract.md "Live simulation"; plan: docs/LIVE-SIM-PLAN.md).

An operator queues a run of one of five routes; the live runner on the presenter laptop (scripts/live_runner.py,
the only thing that can reach the AMD box) claims it and walks the route's 8 stops. Per stop:

  start     -> ACTION_STARTED ingested (no Laya); returns the canonical action event the runner sends to Laya
  result    -> ACTION_COMPLETED/FAILED ingested with the runner's Laya answers (TRIAGE_MODE parallel: rules run
               here, either side can escalate); red/green = the combined decision; live_captures row, event
               document and live KG nodes; returns the System 2 request and the texts to embed
  analysis  -> the runner's photo, vision readings and System 2 (Qwen) output; template if Qwen failed;
               assessment (red), explanation document, explanation KG node
  embeddings-> vectors the runner computed for those texts (checked against the stored text)

What is real: the robot's log is the kit row of `source_action_id`, re-sent now under a fresh action id
(LIVE-<run>-<stop>), so earlier runs are never overwritten; values are never invented. Photos are a fixed set of
kit sample images picked by action type: a simulated capture that never judges the asset (decision "demo images
never judge the asset"), so vision rows are pairing "demo" and red/green comes from the log only.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .embed import vec_literal
from .system2 import normalize_assessment, template_explanation
from . import validator as V
from .triage import T_policy as P
from .triage import T_questions as Q

log = logging.getLogger("backend.live")

ROUTES_PATH = Path(__file__).resolve().parents[1] / "data" / "live_routes.json"
LIVE_SOURCE = "live"
LIVE_TRIAGE_MODE = "parallel"       # rules run beside Laya: either can escalate, rules set the priority floor
SYSTEM2_TEMPLATE_PATHS = ("fallback", "insufficient_context")

# Mirrors amd/mcp/spot_mcp/domain.py ALLOWED_ACTIONS / suggested_action (tests/test_live.py checks they match), so a
# live packet offers System 2 the same action vocabulary as an MCP packet.
ALLOWED_ACTIONS: Dict[str, Dict[str, str]] = {
    "equipment": {
        "DISPATCH_TECHNICIAN": "send a technician to inspect the equipment this shift",
        "SCHEDULE_MAINTENANCE": "open a maintenance work order for the next maintenance window",
        "REINSPECT_NEXT_ROUTE": "re-check this asset on SPOT's next route and compare",
        "ESCALATE_TO_SUPERVISOR": "notify the shift supervisor now (critical asset or critical anomaly)",
        "MONITOR": "no action now; keep watching the trend",
    },
    "data_quality": {
        "REINSPECT_NOW": "send SPOT back to repeat the inspection now",
        "MANUAL_READING": "have a technician take the reading by hand",
        "CHECK_SENSOR_PAYLOAD": "inspect SPOT's sensor payload / camera for faults",
        "REINSPECT_NEXT_ROUTE": "repeat the inspection on the next route",
        "MONITOR": "no action now; keep watching",
    },
    "navigation": {
        "REINSPECT_NOW": "retry the waypoint now",
        "CLEAR_PATH": "have someone remove the obstacle blocking SPOT's path",
        "ADJUST_WAYPOINT": "move or re-record the waypoint / add fiducials (noisy or low-light area)",
        "UPDATE_AUTOWALK_MAP": "re-record the Autowalk route segment",
        "MONITOR": "no action now; keep watching",
    },
    "robot_health": {
        "RETURN_TO_DOCK": "send SPOT to the dock now",
        "CHARGE_BEFORE_NEXT_MISSION": "fully charge SPOT before the next mission",
        "CHECK_COMMS": "check radio / network coverage at this waypoint",
        "ABORT_MISSION": "stop the current mission",
        "MONITOR": "no action now; keep watching",
    },
    "mission": {
        "REVIEW_MISSION": "review the mission's failed actions and anomalies before signing it off",
        "RERUN_FAILED_ACTIONS": "schedule a mission that repeats only the failed actions",
        "CHARGE_BEFORE_NEXT_MISSION": "fully charge SPOT before the next mission",
        "MONITOR": "no action now",
    },
    "none": {
        "NO_ACTION": "routine event; log only",
        "MONITOR": "no action now; keep watching",
    },
}


def suggested_action(category: str, priority: Optional[str], facts: Dict[str, Any]) -> str:
    batt, ft = facts.get("battery_pct"), facts.get("failure_type")
    if category == "equipment":
        if priority == "P1":
            return "ESCALATE_TO_SUPERVISOR" if facts.get("severity") == "CRITICAL" else "DISPATCH_TECHNICIAN"
        return "SCHEDULE_MAINTENANCE" if priority == "P2" else "REINSPECT_NEXT_ROUTE"
    if category == "robot_health":
        if ft == "COMMUNICATION_LOSS":
            return "CHECK_COMMS" if (batt is None or batt > P.BATTERY_LOW) else "RETURN_TO_DOCK"
        if batt is not None and batt <= P.BATTERY_CRITICAL:
            return "RETURN_TO_DOCK"
        return "CHARGE_BEFORE_NEXT_MISSION"
    if category == "navigation":
        if ft in ("PATH_BLOCKED", "OBSTACLE_DETECTION"):
            return "CLEAR_PATH"
        if ft == "LOCALIZATION_FAILURE" or facts.get("environment") in ("HIGH_NOISE", "LOW_LIGHT"):
            return "ADJUST_WAYPOINT"
        return "REINSPECT_NOW"
    if category == "data_quality":
        return "REINSPECT_NOW" if priority in ("P1", "P2") else "REINSPECT_NEXT_ROUTE"
    if category == "mission":
        return "REVIEW_MISSION"
    return "NO_ACTION"


class LiveError(Exception):
    """A request the live API refuses: `status` is the HTTP status (404 unknown run/stop, 409 wrong state)."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def load_routes(path: Path = ROUTES_PATH) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _parse_ts(v: Optional[str]) -> dt.datetime:
    if v:
        try:
            t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            pass
    return _now()


def _h(s: Optional[str]) -> str:
    return (s or "").replace("_", " ").lower()


def _fmt(x: Any) -> str:
    if isinstance(x, float):
        return ("%.1f" % x).rstrip("0").rstrip(".")
    return str(x)


def _compact(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


class LiveService:
    def __init__(self, repo, live_repo, ref, pipeline, sink, embed_model: str, embed_dim: int,
                 routes: Optional[Dict[str, Any]] = None):
        self.repo = repo
        self.live = live_repo
        self.ref = ref
        self.pipeline = pipeline
        self.sink = sink
        self.embed_model = embed_model
        self.embed_dim = embed_dim
        data = routes or load_routes()
        self.routes_version = data.get("version")
        self.routes: Dict[str, Dict[str, Any]] = {r["route_id"]: r for r in data["routes"]}

    # ── routes & runs ─────────────────────────────────────────────────────────────────────────────
    def route_list(self) -> Dict[str, Any]:
        out = []
        for r in self.routes.values():
            stops = []
            for s in r["stops"]:
                loc = self.ref.locations.get(s["location_id"]) or {}
                asset = self.ref.assets.get(s["asset_id"]) or {}
                stops.append({**s, "location_name": loc.get("location_name"), "floor": loc.get("floor"),
                              "asset_name": asset.get("asset_name"), "criticality": asset.get("criticality")})
            out.append({**r, "stops": stops,
                        "planned_red": sum(1 for s in r["stops"] if s["planned_verdict"] == "red")})
        return {"version": self.routes_version, "routes": out}

    def _route(self, route_id: str) -> Dict[str, Any]:
        r = self.routes.get(route_id)
        if r is None:
            raise LiveError(404, "unknown route %r (one of %s)" % (route_id, ", ".join(self.routes)))
        return r

    async def create_run(self, route_id: str, requested_by: Optional[str]) -> Dict[str, Any]:
        r = self._route(route_id)
        run = await self.live.create_run(route_id, len(r["stops"]), requested_by)
        if run is None:
            active = await self.live.active_run()
            raise LiveError(409, "run %s (%s) is already %s; wait for it or cancel it" % (
                (active or {}).get("run_id"), (active or {}).get("route_id"), (active or {}).get("status")))
        return run

    async def cancel_run(self, run_id: int) -> Dict[str, Any]:
        run = await self._run(run_id)
        if run["status"] not in ("pending", "running"):
            return run
        return await self.live.update_run(run_id, status="cancelled", error="cancelled by the operator",
                                          finished=True)

    async def _run(self, run_id: int, running: bool = False) -> Dict[str, Any]:
        run = await self.live.get_run(run_id)
        if run is None:
            raise LiveError(404, "no live run %s" % run_id)
        if running and run["status"] != "running":
            raise LiveError(409, "live run %s is %s, not running" % (run_id, run["status"]))
        return run

    async def runs(self, limit: int) -> List[Dict[str, Any]]:
        return await self.live.list_runs(limit)

    async def run_detail(self, run_id: int) -> Dict[str, Any]:
        run = await self._run(run_id)
        return {"run": run, "route": self.routes.get(run["route_id"]), "captures": await self.live.captures(run_id)}

    async def captures(self, run_id: int, after: Optional[str]) -> Dict[str, Any]:
        run = await self._run(run_id)
        since = _parse_ts(after) if after else None
        rows = await self.live.captures(run_id, since)
        cursor = max([r["updated_at"] for r in rows], default=None)
        return {"run": run, "items": rows, "cursor": cursor.isoformat() if cursor else after}

    async def kg(self, run_id: int) -> Dict[str, Any]:
        await self._run(run_id)
        return await self.live.kg_for_run(run_id)

    def preview(self, route_id: Optional[str] = None) -> Dict[str, Any]:
        """Every stop's canonical action event (as a live run would send it, under run id 0) and the rules verdict,
        with no writes: the runner's --dry-run sends these to Laya and reports where Laya disagrees with the plan."""
        out = []
        for r in self.routes.values():
            if route_id and r["route_id"] != route_id:
                continue
            run = {"run_id": 0, "route_id": r["route_id"], "mission_id": "LIVE-0"}
            for i, stop in enumerate(r["stops"]):
                _, ev, why = self.ref.canonical(self._result_raw(run, i, _iso(_now())), "live")
                rules = V.rules_verdict("action", ev) if ev else None
                out.append({"route_id": r["route_id"], "stop": stop["stop"], "source_action_id": stop["source_action_id"],
                            "planned_verdict": stop["planned_verdict"], "planned_priority": stop["planned_priority"],
                            "action_event": ev, "error": why,
                            "rules": {"escalate": rules["escalate"], "priority": rules["priority"],
                                      "category": rules["category"],
                                      "fired": [x["rule"] for x in rules["rules"]]} if rules else None})
        return {"mode": LIVE_TRIAGE_MODE, "stops": out}

    # ── the runner's calls ────────────────────────────────────────────────────────────────────────
    async def claim(self, runner: Optional[str], request_id: str) -> Dict[str, Any]:
        run = await self.live.claim(runner)
        if run is None:
            return {"run": None}
        r = self._route(run["route_id"])
        first = r["stops"][0]
        kit = self.ref.actions[first["source_action_id"]]
        raw = {"event_id": "%s-start" % run["mission_id"], "mission_id": run["mission_id"],
               "timestamp": _iso(_now()), "event_type": "MISSION_STARTED",
               "details": {"mission_id": run["mission_id"], "battery_pct": first["battery_pct"],
                           "configuration_version": kit.get("configuration_version"), "scenario": "LIVE",
                           "live": self._live_tag(run, None)}}
        ingest = await self.pipeline.ingest(raw, source="live", request_id=request_id, mode=LIVE_TRIAGE_MODE,
                                            laya={"status": "skipped", "error": "mission start"})
        await self._kg_mission(run, r)
        return {"run": run, "route": self.route_list_one(r), "mission_event": _brief(ingest)}

    def route_list_one(self, r: Dict[str, Any]) -> Dict[str, Any]:
        return next(x for x in self.route_list()["routes"] if x["route_id"] == r["route_id"])

    def _live_tag(self, run: Dict[str, Any], stop: Optional[Dict[str, Any]], idx: Optional[int] = None) -> dict:
        tag = {"run_id": run["run_id"], "route_id": run["route_id"], "source": LIVE_SOURCE,
               "routes_version": self.routes_version}
        if stop is not None:
            tag.update(stop=stop["stop"], stop_index=idx, source_action_id=stop["source_action_id"])
        return tag

    def _stop(self, run: Dict[str, Any], idx: int) -> Dict[str, Any]:
        stops = self._route(run["route_id"])["stops"]
        if not 0 <= idx < len(stops):
            raise LiveError(404, "route %s has no stop %s" % (run["route_id"], idx))
        return stops[idx]

    def _details(self, run: Dict[str, Any], idx: int, result: bool) -> Dict[str, Any]:
        """The robot's log for this stop: the kit row of source_action_id, under the live action id."""
        stop = self._stop(run, idx)
        src = stop["source_action_id"]
        a = self.ref.actions[src]
        first = self._route(run["route_id"])["stops"][0]
        d: Dict[str, Any] = {
            "action_id": self.action_id(run, stop), "source_action_id": src, "mission_id": run["mission_id"],
            "location_id": a["location_id"], "asset_id": a["asset_id"], "action_type": a["action_type"],
            "battery_pct": a["battery_pct"], "sequence": idx + 1, "duration_sec": a.get("duration_sec"),
            "configuration_version": a.get("configuration_version"),
            "mission": {"mission_id": run["mission_id"], "mission_name": "Live run %s (%s)" % (
                run["run_id"], run["route_id"]), "scenario": "LIVE",
                "configuration_version": a.get("configuration_version"),
                "start_battery_pct": first["battery_pct"]},
            "live": self._live_tag(run, stop, idx)}
        if result:
            d["status"] = a["status"]
            d["failure_type"] = a.get("failure_type")
            d["reading"] = self.ref.reading_of(src)
            d["anomaly"] = self.ref.anomaly_of(src)
        return d

    @staticmethod
    def action_id(run: Dict[str, Any], stop: Dict[str, Any]) -> str:
        return "LIVE-%s-%s" % (run["run_id"], stop["stop"])

    async def stop_start(self, run_id: int, idx: int, request_id: str) -> Dict[str, Any]:
        run = await self._run(run_id, running=True)
        stop = self._stop(run, idx)
        t0 = time.perf_counter()
        details = self._details(run, idx, result=False)
        now = _now()
        raw = {"event_id": "%s-start" % details["action_id"], "mission_id": run["mission_id"],
               "timestamp": _iso(now), "event_type": "ACTION_STARTED", "details": details}
        ingest = await self.pipeline.ingest(raw, source="live", request_id=request_id, mode=LIVE_TRIAGE_MODE,
                                            laya={"status": "skipped", "error": "pre-action forecast not requested"})
        res = self._details(run, idx, result=True)
        a = self.ref.actions[stop["source_action_id"]]
        capture = await self.live.upsert_capture(run_id, idx, stop["stop"], {
            "action_id": details["action_id"], "source_action_id": stop["source_action_id"],
            "location_id": a["location_id"], "asset_id": a["asset_id"], "action_type": a["action_type"],
            "battery_pct": a["battery_pct"], "status": a["status"], "failure_type": a.get("failure_type"),
            "reading": res["reading"], "anomaly": res["anomaly"], "phase": "arrived",
            "photo": {"path": stop["photo"], "kind": stop["photo_kind"], "label": "simulated capture",
                      "note": "kit sample image picked by action type; it does not show this asset"},
            "timings": {"start_ms": int((time.perf_counter() - t0) * 1000)}})
        await self.live.touch_run(run_id, idx + 1)
        # The canonical action event as the result will be built: the runner sets `timestamp` to the result time
        # it then sends to /result, so Laya sees exactly the event the backend triages.
        raw_result = self._result_raw(run, idx, _iso(now))
        _, ev, why = self.ref.canonical(raw_result, "live")
        return {"capture": capture, "action_event": ev, "action_event_error": why, "kind": "action",
                "start_event": _brief(ingest)}

    def _result_raw(self, run: Dict[str, Any], idx: int, ts: str) -> Dict[str, Any]:
        details = self._details(run, idx, result=True)
        etype = "ACTION_FAILED" if details["status"] == "FAILED" else "ACTION_COMPLETED"
        return {"event_id": "%s-result" % details["action_id"], "mission_id": run["mission_id"], "timestamp": ts,
                "event_type": etype, "details": details}

    async def stop_result(self, run_id: int, idx: int, timestamp: Optional[str], laya: Optional[Dict[str, Any]],
                          request_id: str) -> Dict[str, Any]:
        run = await self._run(run_id, running=True)
        stop = self._stop(run, idx)
        t0 = time.perf_counter()
        ts = _iso(_parse_ts(timestamp))
        raw = self._result_raw(run, idx, ts)
        laya = laya or {"status": "unavailable", "error": "the live runner sent no Laya result"}
        if laya.get("status") == "ok":
            self.sink.model_call(system="system1", ok=True, model=laya.get("model"), endpoint="/v1/predict",
                                 action_id=raw["details"]["action_id"], latency_ms=laya.get("latency_ms"),
                                 device=laya.get("device"), request_id=request_id,
                                 metadata={"via": "live_runner", "run_id": run_id, "stop": stop["stop"]})
        out = await self.pipeline.ingest(raw, source="live", request_id=request_id, mode=LIVE_TRIAGE_MODE, laya=laya)
        decision = out.get("decision") or {}
        red = bool(decision.get("escalated"))
        flag_id = (out.get("flag") or {}).get("flag_id") if red else None
        s1 = out.get("system1") or {}
        laya_view = _compact({
            "status": s1.get("status"), "error": s1.get("error"), "model": s1.get("model"),
            "device": s1.get("device"), "fine_tuned": s1.get("fine_tuned"), "latency_ms": s1.get("latency_ms"),
            "answers": {q: _compact({"answer": a.get("answer"), "confidence": a.get("confidence"),
                                     "meaning": _meaning(q, a.get("answer"))})
                        for q, a in (s1.get("answers") or {}).items() if isinstance(a, dict)},
            "verdict": out.get("laya")})
        ingest_ms = int((time.perf_counter() - t0) * 1000)
        capture = await self.live.upsert_capture(run_id, idx, stop["stop"], {
            "phase": "triaged", "event_id": raw["event_id"], "flag_id": flag_id, "verdict": "red" if red else "green",
            "priority": decision.get("priority"), "severity": decision.get("severity"),
            "category": decision.get("category"), "laya": laya_view, "rules": out.get("rules"),
            "decision": _compact({**decision, "mode": LIVE_TRIAGE_MODE, "persisted": out.get("persisted"),
                                  "warnings": out.get("warnings")}),
            "qwen_source": "pending"}, stamp="triaged_at")
        await self.live.merge_capture_json(run_id, idx, "timings", {"ingest_ms": ingest_ms,
                                                                    "pipeline_ms": out.get("latency_ms")})
        # event document + KG
        ev_text = self._event_text(run, stop, raw, out)
        ev_doc = "event:act-%s" % raw["details"]["action_id"]
        try:
            await self.repo.upsert_document(self._event_doc(ev_doc, raw, out, ev_text), None, None)
        except Exception as exc:  # noqa: BLE001 - the stop still counts; the document is extra
            log.warning("live event document %s not stored: %s", ev_doc, exc)
        kg_ids, embed_nodes = await self._kg_stop(run, stop, raw, out, flag_id, ev_doc, ev_text)
        await self.live.merge_capture_json(run_id, idx, "kg", {"triage": kg_ids})
        packet = await self.packet(run, stop, raw, out, flag_id)
        flag_view = packet["flag"]
        s2 = {"flag_id": flag_id or int(capture["capture_id"]), "request_id": request_id, "flag": flag_view,
              "event": {"event_id": raw["event_id"], "event_type": raw["event_type"]},
              "reasons": [r.get("detail") for r in (out.get("reasons") or []) if isinstance(r, dict) and r.get("detail")],
              "context": packet}
        capture = await self.live.get_capture(run_id, idx) or capture
        return {"capture": capture, "verdict": "red" if red else "green", "flag_id": flag_id,
                "system2_request": s2,
                "photo": {"path": stop["photo"], "kind": stop["photo_kind"]},
                "embed": [{"key": "doc:" + ev_doc, "text": ev_text}] + embed_nodes}

    async def stop_analysis(self, run_id: int, idx: int, body: Dict[str, Any], request_id: str) -> Dict[str, Any]:
        run = await self._run(run_id)
        stop = self._stop(run, idx)
        cap = await self.live.get_capture(run_id, idx)
        if cap is None or cap.get("verdict") is None:
            raise LiveError(409, "stop %s of run %s has not been triaged yet" % (stop["stop"], run_id))
        s2 = body.get("system2") if isinstance(body.get("system2"), dict) else None
        s2_req = body.get("system2_request") if isinstance(body.get("system2_request"), dict) else {}
        flag_like = dict(s2_req.get("flag") or {})
        packet = s2_req.get("context") if isinstance(s2_req.get("context"), dict) else None
        qwen: Dict[str, Any]
        fallback = None
        if s2 and s2.get("answer") and s2.get("path") not in SYSTEM2_TEMPLATE_PATHS and s2.get("model") != "template":
            qwen = {**normalize_assessment(s2), "source": "system2", "path": s2.get("path"),
                    "recommended_action": s2.get("recommended_action"), "confidence_label": s2.get("confidence"),
                    "image": body.get("image_status")}
        else:
            if s2 and s2.get("answer"):
                fallback = "System 2 answered with its checked template (path %s, model %s)" % (
                    s2.get("path"), s2.get("model"))
            else:
                fallback = str(body.get("system2_error") or "System 2 (Qwen) did not answer")[:300]
            event_row = {"payload": {"enriched": self._enriched(cap)}}
            flag_like.setdefault("reasons", [])
            flag_like.update({"escalated": cap["verdict"] == "red", "priority": cap.get("priority"),
                              "severity": cap.get("severity"), "category": cap.get("category"),
                              "asset_id": cap.get("asset_id"), "location_id": cap.get("location_id"),
                              "action_id": cap.get("action_id"), "mission_id": run.get("mission_id")})
            qwen = {**template_explanation(flag_like, event_row, packet), "source": "template",
                    "fallback_reason": fallback, "model": "template"}
        qwen["latency_ms_total"] = body.get("system2_ms")
        source = qwen["source"]
        assessment_id = None
        if cap.get("flag_id"):
            try:
                stored = await self.repo.upsert_assessment(int(cap["flag_id"]), qwen,
                                                           state="DONE" if source == "system2" else "TEMPLATE")
                assessment_id = stored.get("assessment_id")
            except Exception as exc:  # noqa: BLE001
                log.warning("live assessment for flag %s not stored: %s", cap["flag_id"], exc)
        doc_id, embed = None, []
        if source == "system2":     # templates are never indexed as explanations (audit A-8)
            key = str(assessment_id) if assessment_id else "live-%s" % cap["capture_id"]
            doc_id = "explanation:%s" % key
            text = "Q: %s A: %s Why: %s Action: %s" % (qwen.get("question"), qwen.get("answer"),
                                                         qwen.get("reasoning"), qwen.get("recommended_action"))
            try:
                await self.repo.upsert_document({
                    "doc_id": doc_id, "kind": "explanation", "source_id": key, "mission_id": run.get("mission_id"),
                    "action_id": cap.get("action_id"), "asset_id": cap.get("asset_id"),
                    "location_id": cap.get("location_id"), "action_type": cap.get("action_type"),
                    "status": cap.get("status"), "severity": cap.get("severity"),
                    "title": "Qwen on live stop %s (%s, %s)" % (cap.get("action_id"), cap.get("asset_id"),
                                                                cap["verdict"]),
                    "content": text, "metadata": {"flag_id": cap.get("flag_id"), "model": qwen.get("model"),
                                                  "run_id": run_id, "stop": stop["stop"], "source": LIVE_SOURCE}},
                    None, None)
                embed.append({"key": "doc:" + doc_id, "text": text})
                await self._kg_explanation(run, stop, cap, key, doc_id, qwen)
            except Exception as exc:  # noqa: BLE001
                log.warning("live explanation document %s not stored: %s", doc_id, exc)
                doc_id = None
        timings = {k: v for k, v in (body.get("timings") or {}).items() if isinstance(v, (int, float))}
        capture = await self.live.upsert_capture(run_id, idx, stop["stop"], {
            "phase": "done", "qwen": qwen, "qwen_source": source, "doc_id": doc_id,
            "vision": body.get("vision") if isinstance(body.get("vision"), dict) else (
                {"error": str(body.get("vision_error"))[:300]} if body.get("vision_error") else None),
            "photo": {**(cap.get("photo") or {}), **{k: v for k, v in (body.get("photo") or {}).items()
                                                      if k in ("sha256", "bytes", "path", "image_status")}}},
            stamp="analysed_at")
        if timings:
            await self.live.merge_capture_json(run_id, idx, "timings", timings)
        await self.live.touch_run(run_id)
        return {"capture": capture, "qwen_source": source, "fallback_reason": fallback, "embed": embed,
                "assessment_id": assessment_id}

    async def embeddings(self, run_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
        await self._run(run_id)
        model = str(body.get("model") or self.embed_model)
        done, skipped = [], []
        for item in body.get("items") or []:
            key, text, vec = item.get("key"), item.get("text"), item.get("vector")
            if not (isinstance(key, str) and isinstance(text, str) and isinstance(vec, list)):
                skipped.append({"key": key, "why": "needs key, text and vector"})
                continue
            if len(vec) != self.embed_dim:
                skipped.append({"key": key, "why": "vector has %d dims, EMBED_DIM is %d" % (len(vec), self.embed_dim)})
                continue
            lit = vec_literal(vec)
            if key.startswith("doc:"):
                ok = await self.live.embed_document(key[4:], text, lit, model)
            elif key.startswith("node:"):
                ok = await self.live.embed_node(key[5:], text, lit, model)
            else:
                ok = False
            (done if ok else skipped).append(key if ok else {"key": key, "why": "no row with exactly this text"})
        return {"embedded": done, "skipped": skipped, "model": model}

    async def finish(self, run_id: int, status: str, error: Optional[str], request_id: str) -> Dict[str, Any]:
        run = await self._run(run_id)
        if run["status"] not in ("running", "pending"):
            return run
        caps = await self.live.captures(run_id)
        counts = {"red": sum(1 for c in caps if c.get("verdict") == "red"),
                  "green": sum(1 for c in caps if c.get("verdict") == "green"),
                  "qwen": sum(1 for c in caps if c.get("qwen_source") == "system2"),
                  "template": sum(1 for c in caps if c.get("qwen_source") == "template"),
                  "laya_live": sum(1 for c in caps if (c.get("laya") or {}).get("status") == "ok"),
                  "stops": len(caps)}
        last = caps[-1] if caps else None
        raw = {"event_id": "%s-end" % run["mission_id"], "mission_id": run["mission_id"], "timestamp": _iso(_now()),
               "event_type": "MISSION_COMPLETED",
               "details": {"mission_id": run["mission_id"], "status": "COMPLETED" if status == "done" else "FAILED",
                           "end_battery_pct": (last or {}).get("battery_pct"), "live": self._live_tag(run, None),
                           "counts": counts}}
        await self.pipeline.ingest(raw, source="live", request_id=request_id, mode=LIVE_TRIAGE_MODE,
                                   laya={"status": "skipped", "error": "live run summary"})
        return await self.live.update_run(run_id, status=status if status in ("done", "error") else "done",
                                          error=error, counts=counts, finished=True)

    # ── System 2 packet (same shape as MCP get_flag_context, built from the live stop) ─────────────
    async def packet(self, run: Dict[str, Any], stop: Dict[str, Any], raw: Dict[str, Any], out: Dict[str, Any],
                     flag_id: Optional[int]) -> Dict[str, Any]:
        d = raw["details"]
        a_id = d["action_id"]
        loc = self.ref.locations.get(d["location_id"]) or {}
        asset = self.ref.assets.get(d["asset_id"]) or {}
        decision = out.get("decision") or {}
        rules = out.get("rules") or {}
        reasons = [r for r in (out.get("reasons") or []) if isinstance(r, dict)]
        fired = [r for r in reasons if r.get("rule") not in ("laya_verdict", "rules_verdict", "laya_unavailable")]
        red = bool(decision.get("escalated"))
        category = decision.get("category") if decision.get("category") in ALLOWED_ACTIONS else "none"
        if not red:
            category = "none"
        rd, an = d.get("reading"), d.get("anomaly")
        readings = []
        if rd:
            lo, hi = P.SAFE_BANDS.get(rd.get("measurement_type"), (None, None))
            v = rd.get("value")
            out_band = v is not None and ((lo is not None and v < lo) or (hi is not None and v > hi))
            readings.append(_compact({"id": "reading:%s" % rd.get("reading_id"), "measurement_type":
                                      rd.get("measurement_type"), "value": v, "unit": rd.get("unit"),
                                      "quality": rd.get("quality"), "safe_band": [lo, hi],
                                      "assessment": "out of band" if out_band else "within band"}))
        anomalies = [_compact({"id": "anomaly:%s" % an.get("anomaly_id"), "anomaly_type": an.get("anomaly_type"),
                               "severity": an.get("severity"), "description": an.get("description")})] if an else []
        sugg = suggested_action(category, decision.get("priority"), {
            "battery_pct": d.get("battery_pct"), "failure_type": d.get("failure_type"),
            "severity": (an or {}).get("severity"), "environment": loc.get("environment")})
        event = _compact({
            "id": "action:%s" % a_id, "mission_id": run["mission_id"], "sequence": d["sequence"],
            "timestamp": raw["timestamp"], "action_type": d["action_type"], "status": d["status"],
            "failure_type": d.get("failure_type"), "battery_pct": d["battery_pct"],
            "duration_sec": d.get("duration_sec"), "configuration_version": d.get("configuration_version"),
            "source_action_id": d["source_action_id"],
            "asset": _compact({"id": "asset:%s" % d["asset_id"], "name": asset.get("asset_name"),
                               "type": asset.get("asset_type"), "criticality": asset.get("criticality")}),
            "location": _compact({"id": "location:%s" % d["location_id"], "name": loc.get("location_name"),
                                  "zone": loc.get("zone_type"), "environment": loc.get("environment"),
                                  "floor": loc.get("floor")}),
            "readings": readings, "anomalies": anomalies,
            "validator": {"priority": rules.get("priority"), "category": rules.get("category"),
                          "escalate": "A" if rules.get("escalate") else "B", "suggested_action": sugg}})
        title = decision.get("title") or "%s at %s" % (_h(d["action_type"]), d["location_id"])
        flag = _compact({
            "id": "flag:%s" % flag_id if flag_id else "check:%s" % a_id, "flag_id": flag_id, "title": title,
            "severity": decision.get("severity"), "priority": decision.get("priority"), "category": category,
            "status": "OPEN" if red else "NOT_ESCALATED", "escalated": red, "mission_id": run["mission_id"],
            "action_id": a_id, "asset_id": d["asset_id"], "location_id": d["location_id"],
            "reasons": [_compact(dict(r)) for r in reasons[:8]]})
        facts: List[str] = []
        for r in fired[:4]:
            facts.append("Rule %s fired (%s): %s." % (r.get("rule"), r.get("priority") or r.get("tier") or "?",
                                                     r.get("detail")))
        if not fired:
            facts.append("No policy rule fired for this action (rules priority %s, not escalated)."
                         % rules.get("priority"))
        for r in readings:
            lo, hi = r.get("safe_band", [None, None])
            facts.append("Reading %s %s %s (quality %s) vs safe band %s..%s: %s." % (
                r.get("measurement_type"), _fmt(r.get("value")), r.get("unit") or "", r.get("quality"),
                _fmt(lo) if lo is not None else "-inf", _fmt(hi) if hi is not None else "+inf", r.get("assessment")))
        batt = d.get("battery_pct")
        facts.append("Battery %s%% at this stop (critical<=%d, low<=%d, watch<=%d)." % (
            batt, P.BATTERY_CRITICAL, P.BATTERY_LOW, P.BATTERY_WATCH))
        try:
            rel = await self.repo.reliability(None, {"location_id": d["location_id"], "action_type": d["action_type"]})
            if rel and rel[0].get("n"):
                facts.append("Kit history: %s at %s succeeds %s%% of the time (n=%d)." % (
                    d["action_type"], d["location_id"], rel[0].get("success_rate_pct"), rel[0]["n"]))
        except Exception as exc:  # noqa: BLE001 - statistics are extra context
            log.info("live packet reliability lookup failed: %s", exc)
        lv = out.get("laya") or {}
        if lv:
            facts.append("Laya (System 1) says escalate=%s with p=%s; the combined decision (%s mode) is %s %s." % (
                "yes" if lv.get("escalate") else "no", lv.get("p_escalate"), LIVE_TRIAGE_MODE,
                "ESCALATED" if red else "not escalated", decision.get("priority")))
        else:
            facts.append("Laya (System 1) did not answer (%s); the rules decided alone: %s %s." % (
                (out.get("system1") or {}).get("error") or "unavailable", "ESCALATED" if red else "not escalated",
                decision.get("priority")))
        s1 = []
        for q, a in ((out.get("system1") or {}).get("answers") or {}).items():
            if isinstance(a, dict) and a.get("answer") is not None:
                s1.append(_compact({"id": "s1:%s:%s" % (a_id, q), "when": "action", "q": q, "answer": a.get("answer"),
                                    "meaning": _meaning(q, a.get("answer")),
                                    "p": (a.get("probs") or {}).get(a.get("answer"))}))
        packet: Dict[str, Any] = {"packet_version": "1-live", "source": LIVE_SOURCE, "flag": flag, "event": event}
        if s1:
            packet["system1"] = s1[:6]
        packet["facts"] = [{"id": "F%d" % (i + 1), "fact": t} for i, t in enumerate(facts)]
        packet["allowed_actions"] = dict(ALLOWED_ACTIONS[category])
        packet["suggested_action"] = sugg if sugg in ALLOWED_ACTIONS[category] else next(iter(ALLOWED_ACTIONS[category]))
        packet["insufficient_context"] = False
        packet["gaps"] = ["live run: the robot's log is kit row %s re-sent now; the photo is a simulated capture"
                          % d["source_action_id"]]
        ids = [packet["flag"]["id"], event["id"], event["asset"]["id"], event["location"]["id"]]
        ids += [r["id"] for r in readings] + [x["id"] for x in anomalies] + [x["id"] for x in s1]
        ids += [f["id"] for f in packet["facts"]]
        packet["citation_ids"] = ids
        subject = "%s (%s)" % (asset.get("asset_name"), d["asset_id"]) if asset.get("asset_name") else d["asset_id"]
        at = " at %s" % loc.get("location_name") if loc.get("location_name") else ""
        if red:
            packet["question"] = ("Flag %s \"%s\": what is wrong with %s%s, can the evidence be trusted, and which "
                                  "allowed action should the operator take?" % (flag_id, title, subject, at))
        else:
            packet["question"] = ("Stop %s, %s%s was not escalated (%s): does anything in the evidence need the "
                                  "operator's attention, and which allowed action fits?"
                                  % (stop["stop"], subject, at, decision.get("priority")))
        return packet

    # ── documents & knowledge graph ──────────────────────────────────────────────────────────────
    def _event_text(self, run: Dict[str, Any], stop: Dict[str, Any], raw: Dict[str, Any], out: Dict[str, Any]) -> str:
        d = raw["details"]
        loc = self.ref.locations.get(d["location_id"]) or {}
        asset = self.ref.assets.get(d["asset_id"]) or {}
        dec = out.get("decision") or {}
        parts = ["Live run %s stop %s (route %s): %s at %s (%s) on %s (%s, %s criticality), %s%s, battery %s%%." % (
            run["run_id"], stop["stop"], run["route_id"], d["action_type"], d["location_id"],
            loc.get("location_name"), d["asset_id"], asset.get("asset_name"), asset.get("criticality"),
            d["status"], " (%s)" % d["failure_type"] if d.get("failure_type") else "", d["battery_pct"])]
        rd, an = d.get("reading"), d.get("anomaly")
        if rd:
            parts.append("Reading %s %s %s, quality %s." % (rd.get("measurement_type"), _fmt(rd.get("value")),
                                                            rd.get("unit"), rd.get("quality")))
        if an:
            parts.append("Anomaly %s (%s): %s" % (an.get("anomaly_type"), an.get("severity"), an.get("description")))
        lv = out.get("laya") or {}
        parts.append("Verdict %s %s (%s mode; rules %s, fired: %s; Laya %s)." % (
            "RED" if dec.get("escalated") else "GREEN", dec.get("priority"), LIVE_TRIAGE_MODE,
            (out.get("rules") or {}).get("priority"), ", ".join((out.get("rules") or {}).get("fired") or []) or "none",
            ("escalate p=%s" % lv.get("p_escalate")) if lv else "unavailable"))
        parts.append("Log re-sent from kit row %s." % d["source_action_id"])
        return " ".join(parts)

    def _event_doc(self, doc_id: str, raw: Dict[str, Any], out: Dict[str, Any], text: str) -> Dict[str, Any]:
        d = raw["details"]
        an = d.get("anomaly") or {}
        dec = out.get("decision") or {}
        return {"doc_id": doc_id, "kind": "event", "source_id": raw["event_id"], "mission_id": d["mission_id"],
                "action_id": d["action_id"], "asset_id": d["asset_id"], "location_id": d["location_id"],
                "action_type": d["action_type"], "status": d["status"], "anomaly_type": an.get("anomaly_type"),
                "severity": an.get("severity") or dec.get("severity"), "ts": _parse_ts(raw["timestamp"]).replace(tzinfo=None),
                "title": dec.get("title") or "%s at %s" % (d["action_type"], d["location_id"]), "content": text,
                "metadata": {"source": LIVE_SOURCE, "run_id": d["live"]["run_id"], "stop": d["live"]["stop"],
                             "source_action_id": d["source_action_id"], "verdict": "red" if dec.get("escalated") else "green"}}

    def _props(self, run: Dict[str, Any], stop: Optional[Dict[str, Any]], **extra: Any) -> Dict[str, Any]:
        p = {"source": LIVE_SOURCE, "built_by": LIVE_SOURCE, "run_id": str(run["run_id"]), "route_id": run["route_id"]}
        if stop is not None:
            p["stop"] = stop["stop"]
        p.update({k: v for k, v in extra.items() if v is not None})
        return p

    async def _kg_mission(self, run: Dict[str, Any], r: Dict[str, Any]) -> None:
        node = {"node_id": "mission:%s" % run["mission_id"], "kind": "mission", "key": run["mission_id"],
                "label": "Live run #%s · %s %s" % (run["run_id"], r["route_id"], r["name"]),
                "summary": "Live SPOT patrol run %s on route %s (%s), %d stops %s, started %s UTC. %s" % (
                    run["run_id"], r["route_id"], r["name"], len(r["stops"]),
                    " → ".join(s["location_id"] for s in r["stops"]), _iso(_now()), r["description"]),
                "props": self._props(run, None, scenario="LIVE")}
        try:
            await self.live.kg_upsert([node], [])
        except Exception as exc:  # noqa: BLE001 - the graph is extra; the run goes on
            log.warning("live mission node not stored: %s", exc)

    async def _kg_stop(self, run, stop, raw, out, flag_id, ev_doc, ev_text) -> Tuple[Dict[str, Any], List[dict]]:
        d = raw["details"]
        a_id = d["action_id"]
        asset = self.ref.assets.get(d["asset_id"]) or {}
        dec = out.get("decision") or {}
        nodes, edges, embed = [], [], []
        act = "action:%s" % a_id

        def edge(src, rel, dst, evidence=None):
            edges.append({"src": src, "rel": rel, "dst": dst, "props": self._props(run, stop),
                          "evidence": evidence or ev_doc})
        nodes.append({"node_id": act, "kind": "action", "key": a_id,
                      "label": "%s · %s %s @ %s" % (a_id, _h(d["action_type"]), d["asset_id"], d["location_id"]),
                      "summary": ev_text, "doc_id": ev_doc,
                      "props": self._props(run, stop, status=d["status"], verdict="red" if dec.get("escalated")
                                           else "green", source_action_id=d["source_action_id"])})
        edge(act, "part_of", "mission:%s" % run["mission_id"])
        edge(act, "at", "location:%s" % d["location_id"])
        edge(act, "inspects", "asset:%s" % d["asset_id"])
        edge(act, "of_type", "action_type:%s" % d["action_type"])
        if d["status"] == "FAILED" and d.get("failure_type"):
            edge(act, "failed_with", "failure_type:%s" % d["failure_type"])
        an = d.get("anomaly")
        if an:
            nid = "anomaly:%s" % a_id
            summary = "%s (%s) on %s (%s) during live action %s at %s; log from kit row %s: %s" % (
                an.get("anomaly_type"), an.get("severity"), d["asset_id"], asset.get("asset_name"), a_id,
                d["location_id"], an.get("anomaly_id"), an.get("description"))
            nodes.append({"node_id": nid, "kind": "anomaly", "key": a_id,
                          "label": "%s on %s (%s)" % (_h(an.get("anomaly_type")), d["asset_id"], a_id),
                          "summary": summary, "props": self._props(run, stop, severity=an.get("severity"))})
            embed.append({"key": "node:" + nid, "text": summary})
            edge(nid, "of_type", "anomaly_type:%s" % an.get("anomaly_type"))
            edge(nid, "raised_on", "asset:%s" % d["asset_id"])
            edge(nid, "during", act)
        if flag_id:
            fid = "flag:%s" % flag_id
            fired = [r for r in (out.get("rules") or {}).get("fired") or []]
            summary = "Flag #%s %s %s (%s, OPEN), live run %s stop %s: %s. Rules: %s." % (
                flag_id, dec.get("priority"), dec.get("severity"), dec.get("category"), run["run_id"], stop["stop"],
                dec.get("title"), ", ".join(fired) or "none")
            nodes.append({"node_id": fid, "kind": "flag", "key": str(flag_id),
                          "label": "Flag #%s: %s" % (flag_id, (dec.get("title") or "")[:90]), "summary": summary,
                          "props": self._props(run, stop, priority=dec.get("priority"), severity=dec.get("severity"),
                                               category=dec.get("category"))})
            embed.append({"key": "node:" + fid, "text": summary})
            edge(fid, "flags", act)
            for rule in fired:
                edge(fid, "triggered", "policy_rule:%s" % rule)
        try:
            counts = await self.live.kg_upsert(nodes, edges)
        except Exception as exc:  # noqa: BLE001
            log.warning("live KG rows for %s not stored: %s", a_id, exc)
            return {"error": str(exc)[:200]}, []
        return {"nodes": [n["node_id"] for n in nodes], "edges": counts["edges"]}, embed

    async def _kg_explanation(self, run, stop, cap, key, doc_id, qwen) -> None:
        nid = "explanation:%s" % key
        target = "flag:%s" % cap["flag_id"] if cap.get("flag_id") else "action:%s" % cap["action_id"]
        summary = "System 2 (%s) on live stop %s (%s): %s Recommended: %s" % (
            qwen.get("model"), cap["action_id"], cap["verdict"], (qwen.get("answer") or "")[:200],
            (qwen.get("recommended_action") or "-")[:120])
        await self.live.kg_upsert(
            [{"node_id": nid, "kind": "explanation", "key": key, "label": "Qwen on %s" % cap["action_id"],
              "summary": summary, "doc_id": doc_id, "props": self._props(run, stop, model=qwen.get("model"))}],
            [{"src": nid, "rel": "explains", "dst": target, "props": self._props(run, stop), "evidence": doc_id}])
        await self.live.merge_capture_json(run["run_id"], cap["stop_index"], "kg", {"explanation": nid})

    def _enriched(self, cap: Dict[str, Any]) -> Dict[str, Any]:
        loc = self.ref.locations.get(cap.get("location_id") or "") or {}
        asset = self.ref.assets.get(cap.get("asset_id") or "") or {}
        return {"location": {k: loc.get(k) for k in ("location_name", "zone_type", "environment")},
                "asset": {k: asset.get(k) for k in ("asset_name", "asset_type", "criticality")}}


def _meaning(q: str, answer: Any) -> Optional[str]:
    try:
        return ((Q.QUESTIONS.get(q) or {}).get("criteria") or {}).get(answer)
    except AttributeError:
        return None


def _brief(ingest: Dict[str, Any]) -> Dict[str, Any]:
    return {k: ingest.get(k) for k in ("event_id", "event_type", "persisted", "raw_duplicate", "skipped", "warnings")}
