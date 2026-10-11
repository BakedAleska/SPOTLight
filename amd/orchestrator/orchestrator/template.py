"""Deterministic answers: the template fallback, the System 1 short answer, and System 2 normalization.

Same shape as backend/app/services/system2.py (`template_explanation`, `normalize_assessment`), rebuilt from the MCP
packet instead of the DB row (the orchestrator has no database). Not imported from the backend so the two images
stay independent; ACTION_TEXT / suggested_action mirror amd/mcp/spot_mcp/domain.py like the backend's copy does.
Every number in a template comes from the packet or a tool reading, never from model text.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

BATTERY_CRITICAL, BATTERY_LOW = 15, 25   # policy thresholds (amd/system1/triage/policy.py)
VERDICT_RULES = ("laya_verdict", "rules_verdict", "laya_unavailable")
CONFIDENCE = {"high": 0.85, "medium": 0.6, "low": 0.35}

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
    "MANUAL_REVIEW": "a person reviews the flag (not enough context for an automatic recommendation)",
}


def suggested_action(category: Optional[str], priority: Optional[str], facts: Dict[str, Any]) -> str:
    batt, ft = facts.get("battery_pct"), facts.get("failure_type")
    if category == "equipment":
        if priority == "P1":
            return "ESCALATE_TO_SUPERVISOR" if facts.get("severity") == "CRITICAL" else "DISPATCH_TECHNICIAN"
        return "SCHEDULE_MAINTENANCE" if priority == "P2" else "REINSPECT_NEXT_ROUTE"
    if category == "robot_health":
        if ft == "COMMUNICATION_LOSS":
            return "CHECK_COMMS" if (batt is None or batt > BATTERY_LOW) else "RETURN_TO_DOCK"
        if batt is not None and batt <= BATTERY_CRITICAL:
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


def reasons(flag: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fired rules plus the verdict rows. The MCP flag view keeps verdicts (laya_verdict, rules_verdict,
    laya_unavailable) under `flag.verdicts`; the backend's SQL packet still has them inside `reasons`."""
    return [r for r in list(flag.get("reasons") or []) + list(flag.get("verdicts") or []) if isinstance(r, dict)]


def validator_of(packet: Dict[str, Any]) -> Dict[str, Any]:
    ev = packet.get("event") or {}
    return ev.get("validator") or (ev.get("mission") or {}).get("validator") or {}


def _action_code(packet: Dict[str, Any]) -> str:
    flag = packet.get("flag") or {}
    code = packet.get("suggested_action")
    if code in ACTION_TEXT:
        return code
    ev, v = packet.get("event") or {}, validator_of(packet)
    if v.get("suggested_action") in ACTION_TEXT:
        return v["suggested_action"]
    anomalies = ev.get("anomalies") or [{}]
    is_mission = not flag.get("action_id") and "mission" in ev
    category = flag.get("category") or v.get("category") or ("mission" if is_mission else None)
    return suggested_action(category, flag.get("priority") or v.get("priority"), {
        "battery_pct": ev.get("battery_pct"), "failure_type": ev.get("failure_type"),
        "severity": anomalies[0].get("severity"), "environment": (ev.get("location") or {}).get("environment")})


def _question(packet: Dict[str, Any], flag_id: int) -> str:
    q = packet.get("question")
    if isinstance(q, str) and q.strip():
        return q
    flag = packet.get("flag") or {}
    if flag.get("mission_id") and not flag.get("action_id"):
        return "Did mission %s really complete cleanly?" % flag["mission_id"]
    return "Does flag %s need operator attention now?" % flag_id


def _s1_line(answers: List[Dict[str, Any]]) -> Optional[str]:
    parts = []
    for a in answers:
        if a.get("when") in ("action", "mission") and a.get("q") and a.get("answer"):
            p = a.get("p")
            meaning = a.get("meaning") or ""
            parts.append("%s=%s%s%s" % (a["q"], a["answer"], " (%s)" % meaning if meaning else "",
                                        " p=%.2f" % p if isinstance(p, (int, float)) else ""))
    return "System 1 (Laya): %s." % "; ".join(parts) if parts else None


MAX_VISION_LINES = 3


