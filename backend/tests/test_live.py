"""Live simulation API (app/routers/live.py, app/services/live.py) against the in-memory fakes."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.services import live as L

from .conftest import REPO_ROOT
from .fakes_live import FakeLiveRepo


@pytest.fixture
def lh(make_harness):
    h = make_harness()
    fake = FakeLiveRepo()
    fake.docs = h.repo.documents
    fake.base_nodes = {"mission:x"} | {"asset:%s" % a for a in h.svc.ref.assets} | \
        {"location:%s" % loc for loc in h.svc.ref.locations} | \
        {"action_type:%s" % t for t in ("GAUGE_READING", "ACOUSTIC_INSPECTION", "VISUAL_INSPECTION",
                                        "THERMAL_INSPECTION")}
    h.svc.live.live = fake
    h.svc.live.repo = h.repo
    h.svc.live.sink = h.sink
    h.live = fake
    return h


def laya(escalate: bool, p: float = 0.9):
    a = "A" if escalate else "B"
    return {"status": "ok", "model": "laya-test", "latency_ms": 12, "device": "cpu",
            "answers": {"escalate": {"answer": a, "confidence": p, "probs": {"A": p if escalate else 1 - p,
                                                                             "B": 1 - p if escalate else p}},
                        "priority": {"answer": "P1" if escalate else "P4", "confidence": 0.8}}}


async def walk(h, route="R2", laya_for=lambda planned: laya(planned == "red"), qwen=None):
    r = await h.client.post("/api/v1/live/runs", json={"route_id": route})
    assert r.status_code == 200, r.text
    run = r.json()["run"]
    c = (await h.client.post("/api/v1/live/claim", json={"runner": "test"})).json()
    assert c["run"]["run_id"] == run["run_id"] and c["run"]["status"] == "running"
    out = []
    for i, stop in enumerate(c["route"]["stops"]):
        s = (await h.client.post("/api/v1/live/runs/%d/stops/%d/start" % (run["run_id"], i))).json()
        ev = s["action_event"]
        assert ev["action_id"] == "LIVE-%d-%s" % (run["run_id"], stop["stop"])
        res = await h.client.post("/api/v1/live/runs/%d/stops/%d/result" % (run["run_id"], i),
                                  json={"timestamp": ev["timestamp"], "laya": laya_for(stop["planned_verdict"])})
        assert res.status_code == 200, res.text
        res = res.json()
        body = {"system2_request": res["system2_request"], "photo": {"sha256": "ab" * 32, "bytes": 10},
                "vision": {"pairing": "demo", "rows": []}, "system2_ms": 1000}
        if qwen:
            body["system2"] = qwen(res)
        else:
            body["system2_error"] = "system2 unreachable"
        an = await h.client.post("/api/v1/live/runs/%d/stops/%d/analysis" % (run["run_id"], i), json=body)
        assert an.status_code == 200, an.text
        out.append((stop, res, an.json()))
    fin = (await h.client.post("/api/v1/live/runs/%d/finish" % run["run_id"], json={"status": "done"})).json()
    return run, out, fin


async def test_routes_are_five_by_eight_with_two_reds_each(lh):
    data = (await lh.client.get("/api/v1/live/routes")).json()
    assert [r["route_id"] for r in data["routes"]] == ["R1", "R2", "R3", "R4", "R5"]
    for r in data["routes"]:
        assert len(r["stops"]) == 8 and r["planned_red"] >= 2
        assert [s["stop"] for s in r["stops"]] == list("ABCDEFGH")
        for s in r["stops"]:
            a = lh.svc.ref.actions[s["source_action_id"]]
            assert (s["location_id"], s["asset_id"], s["battery_pct"]) == (a["location_id"], a["asset_id"],
                                                                           a["battery_pct"])
            assert Path(REPO_ROOT, "web", "public", s["photo"].lstrip("/")).is_file()
        batt = [s["battery_pct"] for s in r["stops"]]
        assert batt == sorted(batt, reverse=True), "battery never rises along a route"


async def test_live_run_end_to_end_template_path(lh):
    run, stops, fin = await walk(lh, "R2")
    verdicts = [res["verdict"] for _, res, _ in stops]
    assert verdicts == [s["planned_verdict"] for s, _, _ in stops]
    assert verdicts.count("red") == 2
    reds = [res for _, res, _ in stops if res["verdict"] == "red"]
    for res in reds:   # a flag per red stop, keyed on the fresh live action id
        f = lh.repo.flags[res["flag_id"]]
        assert f["triage_event_id"] == "act-" + res["capture"]["action_id"] and f["escalated"]
        assert res["system2_request"]["flag_id"] == res["flag_id"]
    for _, res, an in stops:
        assert an["qwen_source"] == "template" and "unreachable" in an["fallback_reason"]
        assert an["capture"]["qwen"]["model"] == "template" and an["capture"]["phase"] == "done"
        assert an["capture"]["photo"]["label"] == "simulated capture"
    # templates are stored TEMPLATE on red flags and never indexed as explanations
    assert all(a["state"] == "TEMPLATE" for a in lh.repo.assessments.values())
    assert not [d for d in lh.repo.documents if d.startswith("explanation:")]
    assert fin["run"]["status"] == "done" and fin["run"]["counts"]["red"] == 2
    assert fin["run"]["counts"]["template"] == 8
    # events stay off the kit missions table (FK) but carry the live tag; raw log has every event
    ev = lh.repo.events["LIVE-%d-E-result" % run["run_id"]]
    assert ev["mission_id"] is None and ev["payload"]["live"]["source_action_id"] == "A0133"
    assert sum(1 for r in lh.repo.raw if r["source"] == "live") == 2 + 8 * 2


async def test_rerun_creates_new_rows(lh):
    run1, s1, _ = await walk(lh, "R2")
    run2, s2, _ = await walk(lh, "R2")
    f1 = {res["flag_id"] for _, res, _ in s1 if res["flag_id"]}
    f2 = {res["flag_id"] for _, res, _ in s2 if res["flag_id"]}
    assert f1 and f2 and not f1 & f2
    assert len(lh.repo.flags) == 4


async def test_parallel_mode_rules_catch_a_laya_miss(lh):
    _, stops, _ = await walk(lh, "R2", laya_for=lambda planned: laya(False))
    assert [res["verdict"] for _, res, _ in stops].count("red") == 2
    e = next(res for s, res, _ in stops if s["stop"] == "E")
    assert e["capture"]["laya"]["status"] == "ok" and e["capture"]["decision"]["mode"] == "parallel"


async def test_qwen_answer_is_stored_indexed_and_graphed(lh):
    def qwen(res):
        return {"answer": "Gauge read failed on a critical asset.", "question": res["system2_request"]["context"]["question"],
                "recommended_action": "DISPATCH_TECHNICIAN", "confidence": "medium", "model": "qwen3.8-27b-q8",
                "path": "llm", "latency_ms": 11000, "evidence": [{"claim": "x", "ids": ["F1"]}],
                "flag_id": res["system2_request"]["flag_id"], "insufficient_context": False}
    run, stops, fin = await walk(lh, "R2", qwen=qwen)
    assert fin["run"]["counts"]["qwen"] == 8
    e_stop, e_res, e_an = next(x for x in stops if x[0]["stop"] == "E")
    assert e_an["qwen_source"] == "system2" and lh.repo.assessments[e_res["flag_id"]]["state"] == "DONE"
    doc = e_an["capture"]["doc_id"]
    assert doc == "explanation:%s" % e_an["assessment_id"]
    kg = (await lh.client.get("/api/v1/live/kg", params={"run_id": run["run_id"]})).json()
    ids = {n["node_id"] for n in kg["nodes"]}
    a = "action:LIVE-%d-E" % run["run_id"]
    assert {a, "anomaly:LIVE-%d-E" % run["run_id"], "flag:%d" % e_res["flag_id"], "explanation:%s" % e_an["assessment_id"],
            "mission:LIVE-%d" % run["run_id"]} <= ids
    rels = {(e["src"], e["rel"], e["dst"]) for e in kg["edges"]}
    assert (a, "inspects", "asset:HX-217") in rels and (a, "at", "location:WP06") in rels
    assert ("flag:%d" % e_res["flag_id"], "flags", a) in rels
    assert ("explanation:%s" % e_an["assessment_id"], "explains", "flag:%d" % e_res["flag_id"]) in rels
    # embeddings: only rows whose stored text matches are updated
    items = [{"key": x["key"], "text": x["text"], "vector": [0.1, 0.2, 0.3, 0.4]} for x in e_res["embed"] + e_an["embed"]]
    items.append({"key": "doc:" + doc, "text": "tampered", "vector": [0.1, 0.2, 0.3, 0.4]})
    r = (await lh.client.post("/api/v1/live/runs/%d/embeddings" % run["run_id"], json={"items": items})).json()
    assert len(r["embedded"]) == len(items) - 1 and len(r["skipped"]) == 1
    assert lh.live.nodes[a]["embedding"] is not None      # document-backed node reuses the event doc's vector


async def test_one_active_run_and_admin_only(lh):
    assert (await lh.client.post("/api/v1/live/runs", json={"route_id": "R1"})).status_code == 200
    r = await lh.client.post("/api/v1/live/runs", json={"route_id": "R3"})
    assert r.status_code == 409 and r.json()["error"] == "live_conflict"
    assert (await lh.client.post("/api/v1/live/runs", json={"route_id": "R9"})).status_code == 404
    assert (await lh.client.post("/api/v1/live/runs", json={"route_id": "X1"})).status_code == 422
    r = await lh.client.post("/api/v1/live/claim", json={}, headers={"X-Admin-Token": "nope"})
    assert r.status_code == 401
    assert (await lh.client.get("/api/v1/live/runs")).status_code == 200


def _system2_packet_module():
    """System 2's own normalize_packet (amd/system2/analyst/packet.py: pure, pydantic only), imported for one check."""
    import sys
    root = str(REPO_ROOT / "amd" / "system2")
    sys.path.insert(0, root)
    try:
        from analyst import backend_contract, packet
    finally:
        sys.path.remove(root)
    return backend_contract, packet


