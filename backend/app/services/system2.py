"""System 2 ("Analyst") client plus the templated fallbacks used when it is unset or down.

POST {SYSTEM2_URL}/v1/assess (docs/ARCHITECTURE.md §4) -> Orbit-style JSON:
question / answer / reasoning / recommended_action (+ severity_assessment, confidence, model, latency, tokens/s).
POST {SYSTEM2_URL}/v1/debrief -> a mission after-action summary.

The templates build the same shape deterministically from the flag's stored reasons (both verdicts and the
fired policy rules), so the operator always gets an explanation, marked `source: "template"`.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

import httpx

from .calllog import LogSink
from .http import Breaker, ServiceUnavailable, short_error
from .triage import T_policy as P

# Mirrors amd/mcp/spot_mcp/domain.py suggested_action (the allowed-action enum System 2 picks from).
ACTION_TEXT = {
    "DISPATCH_TECHNICIAN": "send a technician to inspect the equipment this shift",
    "SCHEDULE_MAINTENANCE": "open a maintenance work order for the next maintenance window",
    "REINSPECT_NEXT_ROUTE": "re-check this asset on SPOT's next route and compare",
    "ESCALATE_TO_SUPERVISOR": "notify the shift supervisor now (critical asset or critical anomaly)",
    "REINSPECT_NOW": "send SPOT back to repeat the inspection now",
    "CLEAR_PATH": "have someone remove the obstacle blocking SPOT's path",
    "ADJUST_WAYPOINT": "move or re-record the waypoint / add fiducials (noisy or low-light area)",
    "RETURN_TO_DOCK": "send SPOT to the dock now",
    "CHARGE_BEFORE_NEXT_MISSION": "fully charge SPOT before the next mission",
    "CHECK_COMMS": "check radio / network coverage at this waypoint",
    "REVIEW_MISSION": "review the mission's failed actions and anomalies before signing it off",
    "MONITOR": "no action now; keep watching the trend",
    "NO_ACTION": "routine event; log only",
}


def suggested_action(category: Optional[str], priority: Optional[str], facts: Dict[str, Any]) -> str:
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


def _reasons(flag: Dict[str, Any]) -> List[Dict[str, Any]]:
    rs = flag.get("reasons") or []
    return [r for r in rs if isinstance(r, dict)]


def template_explanation(flag: Dict[str, Any], event: Optional[Dict[str, Any]] = None,
                         context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Orbit-style explanation built only from the flag's stored reasons (+ the context packet, if any)."""
    reasons = _reasons(flag)
    by_rule = {r.get("rule"): r for r in reasons}
    fired = [r for r in reasons if r.get("rule") not in ("laya_verdict", "rules_verdict", "laya_unavailable")]
    rv = by_rule.get("rules_verdict") or {}
    payload = (event or {}).get("payload") or {}
    enr = payload.get("enriched") or {}
    asset = (enr.get("asset") or {}).get("asset_name") or flag.get("asset_id")
    loc = (enr.get("location") or {}).get("location_name") or flag.get("location_id")
    env = (enr.get("location") or {}).get("environment")
    is_mission = not flag.get("action_id")
    priority = flag.get("priority") or rv.get("priority")
    category = flag.get("category") or rv.get("category") or ("mission" if is_mission else None)

    if is_mission:
        question = "Did mission %s really complete cleanly?" % flag.get("mission_id")
    else:
        question = "Does %s%s need operator attention now?" % (asset or "this asset", " at %s" % loc if loc else "")
    if flag.get("escalated"):
        answer = "Yes. Escalate (%s, %s): %s." % (priority or "?", flag.get("severity"), flag.get("title"))
    else:
        answer = "Not urgent (%s): %s." % (priority or "?", flag.get("title"))

    parts: List[str] = []
    if "laya_verdict" in by_rule:
        parts.append(by_rule["laya_verdict"].get("detail") or "")
    elif "laya_unavailable" in by_rule:
        parts.append("System 1 (Laya) was unavailable, so the deterministic rules decided alone.")
    if rv:
        parts.append("Validator (%s, mode %s): %s." % (rv.get("policy_version"), rv.get("triage_mode"),
                                                       rv.get("detail")))
    for r in fired[:4]:
        parts.append("Rule %s: %s." % (r.get("rule"), r.get("detail")))
    seen = {r.get("detail") for r in reasons}
    for f in ((context or {}).get("facts") or []):
        txt = (f.get("text") or f.get("fact")) if isinstance(f, dict) else str(f)
        if txt and txt not in seen and len(parts) < 9:
            seen.add(txt)
            parts.append(str(txt).rstrip(".") + ".")

    action = (context or {}).get("suggested_action")
    if action not in ACTION_TEXT:
        ft = (enr.get("action") or {}).get("failure_type")
        sev = (payload.get("anomaly") or {}).get("severity")
        action = suggested_action(category, priority, {"battery_pct": (enr.get("action") or {}).get("battery_pct"),
                                                       "failure_type": ft, "severity": sev, "environment": env})
    laya = by_rule.get("laya_verdict") or {}
    return {
        "question": question, "answer": answer, "reasoning": " ".join(p for p in parts if p),
        "recommended_action": "%s: %s" % (action, ACTION_TEXT.get(action, "")),
        "recommended_action_code": action, "severity_assessment": flag.get("severity"),
        "confidence": laya.get("confidence"), "model": "template", "source": "template",
        "citations": ["flag:%s" % flag.get("flag_id")] + ["policy:%s" % r.get("rule") for r in fired[:4]],
    }


