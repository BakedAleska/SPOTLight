"""The deterministic validator (amd/system1/triage/policy.py) and how its verdict combines with Laya's.

- `rules_verdict` runs the policy (the single source of truth for priority/category/escalate) and lists the
  named rules that fired, so operators and System 2 can see *why* (each rule name is also a RAG doc id,
  `policy:<rule>`; the list is in docs/backend-services-contract.md).
- `combine` applies TRIAGE_MODE:
    serial   : Laya flags, the rules confirm. Escalate only if both say escalate.
    parallel : either can escalate; the rules' priority is a floor Laya can raise but never lower.
  With Laya unavailable, both modes fall back to the rules-only verdict, marked `laya_unavailable`.
  In every mode the final priority matches the decision (audit M1): escalated -> P1/P2, not escalated -> P3/P4;
  severity and score follow the final priority.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .triage import T_policy as P

PRIORITY_RANK = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}
# Stable text of the laya_unavailable reason. The raw error (connection text, "busy: ...") is not copied into
# flags.reasons: System 2 reads reason details as evidence. It stays in app.model_calls and the /ingest response.
LAYA_UNAVAILABLE_DETAIL = "System 1 (Laya) unavailable; rules-only verdict"
SEVERITY_OF = {"P1": "CRITICAL", "P2": "HIGH", "P3": "MEDIUM", "P4": "LOW"}
BASE_SCORE = {"P1": 90, "P2": 70, "P3": 45, "P4": 20}
TIER_WEIGHT = {"P1": 3, "P2": 2, "P3": 1}
POLICY_VERSION = P.POLICY_VERSION

# Rule names mirror db/policy_rules.py exactly (same predicates; the RAG doc for each is `policy:<rule>`).
# tests/test_validator.py checks both the names and that the top rule's priority equals policy.py's.
ACTION_RULES = ("anomaly_critical", "anomaly_high_on_critical_asset", "failed_at_critical_battery",
                "comms_loss_low_battery", "anomaly_high", "anomaly_medium_on_critical_asset",
                "failed_on_critical_asset", "battery_low", "equipment_reading_out_of_band",
                "anomaly_present", "action_failed", "reading_untrusted", "battery_watch", "reading_out_of_band")
MISSION_RULES = ("mission_reported_failed", "mission_misleading_serious", "mission_misleading",
                 "mission_end_battery_low")


def _h(s: Optional[str]) -> str:
    return (s or "").replace("_", " ").lower()


def more_urgent(a: Optional[str], b: Optional[str]) -> Optional[str]:
    if a is None:
        return b
    if b is None:
        return a
    return a if PRIORITY_RANK.get(a, 9) <= PRIORITY_RANK.get(b, 9) else b


def _rule(name: str, tier: str, detail: str) -> Dict[str, Any]:
    return {"rule": name, "tier": tier, "priority": tier, "weight": TIER_WEIGHT[tier], "detail": detail}


# ── rules ───────────────────────────────────────────────────────────────────────────────────────
def action_rules(ev: dict) -> List[Dict[str, Any]]:
    """Every named policy condition that holds for `ev`, most urgent first. Its top tier is the event's
    priority (the same answer as policy.label_action)."""
    an = ev.get("anomaly")
    sev = P.SEV[an["severity"]] if an else 0
    asset = ev["asset"]
    crit = P.CRIT[asset["criticality"]]
    batt, failed, ft = ev["battery_pct"], ev["status"] == "FAILED", ev.get("failure_type")
    trust, out_band = P.reading_state(ev)
    r = ev.get("reading")
    loc_band = out_band and r["measurement_type"] == "LOCALIZATION_CONFIDENCE_PCT"
    equip_band = out_band and not loc_band
    who = "%s (%s, %s criticality)" % (asset.get("asset_name"), asset.get("asset_id"), asset["criticality"])
    anom = ("%s (%s) on %s" % (_h(an["anomaly_type"]), an["severity"], who)) if an else ""
    rd = ("%s = %g %s (quality %s)" % (_h(r["measurement_type"]), r["value"], r.get("unit", ""), r["quality"])
          if r else "")
    checks = [
        ("anomaly_critical", "P1", sev == 4, lambda: "CRITICAL anomaly: " + anom),
        ("anomaly_high_on_critical_asset", "P1", sev == 3 and crit >= 3,
         lambda: "HIGH anomaly on a critical asset: " + anom),
        ("failed_at_critical_battery", "P1", failed and batt <= P.BATTERY_CRITICAL,
         lambda: "action failed at %d%% battery (critical <= %d%%)" % (batt, P.BATTERY_CRITICAL)),
        ("comms_loss_low_battery", "P1", ft in P.COMMS_FAILURES and batt <= P.BATTERY_LOW,
         lambda: "communication loss at %d%% battery (low <= %d%%)" % (batt, P.BATTERY_LOW)),
        ("anomaly_high", "P2", sev == 3, lambda: "HIGH anomaly: " + anom),
        ("anomaly_medium_on_critical_asset", "P2", sev == 2 and crit >= 3,
         lambda: "MEDIUM anomaly on a critical asset: " + anom),
        ("failed_on_critical_asset", "P2", failed and crit >= 3,
         lambda: "%s failed (%s) on %s" % (_h(ev["action_type"]), _h(ft) or "unknown", who)),
        ("battery_low", "P2", batt <= P.BATTERY_LOW,
         lambda: "battery %d%% (low <= %d%%; kit: ~33%% action success at <= %d%%)" % (batt, P.BATTERY_LOW,
                                                                                        P.BATTERY_LOW)),
        ("equipment_reading_out_of_band", "P2", equip_band and crit >= 2,
         lambda: "reading out of safe band on %s: %s" % (who, rd)),
        ("anomaly_present", "P3", sev >= 1, lambda: "anomaly: " + anom),
        ("action_failed", "P3", failed, lambda: "%s failed: %s" % (_h(ev["action_type"]), _h(ft) or "unknown")),
        ("reading_untrusted", "P3", trust == "untrusted", lambda: "untrusted reading: " + rd),
        ("battery_watch", "P3", batt <= P.BATTERY_WATCH,
         lambda: "battery %d%% (watch <= %d%%)" % (batt, P.BATTERY_WATCH)),
        ("reading_out_of_band", "P3", out_band, lambda: "reading out of safe band: " + rd),
    ]
    out = [_rule(name, tier, detail()) for name, tier, cond, detail in checks if cond]
    out.sort(key=lambda x: PRIORITY_RANK[x["tier"]])
    return out


def mission_rules(ev: dict) -> List[Dict[str, Any]]:
    s = ev["anomalies_by_severity"]
    serious = s["HIGH"] + s["CRITICAL"]
    misleading = ev["reported_status"] == "COMPLETED" and (ev["actions_failed"] > 0 or serious > 0)
    checks = [
        ("mission_reported_failed", "P2", ev["reported_status"] == "FAILED",
         lambda: "mission reported FAILED (%d of %d actions failed)" % (ev["actions_failed"], ev["actions_total"])),
        ("mission_misleading_serious", "P2", misleading and serious > 0,
         lambda: "reported COMPLETED but had %d HIGH/CRITICAL anomalies and %d failed actions"
                 % (serious, ev["actions_failed"])),
        ("mission_misleading", "P3", misleading,
         lambda: "reported COMPLETED but %d of %d actions failed" % (ev["actions_failed"], ev["actions_total"])),
        ("mission_end_battery_low", "P3", ev["end_battery_pct"] <= P.BATTERY_LOW,
         lambda: "mission ended at %d%% battery (low <= %d%%)" % (ev["end_battery_pct"], P.BATTERY_LOW)),
    ]
    out = [_rule(name, tier, detail()) for name, tier, cond, detail in checks if cond]
    out.sort(key=lambda x: PRIORITY_RANK[x["tier"]])
    return out


def rules_verdict(kind: str, ev: dict) -> Optional[Dict[str, Any]]:
    """The validator's verdict for an action or mission event (None for pre_action: nothing to validate)."""
    if kind == "action":
        labels = P.label_action(ev)
        fired = action_rules(ev)
        category = labels["category"]
    elif kind == "mission":
        labels = P.label_mission(ev)
        fired = mission_rules(ev)
        category = "mission"
    else:
        return None
    return {"escalate": labels["escalate"] == "A", "priority": labels["priority"], "category": category,
            "labels": labels, "rules": fired, "policy_version": POLICY_VERSION}


