"""Flag workflows: context packet (MCP, else SQL), explanation (System 2, else template), operator ack.

Fallbacks (the demo path never 500s):
- MCP down          -> /context returns a SQL-built minimal packet (`source: "sql_fallback"`) that System 2 accepts:
                       `asset_history` is a list of citable objects, `allowed_actions`/`suggested_action`/
                       `citation_ids` are set, and the MCP notice goes to `notes` (System 2 treats any `gaps` entry
                       as insufficient context, so only failed SQL lookups go there).
- SYSTEM2 unset/down -> /explain returns a templated Orbit-style explanation (`source: "template"`).
- embed down         -> the ack is stored; the operator_note document is stored without a vector (full-text search
                       finds it, `amd/embed/ingest.py --kinds operator_note` backfills it; `rag_indexed: false`).
- Request deadline (REQUEST_DEADLINE_S, audit M4): /context and /explain each finish within it, whatever the
  per-call timeouts; past it they return the same fallbacks, with `fallback_reason: "deadline: ..."`.
- Template explanations are stored with state TEMPLATE (audit M10): not final, so /explain retries System 2 when
  SYSTEM2_URL is set, and they are never indexed as `explanation` RAG docs.
- ORCHESTRATOR_URL set (docs/backend-services-contract.md "Orchestrator path"): /explain asks the LangGraph
  orchestrator first; when it is down or had no context packet, the path above runs unchanged. Unset: no change.
- /explain/stream (contract "Streaming Qwen drafts") runs the very same code (`_explain`) with an `emit` hook: it
  reports stages, relays System 2's drafts (via /v1/assess/stream; a 404/405 falls back to /v1/assess inside the
  same deadline) and ends with `done` = exactly what /explain returns. Drafts are never stored, cached or indexed.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

import anyio
import psycopg

from ..db.pool import DatabaseUnavailable
from .embed import vec_literal
from .http import ServiceUnavailable, short_error
from .system2 import (ALLOWED_ACTIONS, StreamNotSupported, evidence_reasoning, normalize_assessment,
                      sanitize_draft_fields, suggested_action, template_explanation)
from .triage import T_policy as P
from .validator import LAYA_UNAVAILABLE_DETAIL

log = logging.getLogger("backend.flags")

Emit = Callable[[str, Dict[str, Any]], None]
KEEPALIVE = ("keep-alive", None)     # yielded by explain_stream after STREAM_KEEPALIVE_S of silence: an SSE comment
STREAM_KEEPALIVE_S = 15.0


def draft_event(d: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """System 2's `draft` {call, tokens, fields} -> the backend's `draft` {call, fields, reasoning}."""
    if not isinstance(d, dict):
        return None
    fields = sanitize_draft_fields(d.get("fields"))
    call = d.get("call")
    return {"call": call if isinstance(call, int) and not isinstance(call, bool) and call >= 1 else 1,
            "fields": fields, "reasoning": evidence_reasoning(fields.get("evidence"))}


class FlagNotFound(LookupError):
    pass


class RequestDeadlineExceeded(TimeoutError):
    """The flag row itself could not be read within REQUEST_DEADLINE_S (main.py -> 504)."""


EXPLAIN_CONTEXT_SHARE = 0.4      # /explain: the context step gets at most this share of the deadline
# Fields copied from the orchestrator's /v1/assess response (docs/orchestrator-contract.md "Response").
ORCHESTRATOR_ANSWER_KEYS = ("question", "answer", "reasoning", "recommended_action", "recommended_action_code",
                            "severity_assessment", "confidence", "model", "latency_ms", "tokens_per_sec",
                            "insufficient_context", "evidence", "citations", "fallback_reason")
ORCHESTRATOR_TRACE_KEYS = ("route", "route_reasons", "trace", "system1", "vision", "elapsed_ms")
EXPLANATION_SOURCES = ("system2", "system2_template", "template", "system1")   # only system2 is stored DONE
SYSTEM2_TEMPLATE_PATHS = ("fallback", "insufficient_context")   # System 2 paths that are its template, not the model
STATE_SYSTEM2, STATE_TEMPLATE = "DONE", "TEMPLATE"
META_RULES = ("laya_verdict", "rules_verdict", "laya_unavailable")   # verdicts in flags.reasons, not policy rules
SEVERITY_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


class _Deadline:
    """An absolute deadline on the event loop clock."""

    def __init__(self, seconds: float):
        self.total = seconds
        self.at = asyncio.get_running_loop().time() + seconds

    def remaining(self, reserve: float = 0.0) -> float:
        return max(0.0, self.at - asyncio.get_running_loop().time() - reserve)

    def reserve(self) -> float:
        """Time kept back for building and storing the fallback."""
        return min(3.0, 0.1 * self.total)


def _compact(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, [], {}, "")}


def collect_ids(obj: Any) -> List[str]:
    """Every citable id in the packet, in order (like the MCP server's _collect_ids). An object that carries
    nothing but its id is skipped: System 2 would find no content for it and treat the packet as insufficient."""
    ids: List[str] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            if isinstance(x.get("id"), str) and x["id"] not in ids and any(k != "id" for k in _compact(x)):
                ids.append(x["id"])
            for k, v in x.items():
                if k != "citation_ids":
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(obj)
    return ids


def battery_band(pct: Optional[float]) -> Optional[str]:
    if pct is None:
        return None
    return "<=25" if pct <= 25 else "26-40" if pct <= 40 else "41-60" if pct <= 60 else "61-80" if pct <= 80 else "81+"


def band_check(r: Dict[str, Any], status: Optional[str]) -> Dict[str, Any]:
    mt, v = r.get("measurement_type"), r.get("value")
    lo, hi = P.SAFE_BANDS.get(mt, (None, None))
    if v is None:
        return {"trust": "none", "safe_band": [lo, hi]}
    plo, phi = P.PLAUSIBLE.get(mt, (float("-inf"), float("inf")))
    if r.get("quality") != "GOOD" or status == "FAILED" or not (plo <= v <= phi):
        return {"trust": "untrusted", "safe_band": [lo, hi]}
    out = (lo is not None and v < lo) or (hi is not None and v > hi)
    return {"trust": "trusted", "in_safe_band": not out, "safe_band": [lo, hi]}


class FlagService:
    def __init__(self, repo, mcp, system2, embed, sink, deadline_s: float = 45.0, orchestrator=None):
        self.repo = repo
        self.mcp = mcp
        self.system2 = system2
        self.embed = embed
        self.sink = sink
        self.deadline_s = deadline_s
        self.orchestrator = orchestrator
        self.keepalive_s = STREAM_KEEPALIVE_S

    @property
    def orchestrated(self) -> bool:
        return self.orchestrator is not None and self.orchestrator.configured

    async def load(self, flag_id: int, deadline: Optional[_Deadline] = None) -> Dict[str, Any]:
        if deadline is None:
            row = await self.repo.get_flag(flag_id)
        else:
            try:
                row = await asyncio.wait_for(self.repo.get_flag(flag_id), deadline.remaining())
            except asyncio.TimeoutError:
                raise RequestDeadlineExceeded("flag %s could not be read within REQUEST_DEADLINE_S=%gs"
                                              % (flag_id, deadline.total)) from None
        if row is None:
            raise FlagNotFound(flag_id)
        return row

    # ── context ─────────────────────────────────────────────────────────────────────────────────
    async def context(self, flag_id: int, request_id: Optional[str]) -> Dict[str, Any]:
        deadline = _Deadline(self.deadline_s)
        row = await self.load(flag_id, deadline)
        return await self._context(row, flag_id, request_id, deadline, deadline.remaining(deadline.reserve()))

    async def _context(self, row: Dict[str, Any], flag_id: int, request_id: Optional[str], deadline: _Deadline,
                       mcp_budget_s: float) -> Dict[str, Any]:
        """MCP packet within `mcp_budget_s`, else the SQL packet within what is left of `deadline`.
        Why MCP was skipped goes to `notes`, not `gaps`: System 2 reads any gap as insufficient context, and the SQL
        packet is complete evidence by itself."""
        try:
            packet = await asyncio.wait_for(
                self.mcp.call_tool("get_flag_context", {"flag_id": flag_id}, request_id=request_id), mcp_budget_s)
            packet["source"] = "mcp"
            return packet
        except ServiceUnavailable as exc:
            note = "MCP unavailable (%s): SQL-only packet, no RAG retrieval" % exc.reason
            reason = None
        except asyncio.TimeoutError:
            reason = "deadline: MCP did not answer within %.1fs (REQUEST_DEADLINE_S=%gs)" % (mcp_budget_s,
                                                                                              deadline.total)
            note = reason + ": SQL-only packet, no RAG retrieval"
        packet = await self.minimal_packet(row, budget_s=max(0.5, deadline.remaining()))
        packet.setdefault("notes", []).insert(0, note)
        if reason:
            packet["fallback_reason"] = reason
        packet["question"] = packet.pop("question")  # the question always comes last (as in the MCP packet)
        return packet

    async def minimal_packet(self, row: Dict[str, Any], budget_s: Optional[float] = None) -> Dict[str, Any]:
        """The SQL-built fallback packet: flag, event facts, System 1 answers, reliability, asset history, the
        allowed actions for the flag's category and the citable ids (the System 2 packet contract,
        docs/backend-services-contract.md). With `budget_s`, SQL lookups not finished in time are skipped and
        listed in `gaps`."""
        f = row["flag"]
        gaps: List[str] = []
        notes: List[str] = []
        packet: Dict[str, Any] = {"packet_version": "1-min", "source": "sql_fallback"}
        reasons = [r for r in (f.get("reasons") or []) if isinstance(r, dict)]
        # Rows written before the stable laya_unavailable text carry raw connection errors: never send those
        # to System 2 as evidence.
        packet["flag"] = _compact({
            "id": "flag:%s" % f["flag_id"], "title": f.get("title"), "severity": f.get("severity"),
            "score": f.get("score"), "status": f.get("status"), "escalated": f.get("escalated"),
            "priority": f.get("priority"), "category": f.get("category"), "mission_id": f.get("mission_id"),
            "action_id": f.get("action_id"), "asset_id": f.get("asset_id"), "location_id": f.get("location_id"),
            "created_at": f.get("created_at"),
            "reasons": [_compact({"rule": r.get("rule"), "detail": LAYA_UNAVAILABLE_DETAIL
                                  if r.get("rule") == "laya_unavailable" else r.get("detail")})
                        for r in reasons[:8]]})
        acks = row.get("acks") or []
        if acks:
            packet["flag"]["operator_acks"] = [_compact({"id": "note:%s" % k.get("ack_id"), "action": k.get("action"),
                                                         "note": k.get("note"), "operator": k.get("operator")})
                                               for k in acks[:5]]
        facts: List[str] = []
        action_id, mission_id = f.get("action_id"), f.get("mission_id")
        try:
            await asyncio.wait_for(self._sql_lookups(packet, f, facts, gaps), budget_s)
        except asyncio.TimeoutError:
            gaps.append("some SQL lookups did not finish within the request deadline")
        for r in reasons:
            if r.get("rule") not in META_RULES and r.get("detail"):
                facts.insert(0, r["detail"])
        packet["facts"] = [{"id": "F%d" % (i + 1), "text": t} for i, t in enumerate(facts[:8])]
        self._actions(packet, f, reasons, notes)
        packet["insufficient_context"] = "event" not in packet
        if gaps:
            packet["gaps"] = gaps
        if notes:
            packet["notes"] = notes
        packet["citation_ids"] = collect_ids(packet)
        if action_id:
            q = "Does %s need operator attention now, and what should the operator do?" % (
                ((packet.get("event") or {}).get("asset") or {}).get("name") or f.get("asset_id") or "this asset")
        else:
            q = "Did mission %s really complete cleanly, and what should the operator review?" % mission_id
        packet["question"] = q
        return packet

    @staticmethod
    def _actions(packet: Dict[str, Any], f: Dict[str, Any], reasons: List[Dict[str, Any]], notes: List[str]) -> None:
        """allowed_actions for the flag's category (the MCP server's ALLOWED_ACTIONS; the mission set for mission
        flags) and the validator's suggested_action, picked from the same facts the MCP server uses."""
        rv = next((r for r in reasons if r.get("rule") == "rules_verdict"), {})
        is_action = bool(f.get("action_id"))
        category = f.get("category") or rv.get("category") or (None if is_action else "mission")
        if not is_action and category not in ALLOWED_ACTIONS:
            category = "mission"
        if category not in ALLOWED_ACTIONS:
            packet["allowed_actions"] = {a: m for acts in ALLOWED_ACTIONS.values() for a, m in acts.items()}
            notes.append("category unknown; all actions allowed")
            return
        packet["allowed_actions"] = dict(ALLOWED_ACTIONS[category])
        ev = packet.get("event") or {}
        worst = max((n.get("severity") for n in ev.get("anomalies") or []), key=lambda s: SEVERITY_RANK.get(s, 0),
                    default=None)
        code = suggested_action(category, f.get("priority") or rv.get("priority"), {
            "battery_pct": ev.get("battery_pct"), "failure_type": ev.get("failure_type"), "severity": worst,
            "environment": (ev.get("location") or {}).get("environment")})
        if code in packet["allowed_actions"]:
            packet["suggested_action"] = code

    async def _sql_lookups(self, packet: Dict[str, Any], f: Dict[str, Any], facts: List[str],
                           gaps: List[str]) -> None:
        """The SQL part of the fallback packet; fills `packet`, `facts` and `gaps` in place."""
        action_id, mission_id = f.get("action_id"), f.get("mission_id")
        try:
            if action_id:
                b = await self.repo.action_bundle(action_id)
                if b:
                    a, s, l = b["action"], b.get("asset") or {}, b.get("location") or {}
                    readings = [_compact({"id": "reading:%s" % r.get("reading_id"), "measurement_type":
                                          r.get("measurement_type"), "value": r.get("value"), "unit": r.get("unit"),
                                          "quality": r.get("quality"), **band_check(r, a.get("status"))})
                                for r in (b.get("readings") or [])[:3]]
                    anomalies = [_compact({"id": "anomaly:%s" % n.get("anomaly_id"), "anomaly_type":
                                           n.get("anomaly_type"), "severity": n.get("severity"),
                                           "description": n.get("description")}) for n in (b.get("anomalies") or [])[:3]]
                    packet["event"] = _compact({
                        "id": "action:%s" % action_id, "mission_id": a.get("mission_id"), "sequence": a.get("sequence"),
                        "timestamp": a.get("timestamp"), "action_type": a.get("action_type"), "status": a.get("status"),
                        "failure_type": a.get("failure_type"), "battery_pct": a.get("battery_pct"),
                        "configuration_version": a.get("configuration_version"),
                        "asset": _compact({"id": "asset:%s" % s.get("asset_id"), "name": s.get("asset_name"),
                                           "type": s.get("asset_type"), "criticality": s.get("criticality")})
                        if s.get("asset_id") else None,
                        "location": _compact({"id": "location:%s" % l.get("location_id"), "name": l.get("location_name"),
                                              "zone": l.get("zone_type"), "environment": l.get("environment")})
                        if l.get("location_id") else None,
                        "readings": readings, "anomalies": anomalies})
                    rel = await self.repo.reliability(None, {"location_id": a.get("location_id"),
                                                             "action_type": a.get("action_type")})
                    if rel:
                        r0 = rel[0]
                        packet["reliability"] = {"scope": "%s at %s" % (a.get("action_type"), a.get("location_id")),
                                                 "n": r0["n"], "success_rate_pct": r0["success_rate_pct"],
                                                 "ci95_pct": r0["ci95_pct"]}
                        facts.append("%s at %s succeeds %s%% of the time (n=%d)" % (
                            a.get("action_type"), a.get("location_id"), r0["success_rate_pct"], r0["n"]))
                    band = battery_band(a.get("battery_pct"))
                    if band:
                        rb = await self.repo.reliability(None, {"battery_band": band})
                        if rb:
                            packet["battery_band"] = {"band": band, "n": rb[0]["n"],
                                                      "success_rate_pct": rb[0]["success_rate_pct"]}
                            facts.append("actions at battery %s succeed %s%% of the time" %
                                         (band, rb[0]["success_rate_pct"]))
                else:
                    gaps.append("action %s not found" % action_id)
            elif mission_id:
                m = await self.repo.mission(mission_id)
                if m:
                    packet["event"] = {"mission": _compact({"id": "mission:%s" % mission_id, **{
                        k: m.get(k) for k in ("mission_name", "configuration_version", "start_time", "end_time",
                                              "start_battery_pct", "end_battery_pct", "mission_status")}})}
                    packet["event"]["mission"]["failure_types"] = await self.repo.failure_type_counts(mission_id)
            if f.get("asset_id"):
                # A list of citable objects (System 2 rejects any other shape with a 422). Robot time is bounded to
                # before this flag and its own action is left out (Repo.asset_history).
                hist = await self.repo.asset_history(f["asset_id"], f["flag_id"])
                items = [_compact({"id": "anomaly:%s" % n["anomaly_id"], "anomaly_type": n.get("anomaly_type"),
                                   "severity": n.get("severity"), "mission_id": n.get("mission_id")})
                         for n in hist.get("anomalies") or [] if n.get("anomaly_id")]
                items += [_compact({"id": "flag:%s" % p["flag_id"], "detail": p.get("title"),
                                    "severity": p.get("severity"), "status": p.get("status"), "note": p.get("notes")})
                          for p in hist.get("prior_flags") or [] if p.get("flag_id") is not None]
                if items:
                    packet["asset_history"] = items
            preds = await self.repo.predictions(action_id, mission_id)
            seen, s1 = set(), []
            for p in preds:
                key = (p["event_kind"], p["question"])
                if key in seen:
                    continue
                seen.add(key)
                s1.append({"id": "s1:%s" % p["prediction_id"], "when": p["event_kind"], "q": p["question"],
                           "answer": p["answer"], "p": (p.get("probs") or {}).get(p["answer"])})
            if s1:
                packet["system1"] = s1[:8]
        except (DatabaseUnavailable, psycopg.Error) as exc:
            gaps.append("some SQL lookups failed: %s" % short_error(exc))

    # ── explain ─────────────────────────────────────────────────────────────────────────────────
    async def explain(self, flag_id: int, request_id: Optional[str], refresh: bool = False,
                      image: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        deadline = _Deadline(self.deadline_s)
        row = await self.load(flag_id, deadline)
        return await self._explain(row, flag_id, request_id, refresh, image, deadline)

    async def explain_stream(self, flag_id: int, request_id: Optional[str],
                             refresh: bool = False) -> AsyncIterator[Tuple[str, Any]]:
        """The flag row is read here, before any event (FlagNotFound / RequestDeadlineExceeded / database errors
        raise as for /explain, so the router answers them as JSON). Returns the event iterator: `start`, `stage`
        (0..n), `draft` (0..n), then `done` {explanation} (exactly explain()'s result for the same call) or
        `error` {error, detail}; KEEPALIVE after `keepalive_s` without an event."""
        deadline = _Deadline(self.deadline_s)
        row = await self.load(flag_id, deadline)
        return self._explain_events(row, flag_id, request_id, refresh, deadline)

    async def _explain_events(self, row: Dict[str, Any], flag_id: int, request_id: Optional[str], refresh: bool,
                              deadline: _Deadline) -> AsyncIterator[Tuple[str, Any]]:
        q: asyncio.Queue = asyncio.Queue()
        storing = asyncio.Event()

        def emit(event: str, data: Dict[str, Any]) -> None:
            if event == "stage" and data.get("stage") == "storing":
                storing.set()
            q.put_nowait((event, data))

        async def produce() -> None:
            try:
                out = await self._explain(row, flag_id, request_id, refresh, None, deadline, emit)
                q.put_nowait(("done", {"explanation": out}))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the stream must end with an `error` event, never a broken body
                log.exception("explain stream for flag %s failed", flag_id)
                q.put_nowait(("error", {"error": "internal_error", "detail": short_error(exc)}))
            finally:
                q.put_nowait(None)

        yield "start", {"flag_id": flag_id, "request_id": request_id}
        task = asyncio.ensure_future(produce())
        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), self.keepalive_s)
                except asyncio.TimeoutError:
                    yield KEEPALIVE
                    continue
                if item is None:
                    break
                yield item
        finally:
            if not task.done():
                # The client went away. Before storing: stop (System 2 still finishes and caches its answer, so a
                # retry is fast). While storing: let the write finish, so the row is never half-done.
                with anyio.CancelScope(shield=True):
                    if not storing.is_set():
                        task.cancel()
                    try:
                        await task
                    except BaseException:  # noqa: BLE001 - CancelledError, or whatever the producer was raising
                        pass

    async def _explain(self, row: Dict[str, Any], flag_id: int, request_id: Optional[str], refresh: bool,
                       image: Optional[Dict[str, Any]], deadline: _Deadline,
                       emit: Optional[Emit] = None) -> Dict[str, Any]:
        """The one /explain code path. `emit` (streaming only) gets `stage` and `draft` events; without it System 2
        is called with plain /v1/assess, exactly as before."""
        stage: Emit = emit or (lambda event, data: None)
        warning = None
        if image and not self.orchestrated:
            warning, image = "image ignored: ORCHESTRATOR_URL is not set", None
        cached = row.get("assessment")
        if cached and not refresh and not image:
            # A System 2 explanation (DONE, model not 'template') is final. A template (state TEMPLATE, or a
            # pre-M10 DONE row with model 'template') is reused only while neither System 2 nor the orchestrator
            # is configured.
            is_template = cached.get("state") == STATE_TEMPLATE or cached.get("model") in ("template", None)
            retry = self.system2.configured or self.orchestrated
            if cached.get("state") in (STATE_SYSTEM2, STATE_TEMPLATE) and not (is_template and retry):
                stage("stage", {"stage": "cached"})
                # The stored source (assessments.source, migration 014) keeps system2_template apart from the
                # backend's template; rows from before 014 fall back to the state.
                source = cached.get("source")
                if source not in EXPLANATION_SOURCES or (source == "system2") == is_template:
                    source = "template" if is_template else "system2"
                out = {**cached, "source": source, "cached": True}
                if warning:
                    out["warning"] = warning
                return out
        f = row["flag"]
        reserve = deadline.reserve()
        orchestrator_error = None
        if self.orchestrated:
            stage("stage", {"stage": "system2"})        # the orchestrator's graph ends in System 2; no drafts
            result, orchestrator_error = await self._via_orchestrator(f, flag_id, request_id, refresh, image,
                                                                      deadline, reserve)
            if result is not None:
                stage("stage", {"stage": "storing"})
                return await self._store(result, f, flag_id, request_id, deadline, warning)
        stage("stage", {"stage": "context"})
        ctx_budget = min(deadline.remaining(reserve), EXPLAIN_CONTEXT_SHARE * deadline.total)
        context = await self._context(row, flag_id, request_id, deadline, ctx_budget)
        result: Dict[str, Any]
        stage("stage", {"stage": "system2"})
        s2_budget = deadline.remaining(reserve)
        try:
            body = {"flag_id": flag_id, "request_id": request_id, "flag": f, "event": row.get("event"),
                    "reasons": [r.get("detail") for r in (f.get("reasons") or []) if isinstance(r, dict)],
                    "context": context}
            out = await asyncio.wait_for(self._assess(body, request_id, flag_id, f.get("action_id"), emit),
                                         s2_budget)
            result = normalize_assessment(out, default_severity=f.get("severity"))
            if not result.get("answer"):
                raise ServiceUnavailable("system2", "response had no answer")
            if out.get("path") in SYSTEM2_TEMPLATE_PATHS or out.get("model") == "template":
                # System 2's own checked template, not a model answer: stored TEMPLATE, never indexed (audit A-8/P-5)
                result["source"] = "system2_template"
                result["fallback_reason"] = "System 2 answered with its checked template (path %s, model %s)" % (
                    out.get("path"), out.get("model"))
                result["latency_ms"] = None    # no model generated it (System 2 reports 0 for its template)
        except ServiceUnavailable as exc:
            result = template_explanation(f, row.get("event"), context)
            result["fallback_reason"] = exc.reason
        except asyncio.TimeoutError:
            result = template_explanation(f, row.get("event"), context)
            result["fallback_reason"] = "deadline: System 2 did not answer within %.1fs (REQUEST_DEADLINE_S=%gs)" % (
                s2_budget, deadline.total)
        result["context_source"] = context.get("source")
        if orchestrator_error:
            result["orchestrator"] = {"error": orchestrator_error}
        stage("stage", {"stage": "storing"})
        return await self._store(result, f, flag_id, request_id, deadline, warning)

    async def _assess(self, body: Dict[str, Any], request_id: Optional[str], flag_id: int,
                      action_id: Optional[str], emit: Optional[Emit]) -> Dict[str, Any]:
        """System 2's answer: /v1/assess, or (streaming) /v1/assess/stream relaying its drafts, falling back to
        /v1/assess when System 2 is an older build without the stream. The caller's deadline covers both calls."""
        if emit is None:
            return await self.system2.assess(body, request_id=request_id, flag_id=flag_id, action_id=action_id)

        def on_draft(d: Dict[str, Any]) -> None:
            ev = draft_event(d)
            if ev is not None:
                emit("draft", ev)
        try:
            out, _meta = await self.system2.assess_stream(body, request_id=request_id, flag_id=flag_id,
                                                          action_id=action_id, on_draft=on_draft)
            return out
        except StreamNotSupported:
            return await self.system2.assess(body, request_id=request_id, flag_id=flag_id, action_id=action_id)

    async def _via_orchestrator(self, f: Dict[str, Any], flag_id: int, request_id: Optional[str], refresh: bool,
                                image: Optional[Dict[str, Any]], deadline: _Deadline,
                                reserve: float) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        """(explanation, None) from the orchestrator's /v1/assess, or (None, why) to run the existing path."""
        budget = deadline.remaining(reserve)
        if budget < 0.5:
            return None, "deadline: no time left for the orchestrator"
        body: Dict[str, Any] = {"flag_id": flag_id, "request_id": request_id, "refresh": refresh}
        if image:
            body.update({k: v for k, v in image.items() if k in ("image_b64", "image_kind") and v})
        try:
            out = await asyncio.wait_for(self.orchestrator.assess(body, request_id=request_id, flag_id=flag_id,
                                                                  action_id=f.get("action_id"), timeout_s=budget),
                                         budget)
        except ServiceUnavailable as exc:
            return None, exc.reason
        except asyncio.TimeoutError:
            return None, "deadline: the orchestrator did not answer within %.1fs (REQUEST_DEADLINE_S=%gs)" % (
                budget, deadline.total)
        if out.get("context_source") in (None, "none"):
            return None, "orchestrator had no context packet (%s)" % (out.get("fallback_reason") or "no reason")
        if not out.get("answer"):
            return None, "orchestrator response had no answer"
        result = {k: out.get(k) for k in ORCHESTRATOR_ANSWER_KEYS}
        result["source"] = out.get("source") if out.get("source") in EXPLANATION_SOURCES else "template"
        result["context_source"] = out.get("context_source")
        result["orchestrator"] = {k: out.get(k) for k in ORCHESTRATOR_TRACE_KEYS}
        return result, None

    async def _store(self, result: Dict[str, Any], f: Dict[str, Any], flag_id: int, request_id: Optional[str],
                     deadline: _Deadline, warning: Optional[str] = None) -> Dict[str, Any]:
        """Store the explanation (DONE only for System 2, else TEMPLATE) with its source and fallback_reason, and
        index System 2 ones as RAG docs."""
        state = STATE_SYSTEM2 if result.get("source") == "system2" else STATE_TEMPLATE
        if not result.get("severity_assessment"):
            result["severity_assessment"] = f.get("severity")
        if warning:
            result["warning"] = warning
        stored: Dict[str, Any] = {}
        try:
            stored = await asyncio.wait_for(self.repo.upsert_assessment(flag_id, result, state=state),
                                            max(0.5, deadline.remaining()))
            result["assessment_id"] = stored.get("assessment_id")
        except (DatabaseUnavailable, psycopg.Error) as exc:
            result["warning"] = "; ".join(w for w in (warning, "assessment not stored: %s" % short_error(exc)) if w)
        except asyncio.TimeoutError:
            result["warning"] = "; ".join(w for w in (warning, "assessment not stored: the request deadline passed")
                                          if w)
        if result.get("assessment_id") and state == STATE_SYSTEM2:
            self.sink.spawn(self._index_explanation(result, f, request_id, stored.get("completed_at")))
        result["cached"] = False
        return result

    async def _index_explanation(self, a: Dict[str, Any], f: Dict[str, Any], request_id: Optional[str],
                                 ts: Any = None) -> None:
        content = "Q: %s A: %s Why: %s Action: %s" % (a.get("question"), a.get("answer"), a.get("reasoning"),
                                                     a.get("recommended_action"))
        await self._upsert_doc({"doc_id": "explanation:%s" % a["assessment_id"], "kind": "explanation",
                                "source_id": str(a["assessment_id"]), "mission_id": f.get("mission_id"),
                                "action_id": f.get("action_id"), "asset_id": f.get("asset_id"),
                                "location_id": f.get("location_id"), "severity": f.get("severity"),
                                "ts": ts, "title": f.get("title"), "content": content,
                                "metadata": {"flag_id": f.get("flag_id"), "model": a.get("model")}}, request_id)

    # ── ack ─────────────────────────────────────────────────────────────────────────────────────
    async def ack(self, flag_id: int, action: str, note: Optional[str], operator: str,
                  request_id: Optional[str]) -> Dict[str, Any]:
        row = await self.load(flag_id)
        ack = await self.repo.insert_ack(flag_id, action, note, operator)
        out = {"ack": ack, "flag_status": {"ACKNOWLEDGE": "ACKNOWLEDGED", "RESOLVE": "RESOLVED",
                                           "ESCALATE": "OPEN"}.get(action, row["flag"].get("status")),
               "rag_indexed": False}
        if note and note.strip():
            f = row["flag"]
            doc = {"doc_id": "note:%s" % ack["ack_id"], "kind": "operator_note", "source_id": str(ack["ack_id"]),
                   "mission_id": f.get("mission_id"), "action_id": f.get("action_id"), "asset_id": f.get("asset_id"),
                   "location_id": f.get("location_id"), "severity": f.get("severity"),
                   "status": out["flag_status"], "ts": ack.get("created_at"),
                   "title": "Operator %s on: %s" % (action.lower(), f.get("title")),
                   "content": "Operator %s %s flag %s (%s, %s). Note: %s" % (
                       operator, {"ACKNOWLEDGE": "acknowledged", "RESOLVE": "resolved", "ESCALATE": "escalated",
                                  "NOTE": "commented on"}.get(action, action.lower()), flag_id, f.get("title"),
                       f.get("severity"), note.strip()),
                   "metadata": {"flag_id": flag_id, "ack_id": ack["ack_id"], "action": action, "operator": operator}}
            out["rag_indexed"], stored, err = await self._upsert_doc(doc, request_id)
            out["doc_id"] = doc["doc_id"] if stored else None
            if err:
                out["rag_error"] = err
        else:
            out["rag_error"] = "no note: nothing to index"
        return out

    async def _upsert_doc(self, doc: Dict[str, Any],
                          request_id: Optional[str]) -> tuple[bool, bool, Optional[str]]:
        """(embedded, stored, error). With the embedding service down the document is still stored, with a NULL
        vector: full-text search finds it and `amd/embed/ingest.py` backfills the vector."""
        vec, err = None, None
        try:
            vec = (await self.embed.embed_documents([doc["content"]], request_id=request_id))[0]
        except ServiceUnavailable as exc:
            err = "embedding service unavailable: %s (stored without a vector: full-text search only)" % exc.reason
        try:
            await self.repo.upsert_document(doc, vec_literal(vec) if vec is not None else None,
                                            self.embed.model if vec is not None else None)
        except (DatabaseUnavailable, psycopg.Error) as exc:
            return False, False, "; ".join(e for e in (err, "document not stored: %s" % short_error(exc)) if e)
        return vec is not None, True, err