def _usable(r: Dict[str, Any]) -> bool:
    """A vision row whose value may be printed: HIGH trust and a value present."""
    return r.get("trust") == "HIGH" and r.get("value") is not None


def _citable(r: Dict[str, Any]) -> bool:
    """Only a HIGH-trust reading of this asset's own image is evidence; a demo sample image never is."""
    return _usable(r) and r.get("pairing") != "demo" and bool(r.get("id"))


def _vision_lines(rows: List[Dict[str, Any]]) -> List[str]:
    """One line per row: LOW rows never show a value or comparison; comparison is agrees / disagrees / not judged."""
    out = []
    for r in rows[:MAX_VISION_LINES]:
        tool = r.get("tool") or r.get("kind") or "tool"
        quantity = r.get("quantity") or "reading"
        demo = r.get("pairing") == "demo"
        head = "Vision %s (%s, %s trust%s)" % (r.get("id"), tool, r.get("trust") or "LOW",
                                               ", demo sample image, not this asset" if demo else "")
        if not _usable(r):
            out.append("%s: %s reading unreliable (low confidence); its value is not used." % (head, quantity))
            continue
        value = ("%s %s" % (r["value"], r.get("unit") or "")).strip()
        if r.get("bound") == "lower":
            value = "at least %s (top of the scale)" % value
        line = "%s: %s %s" % (head, quantity, value)
        if demo:
            line += "; illustrative only, not judged against this asset"
        elif r.get("logged_value") is not None:
            logged = "logged %s%s" % (r["logged_value"], " (%s)" % r["logged_id"] if r.get("logged_id") else "")
            if r.get("agrees") is True:
                line += "; agrees with %s" % logged
            elif r.get("agrees") is False:
                line += "; disagrees with %s%s" % (logged, ", delta %s" % r["delta"] if r.get("delta") is not None
                                                   else "")
            else:
                line += "; vs %s: not judged" % logged
        out.append(line + ".")
    return out