# ── Laya ────────────────────────────────────────────────────────────────────────────────────────
def laya_verdict(answers: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Laya's escalate/priority/category answers -> a verdict (None if it didn't answer `escalate`)."""
    if not answers or not isinstance(answers.get("escalate"), dict):
        return None
    esc = answers["escalate"]
    probs = esc.get("probs") or {}
    p_esc = probs.get("A")
    if p_esc is None:
        p_esc = esc.get("confidence") if esc.get("answer") == "A" else 1 - float(esc.get("confidence") or 0)
    pr = (answers.get("priority") or {}).get("answer")
    cat = (answers.get("category") or {}).get("answer")
    return {"escalate": esc.get("answer") == "A", "p_escalate": round(float(p_esc), 4),
            "priority": pr if pr in PRIORITY_RANK else None, "category": cat,
            "priority_confidence": (answers.get("priority") or {}).get("confidence")}


# ── combine (TRIAGE_MODE) ───────────────────────────────────────────────────────────────────────
def consistent_priority(escalated: bool, priority: Optional[str], rules_priority: str) -> str:
    """Audit M1: an escalated flag is P1/P2 and a non-escalated event P3/P4, in both TRIAGE_MODEs.
    Escalated with P3/P4 -> the rules' priority if that is P1/P2, else P2. Not escalated with P1/P2 -> P3."""
    p = priority if priority in PRIORITY_RANK else rules_priority
    if escalated and PRIORITY_RANK[p] > 2:
        return rules_priority if PRIORITY_RANK[rules_priority] <= 2 else "P2"
    if not escalated and PRIORITY_RANK[p] < 3:
        return "P3"
    return p