def template_debrief(summary: Dict[str, Any]) -> Dict[str, Any]:
    m = summary["mission"]
    lines = ["Mission %s (%s, config %s) reported %s." % (m["mission_id"], m.get("mission_name") or "",
                                                         m.get("configuration_version"), m.get("mission_status"))]
    lines.append("%d of %d actions failed; true health %s%%." % (summary["actions_failed"], summary["actions_total"],
                                                                summary.get("true_health_pct")))
    if summary.get("status_misleading"):
        lines.append("The reported status is misleading: it hides %s." % summary.get("why"))
    if summary.get("failure_types"):
        lines.append("Failures: %s." % ", ".join("%s x%d" % (k, v) for k, v in summary["failure_types"].items()))
    if summary.get("serious_anomalies"):
        lines.append("Serious anomalies: %s." % "; ".join(
            "%s %s on %s" % (a["severity"], a["anomaly_type"], a["asset_id"]) for a in summary["serious_anomalies"]))
    lines.append("Battery %s%% -> %s%% (%s%% per action)." % (m.get("start_battery_pct"), m.get("end_battery_pct"),
                                                             summary.get("battery_drain_per_action")))
    recs = []
    if summary.get("status_misleading"):
        recs.append("REVIEW_MISSION: review failed actions and anomalies before sign-off")
    if summary.get("actions_failed"):
        recs.append("RERUN_FAILED_ACTIONS: schedule a route that repeats only the failed actions")
    if (m.get("end_battery_pct") or 100) <= P.BATTERY_LOW or summary.get("actions_at_low_battery"):
        recs.append("CHARGE_BEFORE_NEXT_MISSION: battery entered the <= %d%% cliff band" % P.BATTERY_LOW)
    return {"summary": " ".join(lines), "recommendations": recs or ["MONITOR: no action needed"],
            "model": "template", "source": "template"}