async def test_live_packet_has_no_gaps_so_system2_calls_the_model(lh):
    """Any `gaps` entry makes System 2 answer insufficient_context with no model call (and no drafts): the live
    provenance note is an ordinary, cited fact instead."""
    _, stops, _ = await walk(lh, "R2")
    contract, packet = _system2_packet_module()
    for _, res, _ in stops:
        req = res["system2_request"]
        ctx = req["context"]
        assert not ctx.get("gaps") and ctx["insufficient_context"] is False
        notes = [f for f in ctx["facts"] if "re-sent now" in f["fact"]]
        assert len(notes) == 1 and "simulated capture" in notes[0]["fact"]
        assert notes[0]["id"] in ctx["citation_ids"]
        prepared = packet.normalize_packet(contract.BackendAssessRequest.model_validate(req))
        assert prepared.insufficient_context is False, res["capture"]["stop"]


def test_allowed_actions_mirror_mcp_domain():
    path = REPO_ROOT / "amd" / "mcp" / "spot_mcp" / "domain.py"
    spec = importlib.util.spec_from_file_location("spot_mcp_domain_for_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.ALLOWED_ACTIONS == L.ALLOWED_ACTIONS
    for cat in L.ALLOWED_ACTIONS:
        for prio in ("P1", "P2", "P3", "P4"):
            for facts in ({}, {"battery_pct": 10, "failure_type": "COMMUNICATION_LOSS"}, {"severity": "CRITICAL"},
                          {"failure_type": "PATH_BLOCKED"}, {"environment": "LOW_LIGHT"}):
                assert mod.suggested_action(cat, prio, facts) == L.suggested_action(cat, prio, facts)


async def test_preview_matches_the_plan_without_writing(lh):
    data = (await lh.client.get("/api/v1/live/preview")).json()
    assert len(data["stops"]) == 40 and data["mode"] == "parallel"
    for s in data["stops"]:
        assert s["error"] is None and s["action_event"]["kind"] == "action"
        assert ("red" if s["rules"]["escalate"] else "green") == s["planned_verdict"]
    assert not lh.repo.events and not lh.live.runs