def combine(mode: str, laya: Optional[Dict[str, Any]], rules: Dict[str, Any], kind: str) -> Dict[str, Any]:
    """Final decision: escalated, priority, category, severity, score, plus a one-line summary."""
    if laya is None:
        escalated, priority, category = rules["escalate"], rules["priority"], rules["category"]
        decided_by = "rules_only"
        agreement = "laya_unavailable"
    else:
        if mode == "serial":
            escalated = laya["escalate"] and rules["escalate"]
            priority = laya["priority"] or rules["priority"]
            decided_by = "laya+rules (serial: Laya flags, rules confirm)"
        else:
            escalated = laya["escalate"] or rules["escalate"]
            priority = more_urgent(laya["priority"], rules["priority"])
            decided_by = "laya|rules (parallel: either escalates; rules set the priority floor)"
        priority = consistent_priority(escalated, priority, rules["priority"])
        if kind == "mission":
            category = "mission"
        elif PRIORITY_RANK.get(laya.get("priority") or "", 9) < PRIORITY_RANK[rules["priority"]] \
                and laya.get("category") not in (None, "none"):
            category = laya["category"]
        else:
            category = rules["category"] if rules["category"] != "none" else (laya.get("category") or "none")
        if laya["escalate"] and rules["escalate"]:
            agreement = "confirmed"
        elif laya["escalate"]:
            agreement = "rules_rejected"
        elif rules["escalate"]:
            agreement = "rules_only_escalate"
        else:
            agreement = "both_routine"
    p = laya["p_escalate"] if laya else min(1.0, sum(r["weight"] for r in rules["rules"]) / 6.0)
    score = min(99.0, BASE_SCORE[priority] + round(9 * p, 1))
    laya_txt = ("Laya: %s (%.2f)" % ("escalate" if laya["escalate"] else "log only", laya["p_escalate"])
                if laya else "Laya: unavailable")
    rules_txt = "rules: " + {"confirmed": "confirmed", "rules_rejected": "not confirmed",
                             "rules_only_escalate": "escalate", "both_routine": "routine",
                             "laya_unavailable": "escalate" if rules["escalate"] else "routine"}[agreement]
    return {"escalated": escalated, "priority": priority, "category": category,
            "severity": SEVERITY_OF[priority], "score": score, "decided_by": decided_by,
            "agreement": agreement, "summary": "%s · %s" % (laya_txt, rules_txt)}