class System2Client:
    name = "system2"

    def __init__(self, base_url: Optional[str], http: httpx.AsyncClient, sink: LogSink, api_key: Optional[str] = None,
                 timeout_s: float = 60.0, breaker: Optional[Breaker] = None):
        self.base_url = (base_url or "").rstrip("/") or None
        self.http = http
        self.sink = sink
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.breaker = breaker or Breaker()

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    async def _post(self, path: str, body: Dict[str, Any], request_id: Optional[str],
                    flag_id: Optional[int] = None, action_id: Optional[str] = None) -> Dict[str, Any]:
        if not self.base_url:
            raise ServiceUnavailable(self.name, "SYSTEM2_URL is not set")
        if not self.breaker.allow():
            raise ServiceUnavailable(self.name, "circuit open after: %s" % self.breaker.last_error)
        headers = {"X-Request-ID": request_id or ""}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        t0 = time.perf_counter()
        try:
            payload = json.loads(json.dumps(body, default=str))  # datetimes/decimals from DB rows -> strings
            r = await self.http.post(self.base_url + path, json=payload, headers=headers, timeout=self.timeout_s)
            if r.status_code != 200:
                raise ServiceUnavailable(self.name, "HTTP %d: %s" % (r.status_code, r.text[:200]))
            out = r.json()
            if not isinstance(out, dict):
                raise ServiceUnavailable(self.name, "response is not a JSON object")
        except ServiceUnavailable as exc:
            self._log(False, path, t0, request_id, flag_id, action_id, error=exc.reason)
            self.breaker.failure(exc.reason)
            raise
        except (httpx.HTTPError, ValueError) as exc:
            err = short_error(exc)
            self._log(False, path, t0, request_id, flag_id, action_id, error=err)
            self.breaker.failure(err)
            raise ServiceUnavailable(self.name, err) from exc
        self.breaker.success()
        self._log(True, path, t0, request_id, flag_id, action_id, out=out)
        return out

    def _log(self, ok: bool, path: str, t0: float, request_id, flag_id, action_id, error=None, out=None) -> None:
        out = out or {}
        usage = out.get("usage") or {}
        self.sink.model_call(
            system=self.name, ok=ok, endpoint=path, request_id=request_id, flag_id=flag_id, action_id=action_id,
            model=out.get("model"), latency_ms=int((time.perf_counter() - t0) * 1000), error=error,
            tokens_per_sec=out.get("tokens_per_sec"), device=out.get("device"),
            input_tokens=out.get("input_tokens") or usage.get("prompt_tokens"),
            output_tokens=out.get("output_tokens") or usage.get("completion_tokens"),
            metadata={"service_latency_ms": out.get("latency_ms")} if out else {})

    async def assess(self, body: Dict[str, Any], request_id: Optional[str], flag_id: Optional[int],
                     action_id: Optional[str]) -> Dict[str, Any]:
        return await self._post("/v1/assess", body, request_id, flag_id, action_id)

    async def debrief(self, body: Dict[str, Any], request_id: Optional[str]) -> Dict[str, Any]:
        return await self._post("/v1/debrief", body, request_id)

    async def health(self) -> dict:
        if not self.base_url:
            return {"status": "unset"}
        try:
            r = await self.http.get(self.base_url + "/health", timeout=2.0)
        except httpx.HTTPError as exc:
            return {"status": "down", "error": short_error(exc)}
        out = {"status": "up" if r.status_code == 200 else "degraded", "http": r.status_code}
        try:
            body = r.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            # live System 2 probes its model server (contract: "Model probe"); stub mode has no model behind it
            status = body.get("model_status") if body.get("mode") == "live" else "stub"
            if status in ("up", "down", "unknown", "stub"):
                out["model_status"] = status
            if status == "down":
                out["status"] = "degraded"   # System 2 answers, but every explanation is its template
                if isinstance(body.get("model_error"), str):
                    out["error"] = "model: " + body["model_error"][:80]
        return out


def normalize_assessment(out: Dict[str, Any]) -> Dict[str, Any]:
    """Accept System 2's shape (incl. the R3 `evidence[{claim, ids}]` variant) -> the assessments columns."""
    reasoning = out.get("reasoning")
    if not reasoning and isinstance(out.get("evidence"), list):
        reasoning = " ".join("%s [%s]" % (e.get("claim"), ", ".join(e.get("ids") or []))
                             for e in out["evidence"] if isinstance(e, dict))
    rec = out.get("recommended_action")
    if isinstance(rec, dict):
        rec = "%s: %s" % (rec.get("action") or rec.get("code"), rec.get("detail") or rec.get("why") or "")
    conf = out.get("confidence")
    if isinstance(conf, str):
        conf = {"high": 0.85, "medium": 0.6, "low": 0.35}.get(conf.lower())
    return {"question": out.get("question"), "answer": out.get("answer"), "reasoning": reasoning,
            "recommended_action": rec, "severity_assessment": out.get("severity_assessment"),
            "confidence": conf, "model": out.get("model"), "latency_ms": out.get("latency_ms"),
            "tokens_per_sec": out.get("tokens_per_sec"), "insufficient_context": out.get("insufficient_context"),
            "evidence": out.get("evidence"), "source": "system2"}
