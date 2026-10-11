"""POST /live/runs/{run_id}/stops/{stop_index}/draft (contract "Streaming Qwen drafts"): Qwen's unchecked partial
answer shown in the /live Qwen box while the stop's analysis is pending, and never read as an answer."""
from __future__ import annotations

import json

from .conftest import ADMIN
from .test_live import laya, lh  # noqa: F401 - the `lh` fixture

FIELDS = {"evidence": [{"claim": "Gauge read failed", "ids": ["F1", "reading:R9", 7]}, {"claim": "Critical"}],
          "answer": "Gauge read fai", "question": "never sent", "confidence": 0.7, "extra": "x"}


async def _triaged(h, route="R2", stops=1):
    """A running run whose first `stops` stops are triaged (qwen_source pending); returns (run_id, results)."""
    run = (await h.client.post("/api/v1/live/runs", json={"route_id": route})).json()["run"]
    c = (await h.client.post("/api/v1/live/claim", json={"runner": "test"})).json()
    out = []
    for i, stop in enumerate(c["route"]["stops"][:stops]):
        s = (await h.client.post("/api/v1/live/runs/%d/stops/%d/start" % (run["run_id"], i))).json()
        res = (await h.client.post("/api/v1/live/runs/%d/stops/%d/result" % (run["run_id"], i), json={
            "timestamp": s["action_event"]["timestamp"], "laya": laya(stop["planned_verdict"] == "red")})).json()
        out.append(res)
    return run["run_id"], out


def _url(run_id, idx):
    return "/api/v1/live/runs/%d/stops/%d/draft" % (run_id, idx)


async def test_draft_updates_the_capture_while_pending_and_polling_sees_it(lh):  # noqa: F811
    run_id, (res,) = await _triaged(lh)
    assert res["capture"]["qwen_source"] == "pending"
    before = (await lh.client.get("/api/v1/live/captures", params={"run_id": run_id})).json()["cursor"]
    r = await lh.client.post(_url(run_id, 0), json={"call": 1, "tokens": 42, "fields": FIELDS})
    assert r.status_code == 200 and r.json() == {"updated": True}
    page = (await lh.client.get("/api/v1/live/captures", params={"run_id": run_id, "after": before})).json()
    (cap,) = page["items"]                                   # the cursor (updated_at) picks the draft up
    q = cap["qwen"]
    assert cap["qwen_source"] == "pending" and cap["phase"] == "analysing"
    assert {k: q[k] for k in q if k != "drafted_at"} == {
        "draft": True, "call": 1, "tokens": 42, "answer": "Gauge read fai",
        "evidence": [{"claim": "Gauge read failed", "ids": ["F1", "reading:R9"]}, {"claim": "Critical", "ids": []}],
        "recommended_action": None, "confidence": None,
        "reasoning": "Gauge read failed [F1, reading:R9] Critical []"}
    assert q["drafted_at"]
    # a newer snapshot replaces it (the runner's retry: call 2)
    await lh.client.post(_url(run_id, 0), json={"call": 2, "fields": {"answer": "Re"}})
    q = (await lh.client.get("/api/v1/live/runs/%d" % run_id)).json()["captures"][0]["qwen"]
    assert q["call"] == 2 and q["answer"] == "Re" and q["evidence"] == [] and q["reasoning"] is None
    assert lh.live.draft_calls == 2           # each draft is one conditional UPDATE, no other reads


async def test_draft_is_a_no_op_after_analysis_and_never_counted(lh):  # noqa: F811
    run_id, (res,) = await _triaged(lh)
    await lh.client.post(_url(run_id, 0), json={"call": 1, "fields": FIELDS})
    an = await lh.client.post("/api/v1/live/runs/%d/stops/0/analysis" % run_id, json={
        "system2_request": res["system2_request"], "system2_error": "system2 unreachable"})
    assert an.status_code == 200 and an.json()["qwen_source"] == "template"
    final = an.json()["capture"]["qwen"]
    assert "draft" not in final and final["model"] == "template"
    r = await lh.client.post(_url(run_id, 0), json={"call": 1, "fields": {"answer": "late draft"}})
    assert r.status_code == 200 and r.json() == {"updated": False}
    cap = (await lh.client.get("/api/v1/live/runs/%d" % run_id)).json()["captures"][0]
    assert cap["qwen"] == json.loads(json.dumps(final)) and cap["phase"] == "done"
    fin = (await lh.client.post("/api/v1/live/runs/%d/finish" % run_id, json={"status": "done"})).json()
    assert fin["run"]["counts"]["template"] == 1 and fin["run"]["counts"]["qwen"] == 0