def build_reasons(mode: str, laya: Optional[Dict[str, Any]], laya_error: Optional[str], rules: Dict[str, Any],
                  decision: Dict[str, Any], model: Optional[str]) -> List[Dict[str, Any]]:
    """flags.reasons: both verdicts first, then the fired policy rules ([{rule, detail, weight, ...}]).
    `laya_error` is not stored here (see LAYA_UNAVAILABLE_DETAIL)."""
    reasons: List[Dict[str, Any]] = []
    if laya is None:
        reasons.append({"rule": "laya_unavailable", "source": "system1", "weight": 0,
                        "detail": LAYA_UNAVAILABLE_DETAIL})
    else:
        reasons.append({"rule": "laya_verdict", "source": "system1", "weight": 0, "model": model,
                        "verdict": "escalate" if laya["escalate"] else "log",
                        "confidence": laya["p_escalate"], "priority": laya["priority"], "category": laya["category"],
                        "detail": "Laya: %s (%.2f), %s %s" % ("escalate" if laya["escalate"] else "log only",
                                                             laya["p_escalate"], laya["priority"] or "?",
                                                             laya["category"] or "")})
    reasons.append({"rule": "rules_verdict", "source": "validator", "weight": 0,
                    "verdict": "escalate" if rules["escalate"] else "log", "priority": rules["priority"],
                    "category": rules["category"], "policy_version": rules["policy_version"],
                    "agreement": decision["agreement"], "triage_mode": mode,
                    "detail": decision["summary"]})
    for r in rules["rules"]:
        reasons.append({"rule": r["rule"], "source": "validator", "priority": r["tier"], "weight": r["weight"],
                        "detail": r["detail"]})
    return reasons


def flag_title(kind: str, ev: dict, decision: Dict[str, Any],
               rules: Optional[List[Dict[str, Any]]] = None) -> str:
    """One operator-readable line."""
    if kind == "mission":
        s = ev["anomalies_by_severity"]
        serious = s["HIGH"] + s["CRITICAL"]
        bits = []
        if ev["actions_failed"]:
            bits.append("%d of %d actions failed" % (ev["actions_failed"], ev["actions_total"]))
        if serious:
            bits.append("%d HIGH/CRITICAL anomal%s" % (serious, "y" if serious == 1 else "ies"))
        tail = (" but " + " and ".join(bits)) if bits and ev["reported_status"] == "COMPLETED" else \
            (": " + ", ".join(bits) if bits else "")
        return "Mission %s reported %s%s" % (ev["mission_id"], ev["reported_status"], tail)
    asset, loc = ev["asset"], ev["location"]
    where = "%s (%s) at %s %s" % (asset["asset_name"], asset["asset_id"], loc["location_id"], loc["location_name"])
    an = ev.get("anomaly")
    if an:
        head = "%s %s" % (an["severity"], _h(an["anomaly_type"]))
    elif ev["status"] == "FAILED":
        head = "%s failed (%s)" % (_h(ev["action_type"]).capitalize(), _h(ev.get("failure_type")) or "unknown")
    elif decision["category"] == "robot_health":
        head = "Low battery during %s" % _h(ev["action_type"])
    elif rules:
        head = rules[0]["detail"]
    else:
        head = "%s needs review" % _h(ev["action_type"]).capitalize()
    return "%s: %s · battery %d%%" % (head[0].upper() + head[1:], where, ev["battery_pct"])
