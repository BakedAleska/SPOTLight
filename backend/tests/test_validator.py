"""The validator wrapper: rule explanations agree with policy.py (and db/policy_rules.py); TRIAGE_MODE logic."""
import sys

import pytest

from app.services import validator as V
from app.services.triage import T_events, T_kit

from .conftest import KIT, REPO_ROOT


def kit_events():
    k = T_kit.load_kit(KIT)
    for mid, m in k.missions.items():
        for e in T_events.mission_events(m, k.actions_of(mid), k.locations, k.assets, k.readings, k.anomalies, "kit"):
            if e["kind"] != "pre_action":
                yield e


def test_top_rule_priority_equals_policy_on_every_kit_event():
    n = 0
    for e in kit_events():
        v = V.rules_verdict(e["kind"], e)
        top = v["rules"][0]["tier"] if v["rules"] else "P4"
        assert top == v["priority"], (e["event_id"], v["rules"], v["priority"])
        n += 1
    assert n == 297


def test_rule_names_match_db_policy_rules():
    """flags.reasons rule names must equal db/policy_rules.py (they are the `policy:<rule>` RAG doc ids)."""
    sys.path.insert(0, str(REPO_ROOT / "db"))
    PR = pytest.importorskip("policy_rules")
    for e in kit_events():
        mine = sorted(r["rule"] for r in V.rules_verdict(e["kind"], e)["rules"])
        theirs = sorted(r["rule"] for r in PR.evaluate(e)["reasons"])
        assert mine == theirs, e["event_id"]


RULES_ESC = {"escalate": True, "priority": "P2", "category": "equipment", "rules": [], "policy_version": "x"}
RULES_LOG = {"escalate": False, "priority": "P3", "category": "data_quality", "rules": [], "policy_version": "x"}


def laya(esc, pr, p=0.8, cat="equipment"):
    return {"escalate": esc, "p_escalate": p if esc else 1 - p, "priority": pr, "category": cat}


@pytest.mark.parametrize("mode,laya_v,rules,escalated,priority,agreement", [
    ("serial", laya(True, "P2"), RULES_ESC, True, "P2", "confirmed"),
    ("serial", laya(True, "P1"), RULES_LOG, False, "P3", "rules_rejected"),          # not escalated -> <= P3 (M1)
    ("serial", laya(False, "P4"), RULES_ESC, False, "P4", "rules_only_escalate"),
    ("parallel", laya(False, "P4"), RULES_ESC, True, "P2", "rules_only_escalate"),   # rules floor: Laya miss caught
    ("parallel", laya(True, "P1"), RULES_LOG, True, "P1", "rules_rejected"),         # Laya can raise
    ("parallel", laya(True, "P3"), RULES_ESC, True, "P2", "confirmed"),              # ...but never lower
    ("serial", None, RULES_ESC, True, "P2", "laya_unavailable"),
    ("parallel", None, RULES_LOG, False, "P3", "laya_unavailable"),
])
def test_combine(mode, laya_v, rules, escalated, priority, agreement):
    d = V.combine(mode, laya_v, rules, "action")
    assert (d["escalated"], d["priority"], d["agreement"]) == (escalated, priority, agreement)
    assert d["severity"] == V.SEVERITY_OF[priority]
    assert 0 < d["score"] < 100


def rules(pr, cat="equipment"):
    return {"escalate": pr in ("P1", "P2"), "priority": pr, "category": cat, "rules": [], "policy_version": "x"}


# Audit M1: the probed cases, plus parallel-mode cases. (mode, laya, rules, escalated, priority, severity)
@pytest.mark.parametrize("mode,laya_v,rules_v,escalated,priority,severity", [
    ("parallel", laya(True, "P4"), rules("P4"), True, "P2", "HIGH"),        # probed: was escalated P4/LOW
    ("parallel", laya(False, "P1"), rules("P3"), False, "P3", "MEDIUM"),    # probed: was routine P1/CRITICAL
    ("serial", laya(False, "P1"), rules("P2"), False, "P3", "MEDIUM"),      # probed: was routine P1
    ("parallel", laya(True, "P3"), rules("P3"), True, "P2", "HIGH"),        # Laya escalates a rules-P3 event
    ("parallel", laya(True, "P4"), rules("P1"), True, "P1", "CRITICAL"),    # rules floor P1 kept
    ("parallel", laya(False, "P4"), rules("P1"), True, "P1", "CRITICAL"),   # Laya miss caught at the rules' P1
    ("parallel", laya(False, "P2"), rules("P4"), False, "P3", "MEDIUM"),    # both routine: capped at P3
    ("parallel", laya(False, "P4"), rules("P4"), False, "P4", "LOW"),
    ("serial", laya(True, "P4"), rules("P1"), True, "P1", "CRITICAL"),      # confirmed: rules' P1
    ("serial", laya(True, "P1"), rules("P4"), False, "P3", "MEDIUM"),       # rules_rejected: capped at P3
    ("serial", laya(False, "P2"), rules("P1"), False, "P3", "MEDIUM"),      # rules_only_escalate in serial
])
def test_combine_priority_consistent_with_escalation(mode, laya_v, rules_v, escalated, priority, severity):
    d = V.combine(mode, laya_v, rules_v, "action")
    assert (d["escalated"], d["priority"], d["severity"]) == (escalated, priority, severity)
    assert V.BASE_SCORE[priority] <= d["score"] < (V.BASE_SCORE[priority] + 10)


@pytest.mark.parametrize("mode", ["serial", "parallel"])
def test_combine_invariant_every_combination(mode):
    """Every (Laya escalate, Laya priority, rules priority, kind): escalated <=> P1/P2, severity follows."""
    n = 0
    for kind in ("action", "mission"):
        for esc in (True, False):
            for lp in ("P1", "P2", "P3", "P4", None):
                for rp in ("P1", "P2", "P3", "P4"):
                    for lv in (laya(esc, lp), None):
                        d = V.combine(mode, lv, rules(rp, "mission" if kind == "mission" else "equipment"), kind)
                        assert d["escalated"] == (d["priority"] in ("P1", "P2")), (mode, esc, lp, rp, d)
                        assert d["severity"] == V.SEVERITY_OF[d["priority"]]
                        if mode == "parallel" and lv is not None:   # parallel: either escalates
                            assert d["escalated"] == (esc or rp in ("P1", "P2"))
                        n += 1
    assert n == 2 * 2 * 5 * 4 * 2


def test_serial_escalation_is_always_p1_or_p2():
    d = V.combine("serial", laya(True, "P3"), RULES_ESC, "action")
    assert d["escalated"] and d["priority"] == "P2"


def test_reasons_carry_both_verdicts():
    d = V.combine("serial", laya(True, "P2", 0.83), RULES_ESC, "action")
    r = V.build_reasons("serial", laya(True, "P2", 0.83), None, RULES_ESC, d, "laya-test")
    assert [x["rule"] for x in r[:2]] == ["laya_verdict", "rules_verdict"]
    assert d["summary"] == "Laya: escalate (0.83) · rules: confirmed"
    r2 = V.build_reasons("serial", None, "boom", RULES_ESC, V.combine("serial", None, RULES_ESC, "action"), None)
    assert r2[0]["rule"] == "laya_unavailable" and r2[0]["detail"] == V.LAYA_UNAVAILABLE_DETAIL
    assert "boom" not in r2[0]["detail"]                             # raw error stays in app.model_calls