async def test_a_drafted_but_unanalysed_stop_is_not_an_answer(lh):  # noqa: F811
    run_id, (res,) = await _triaged(lh)
    await lh.client.post(_url(run_id, 0), json={"call": 1, "fields": FIELDS})
    fin = (await lh.client.post("/api/v1/live/runs/%d/finish" % run_id, json={"status": "done"})).json()
    assert fin["run"]["counts"]["qwen"] == 0 and fin["run"]["counts"]["template"] == 0
    assert not lh.repo.assessments and not [d for d in lh.repo.documents if d.startswith("explanation:")]
    assert not [n for n in lh.live.nodes if n.startswith("explanation:")]
    # finish() expired the stop: no longer pending (no fast poll, no "Qwen writing"), the draft kept as discarded
    cap = (await lh.client.get("/api/v1/live/runs/%d" % run_id)).json()["captures"][0]
    assert cap["qwen_source"] == "error" and cap["phase"] == "error" and cap["qwen"]["draft"] is True
    r = await lh.client.post(_url(run_id, 0), json={"call": 1, "fields": {"answer": "late"}})
    assert r.status_code == 409 and r.json()["error"] == "live_conflict"      # the run is done


async def test_cancel_expires_pending_stops_and_refuses_later_drafts(lh):  # noqa: F811
    run_id, results = await _triaged(lh, stops=2)
    await lh.client.post("/api/v1/live/runs/%d/stops/0/analysis" % run_id, json={
        "system2_request": results[0]["system2_request"], "system2_error": "system2 unreachable"})
    await lh.client.post(_url(run_id, 1), json={"call": 1, "fields": FIELDS})
    assert (await lh.client.post("/api/v1/live/runs/%d/cancel" % run_id)).status_code == 200
    caps = (await lh.client.get("/api/v1/live/runs/%d" % run_id)).json()["captures"]
    assert [c["qwen_source"] for c in caps] == ["template", "error"]       # an analysed stop is left alone
    assert caps[1]["qwen"]["draft"] is True and caps[1]["qwen"]["answer"] == "Gauge read fai"
    r = await lh.client.post(_url(run_id, 1), json={"call": 1, "fields": {"answer": "after cancel"}})
    assert r.status_code == 409 and "not running" in r.json()["detail"]


async def test_draft_errors(lh):  # noqa: F811
    run_id, _ = await _triaged(lh, stops=1)
    r = await lh.client.post(_url(999, 0), json={"call": 1, "fields": {}})
    assert r.status_code == 404 and r.json()["error"] == "live_not_found"
    r = await lh.client.post(_url(run_id, 8), json={"call": 1, "fields": {}})
    assert r.status_code == 404 and r.json()["error"] == "live_not_found"
    await lh.client.post("/api/v1/live/runs/%d/stops/1/start" % run_id)          # arrived, not triaged yet
    for idx in (1, 2):
        r = await lh.client.post(_url(run_id, idx), json={"call": 1, "fields": {}})
        assert r.status_code == 409 and r.json()["error"] == "live_conflict", idx
    for body in ({"call": 0, "fields": {}}, {"fields": {}}, {"call": 1}, {"call": 1, "fields": []},
                 {"call": 1, "fields": {}, "tokens": -1}):
        assert (await lh.client.post(_url(run_id, 0), json=body)).status_code == 422, body
    big = {"answer": "x" * (16 * 1024)}
    r = await lh.client.post(_url(run_id, 0), json={"call": 1, "fields": big})
    assert r.status_code == 413 and r.json()["error"] == "live_too_large"
    ok = {"answer": "x" * (16 * 1024 - 20)}
    assert (await lh.client.post(_url(run_id, 0), json={"call": 1, "fields": ok})).json() == {"updated": True}


async def test_draft_needs_the_admin_token(lh):  # noqa: F811
    run_id, _ = await _triaged(lh)
    for headers in ({"X-Admin-Token": "nope"}, {"X-Admin-Token": ""}):
        r = await lh.client.post(_url(run_id, 0), json={"call": 1, "fields": {}}, headers=headers)
        assert r.status_code == 401 and r.json()["error"] == "unauthorized"
    assert (await lh.client.post(_url(run_id, 0), json={"call": 1, "fields": {}}, headers=ADMIN)).status_code == 200