def template_answer(packet: Dict[str, Any], flag_id: int, system1: Optional[List[Dict[str, Any]]] = None,
                    vision_rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Orbit-style explanation from the packet alone (System 2 unset, down, invalid or out of time)."""
    flag = packet.get("flag") or {}
    rs = reasons(flag)
    by_rule = {r.get("rule"): r for r in rs}
    fired = [r for r in rs if r.get("rule") not in VERDICT_RULES]
    v = validator_of(packet)
    priority = flag.get("priority") or v.get("priority")
    title = flag.get("title") or "flag %s" % flag_id
    insufficient = bool(packet.get("insufficient_context")) or not packet.get("flag")
    if not packet.get("flag"):
        answer = "Context for flag %s is unavailable; request manual review." % flag_id
    elif flag.get("escalated"):
        answer = "Yes. Escalate (%s, %s): %s." % (priority or "?", flag.get("severity"), title)
    else:
        answer = "Not urgent (%s): %s." % (priority or "?", title)

    parts: List[str] = []
    if "laya_verdict" in by_rule:
        parts.append(by_rule["laya_verdict"].get("detail") or "")
    elif "laya_unavailable" in by_rule:
        parts.append("System 1 (Laya) was unavailable, so the deterministic rules decided alone.")
    else:
        line = _s1_line(system1 or [])
        if line:
            parts.append(line)
    if "rules_verdict" in by_rule:
        parts.append("Validator: %s." % (by_rule["rules_verdict"].get("detail") or "").rstrip("."))
    for r in fired[:4]:
        parts.append("Rule %s: %s." % (r.get("rule"), (r.get("detail") or "").rstrip(".")))
    seen = {r.get("detail") for r in rs}
    for f in packet.get("facts") or []:
        txt = (f.get("fact") or f.get("text")) if isinstance(f, dict) else str(f)
        if txt and txt not in seen and len(parts) < 9:
            seen.add(txt)
            parts.append(str(txt).rstrip(".") + ".")
    parts.extend(_vision_lines(vision_rows or []))

    code = "MANUAL_REVIEW" if insufficient else _action_code(packet)
    laya = by_rule.get("laya_verdict") or {}
    return {
        "question": _question(packet, flag_id), "answer": answer, "reasoning": " ".join(p for p in parts if p),
        "recommended_action": "%s: %s" % (code, ACTION_TEXT.get(code, "")), "recommended_action_code": code,
        "severity_assessment": flag.get("severity"), "confidence": laya.get("confidence"), "model": "template",
        "source": "template", "insufficient_context": insufficient, "evidence": None, "latency_ms": None,
        "tokens_per_sec": None,
        "citations": (["flag:%s" % flag_id] + ["policy:%s" % r.get("rule") for r in fired[:4]]
                      + [r["id"] for r in (vision_rows or [])[:MAX_VISION_LINES] if _citable(r)]),
    }


def system1_answer(packet: Dict[str, Any], flag_id: int, system1: List[Dict[str, Any]], model: Optional[str],
                   route_detail: str) -> Dict[str, Any]:
    """The short answer for flags routed system1_only: no System 2 call, no vision."""
    flag = packet.get("flag") or {}
    v = validator_of(packet)
    by_q = {a.get("q"): a for a in system1 if a.get("when") in ("action", "mission")}
    priority = (by_q.get("priority") or {}).get("answer") or flag.get("priority") or v.get("priority")
    title = flag.get("title") or "flag %s" % flag_id
    parts = [p for p in (_s1_line(system1),) if p]
    if v:
        parts.append("Validator: priority %s, escalate=%s." % (v.get("priority"), "yes" if v.get("escalate") == "A"
                                                                else "no"))
    parts.append("Routing: %s; System 2 was not called." % route_detail)
    code = _action_code(packet)
    if code not in ("MONITOR", "NO_ACTION", "REINSPECT_NEXT_ROUTE"):
        code = "MONITOR"   # a routine flag never gets an urgent action without System 2
    esc = by_q.get("escalate") or {}
    conf = esc.get("p") if isinstance(esc.get("p"), (int, float)) else None
    return {
        "question": _question(packet, flag_id), "answer": "Routine (%s): %s. No escalation signal." % (priority or "?",
                                                                                                     title),
        "reasoning": " ".join(parts), "recommended_action": "%s: %s" % (code, ACTION_TEXT[code]),
        "recommended_action_code": code, "severity_assessment": flag.get("severity"), "confidence": conf,
        "model": model or "system1", "source": "system1", "insufficient_context": False, "evidence": None,
        "latency_ms": None, "tokens_per_sec": None,
        "citations": ["flag:%s" % flag_id] + [a["id"] for a in system1 if a.get("id")][:4],
    }


def normalize_system2(out: Dict[str, Any]) -> Dict[str, Any]:
    """System 2's flat response -> the backend's assessment fields (backend normalize_assessment)."""
    evidence = out.get("evidence") if isinstance(out.get("evidence"), list) else []
    reasoning = out.get("reasoning")
    if not reasoning and evidence:
        reasoning = " ".join("%s [%s]" % (e.get("claim"), ", ".join(e.get("ids") or []))
                             for e in evidence if isinstance(e, dict))
    rec = out.get("recommended_action")
    if isinstance(rec, dict):
        rec = rec.get("action") or rec.get("code")
    conf = out.get("confidence")
    if isinstance(conf, str):
        conf = CONFIDENCE.get(conf.lower())
    cites: List[str] = []
    for e in evidence:
        for i in (e.get("ids") or []) if isinstance(e, dict) else []:
            if i not in cites:
                cites.append(i)
    return {"question": out.get("question"), "answer": out.get("answer"), "reasoning": reasoning,
            "recommended_action": rec, "recommended_action_code": rec if rec in ACTION_TEXT else None,
            "severity_assessment": out.get("severity_assessment"), "confidence": conf, "model": out.get("model"),
            "latency_ms": out.get("latency_ms"), "tokens_per_sec": out.get("tokens_per_sec"),
            "insufficient_context": out.get("insufficient_context"), "evidence": evidence, "citations": cites,
            "source": "system2"}


def system2_problem(out: Dict[str, Any]) -> Optional[str]:
    """Why a System 2 reply cannot be used as-is (then the template is served), or None."""
    if not isinstance(out.get("answer"), str) or not out["answer"].strip():
        return "response had no answer"
    if not isinstance(out.get("evidence", []), list):
        return "evidence is not a list"
    rec = out.get("recommended_action")
    if not isinstance(rec, (str, dict)) or not rec:
        return "response had no recommended_action"
    return None
