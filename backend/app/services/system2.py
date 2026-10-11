"""System 2 ("Analyst") client plus the templated fallbacks used when it is unset or down.

POST {SYSTEM2_URL}/v1/assess (docs/ARCHITECTURE.md §4) -> Orbit-style JSON:
question / answer / reasoning / recommended_action (+ severity_assessment, confidence, model, latency, tokens/s).
POST {SYSTEM2_URL}/v1/assess/stream -> the same answer as server-sent events, with `draft` snapshots of the model's
partial JSON while it generates (contract "Streaming Qwen drafts"). A draft is never an answer: only `final` is.
POST {SYSTEM2_URL}/v1/debrief -> a mission after-action summary.

The templates build the same shape deterministically from the flag's stored reasons (both verdicts and the
fired policy rules), so the operator always gets an explanation, marked `source: "template"`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from .calllog import LogSink
from .http import Breaker, ServiceUnavailable, short_error
from .triage import T_policy as P

log = logging.getLogger("backend.system2")

NO_STREAM_TTL_S = 300.0       # after a 404/405 on /v1/assess/stream (an older System 2), use /v1/assess this long
STREAM_BUSY_STATUS = 503      # System 2's stream slots are all taken: use /v1/assess for this call only
DRAFT_TEXT_KEYS = ("answer", "recommended_action", "confidence")
_EOL = re.compile(r"\r\n|\r|\n")

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


class StreamNotSupported(Exception):
    """System 2 answered /v1/assess/stream with 404/405 (an older build) or 503 (every stream slot busy): the caller
    uses /v1/assess instead."""


class SSEParser:
    """Incremental server-sent-events parser: feed text chunks, get (event, data) pairs.

    Lines end in LF, CRLF or CR (a CR at the end of a chunk waits for the next chunk); `:` lines are comments;
    several `data:` lines join with a newline; a blank line dispatches. `close()` flushes a last event that had no
    trailing blank line (lenient: a server that closes right after its final event still counts)."""

    def __init__(self):
        self._buf = ""
        self._event: Optional[str] = None
        self._data: List[str] = []

    def feed(self, text: str) -> List[Tuple[str, str]]:
        self._buf += text
        out: List[Tuple[str, str]] = []
        while True:
            m = _EOL.search(self._buf)
            if m is None or (m.group() == "\r" and m.end() == len(self._buf)):
                break
            line, self._buf = self._buf[:m.start()], self._buf[m.end():]
            ev = self._line(line)
            if ev is not None:
                out.append(ev)
        return out

    def close(self) -> List[Tuple[str, str]]:
        out = self.feed("\n") if self._buf else []
        ev = self._line("")
        return out + ([ev] if ev is not None else [])

    def _line(self, line: str) -> Optional[Tuple[str, str]]:
        if line == "":
            ev = (self._event or "message", "\n".join(self._data)) if self._data else None
            self._event, self._data = None, []
            return ev
        if line.startswith(":"):
            return None
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "event":
            self._event = value
        elif name == "data":
            self._data.append(value)
        return None


def evidence_reasoning(evidence: Any) -> str:
    """The evidence claims joined the way `normalize_assessment` builds `reasoning`: "<claim> [<ids>]" ..."""
    return " ".join("%s [%s]" % (e.get("claim"), ", ".join(e.get("ids") or []))
                    for e in (evidence if isinstance(evidence, list) else []) if isinstance(e, dict))


def sanitize_draft_fields(fields: Any) -> Dict[str, Any]:
    """A draft snapshot reduced to {evidence: [{claim, ids}], answer, recommended_action, confidence}: only those
    keys, only strings, `ids` only strings. Keys that are absent (not started yet) stay absent."""
    out: Dict[str, Any] = {}
    if not isinstance(fields, dict):
        return out
    ev = fields.get("evidence")
    if isinstance(ev, list):
        items = []
        for e in ev:
            if not isinstance(e, dict):
                continue
            claim = e.get("claim") if isinstance(e.get("claim"), str) else ""
            ids = [i for i in e.get("ids") if isinstance(i, str)] if isinstance(e.get("ids"), list) else []
            if claim or ids:
                items.append({"claim": claim, "ids": ids})
        out["evidence"] = items
    for k in DRAFT_TEXT_KEYS:
        if isinstance(fields.get(k), str):
            out[k] = fields[k]
    return out


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
        self._no_stream_until = 0.0      # monotonic time until which /v1/assess/stream is known to be missing

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

    def _log(self, ok: bool, path: str, t0: float, request_id, flag_id, action_id, error=None, out=None,
             extra: Optional[Dict[str, Any]] = None) -> None:
        out = out or {}
        usage = out.get("usage") or {}
        metadata = {"service_latency_ms": out.get("latency_ms")} if out else {}
        if extra:
            metadata.update(extra)
        self.sink.model_call(
            system=self.name, ok=ok, endpoint=path, request_id=request_id, flag_id=flag_id, action_id=action_id,
            model=out.get("model"), latency_ms=int((time.perf_counter() - t0) * 1000), error=error,
            tokens_per_sec=out.get("tokens_per_sec"), device=out.get("device"),
            input_tokens=out.get("input_tokens") or usage.get("prompt_tokens"),
            output_tokens=out.get("output_tokens") or usage.get("completion_tokens"),
            metadata=metadata)

    async def assess(self, body: Dict[str, Any], request_id: Optional[str], flag_id: Optional[int],
                     action_id: Optional[str]) -> Dict[str, Any]:
        return await self._post("/v1/assess", body, request_id, flag_id, action_id)

    async def assess_stream(self, body: Dict[str, Any], request_id: Optional[str], flag_id: Optional[int],
                            action_id: Optional[str],
                            on_draft: Optional[Callable[[Dict[str, Any]], None]] = None
                            ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """POST /v1/assess/stream: calls `on_draft({call, tokens, fields})` per `draft` event and returns
        (the `final` response, exactly the /v1/assess body; {stream, drafts, cache, image, mode}).

        Same failure semantics as `assess` (breaker, one app.model_calls row, ServiceUnavailable on a transport or
        HTTP error, an `error` event or a stream without `final`). A 404/405 raises StreamNotSupported (nothing
        logged, breaker untouched) and is remembered for NO_STREAM_TTL_S, so the caller can use `assess`; a 503
        (System 2's stream slots are busy) raises it too, for this call only. A 200
        `application/json` reply (a proxy or build that does not stream) is read as the final response, no drafts.
        Cancellation (client disconnect, the caller's deadline) logs an `ok=False, error="cancelled"` row without
        touching the breaker, then propagates."""
        path = "/v1/assess/stream"
        if not self.base_url:
            raise ServiceUnavailable(self.name, "SYSTEM2_URL is not set")
        if time.monotonic() < self._no_stream_until:
            raise StreamNotSupported("System 2 has no /v1/assess/stream (remembered)")
        if not self.breaker.allow():
            raise ServiceUnavailable(self.name, "circuit open after: %s" % self.breaker.last_error)
        headers = {"X-Request-ID": request_id or "", "Accept": "text/event-stream"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        meta: Dict[str, Any] = {"stream": True, "drafts": 0, "cache": None, "image": None}
        t0 = time.perf_counter()
        try:
            payload = json.loads(json.dumps(body, default=str))  # datetimes/decimals from DB rows -> strings
            async with self.http.stream("POST", self.base_url + path, json=payload, headers=headers,
                                        timeout=self.timeout_s) as r:
                if r.status_code in (404, 405):
                    self._no_stream_until = time.monotonic() + NO_STREAM_TTL_S
                    raise StreamNotSupported("System 2 answered /v1/assess/stream with HTTP %d" % r.status_code)
                if r.status_code == STREAM_BUSY_STATUS:     # not remembered: the next call may get a slot
                    raise StreamNotSupported("System 2 streams are busy (HTTP 503)")
                if r.status_code != 200:
                    await r.aread()
                    raise ServiceUnavailable(self.name, "HTTP %d: %s" % (r.status_code, r.text[:200]))
                ctype = r.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if ctype == "application/json":    # not streamed: the body is the /v1/assess answer itself
                    await r.aread()
                    out = r.json()
                    if not isinstance(out, dict):
                        raise ServiceUnavailable(self.name, "JSON reply is not an object")
                    meta["stream"] = False
                else:
                    out = await self._read_stream(r, on_draft, meta)
        except StreamNotSupported:
            raise
        except asyncio.CancelledError:
            # the client left or the caller's deadline passed: not System 2's fault, so the breaker is untouched
            self._log(False, path, t0, request_id, flag_id, action_id, error="cancelled", extra=meta)
            raise
        except ServiceUnavailable as exc:
            self._log(False, path, t0, request_id, flag_id, action_id, error=exc.reason, extra=meta)
            self.breaker.failure(exc.reason)
            raise
        except (httpx.HTTPError, ValueError) as exc:
            err = short_error(exc)
            self._log(False, path, t0, request_id, flag_id, action_id, error=err, extra=meta)
            self.breaker.failure(err)
            raise ServiceUnavailable(self.name, err) from exc
        self.breaker.success()
        self._log(True, path, t0, request_id, flag_id, action_id, out=out, extra=meta)
        return out, meta

    async def _read_stream(self, r: httpx.Response, on_draft, meta: Dict[str, Any]) -> Dict[str, Any]:
        parser = SSEParser()
        async for chunk in r.aiter_text():
            for event, data in parser.feed(chunk):
                out = self._on_event(event, data, on_draft, meta)
                if out is not None:
                    return out          # `final` is the last event: stop reading, close the response
        for event, data in parser.close():
            out = self._on_event(event, data, on_draft, meta)
            if out is not None:
                return out
        raise ServiceUnavailable(self.name, "stream ended without a final event")

    def _on_event(self, event: str, data: str, on_draft, meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            obj = json.loads(data)
        except ValueError:
            if event in ("final", "error"):
                raise ServiceUnavailable(self.name, "%s event is not JSON" % event) from None
            return None
        if not isinstance(obj, dict):
            obj = {}
        if event == "start":
            meta["mode"] = obj.get("mode")
        elif event == "draft":
            meta["drafts"] += 1
            if on_draft is not None:
                try:
                    on_draft(obj)
                except Exception:  # noqa: BLE001 - a draft is cosmetic; never fail the assessment over it
                    log.exception("draft callback failed")
        elif event == "final":
            response = obj.get("response")
            if not isinstance(response, dict):
                raise ServiceUnavailable(self.name, "final event has no response object")
            meta["cache"], meta["image"] = obj.get("cache"), obj.get("image")
            return response
        elif event == "error":
            raise ServiceUnavailable(self.name, "stream error: %s" % str(obj.get("detail") or "no detail")[:200])
        return None

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
        reasoning = evidence_reasoning(out["evidence"])
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
