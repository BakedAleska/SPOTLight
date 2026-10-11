"""The ingest pipeline end to end over HTTP, with System 1 mocked and the database faked."""
import httpx
import pytest

from app.services import validator as V
from app.services.reference import Reference

from .conftest import KIT, S1, laya_answers, replay_event, replay_events, system1_route


def _pick(escalates: bool):
    """A replay ACTION_COMPLETED/FAILED event whose rules verdict escalates (or not)."""
    ref = Reference(KIT)
    for e in replay_events():
        ref.normalize(e)
        if e["event_type"] in ("ACTION_COMPLETED", "ACTION_FAILED"):
            kind, ev, _ = ref.canonical(e)
            if kind == "action" and V.rules_verdict(kind, ev)["escalate"] == escalates:
                return e, ev
    raise AssertionError("no such event")


async def _post(h, ev, mode=None):
    params = {"triage_mode": mode} if mode else {}
    r = await h.client.post("/api/v1/events", json=ev, params=params)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("mode", ["serial", "parallel"])
async def test_agreeing_escalation_creates_flag_with_both_verdicts(h, mock, mode):
    raw, canon = _pick(True)
    captured = []
    system1_route(mock, lambda it: laya_answers(it["kind"], event=it["event"]), captured)
    res = await _post(h, raw, mode)
    assert res["kind"] == "action" and res["triage_mode"] == mode
    assert res["decision"]["escalated"] is True and res["decision"]["agreement"] == "confirmed"
    assert res["flag"]["created"] is True
    flag = h.repo.flags[res["flag"]["flag_id"]]
    rules = [r["rule"] for r in flag["reasons"]]
    assert rules[:2] == ["laya_verdict", "rules_verdict"] and len(rules) > 2
    assert flag["triage_event_id"] == canon["event_id"] and flag["priority"] in ("P1", "P2")
    # predictions stored, one row per question
    qs = {p["question"] for p in h.repo.predictions_rows if p["action_id"] == canon["action_id"]}
    assert qs == {"escalate", "priority", "category", "reading_trust"}
    # System 1 got the canonical event (not text)
    item = captured[0]["body"]["items"][0]
    assert item["kind"] == "action" and item["event"]["asset"]["asset_id"] == canon["asset"]["asset_id"]


async def test_one_request_id_across_raw_model_calls_and_request_log(h, mock):
    raw, _ = _pick(True)
    captured = []
    system1_route(mock, lambda it: laya_answers(it["kind"], event=it["event"]), captured)
    r = await h.client.post("/api/v1/events", json=raw, headers={"X-Request-ID": "r-trace-1"})
    assert r.headers["x-request-id"] == "r-trace-1"
    assert r.json()["request_id"] == "r-trace-1"
    assert h.repo.raw[-1]["request_id"] == "r-trace-1"                       # raw.spot_event_log
    assert captured[0]["body"]["request_id"] == "r-trace-1"                  # forwarded to System 1
    assert captured[0]["headers"]["x-request-id"] == "r-trace-1"
    # app.model_calls: a successful call is logged by the System 1 service, not here (audit M2: no duplicate)
    assert [c for c in h.sink.model_calls if c["system"] == "system1"] == []
    assert h.sink.requests[-1]["request_id"] == "r-trace-1"                  # app.request_log
    assert h.sink.requests[-1]["route"] == "/api/v1/events"
    assert h.repo.events[raw["event_id"]]["payload"]["triage"]["request_id"] == "r-trace-1"


async def test_serial_vs_parallel_when_laya_misses(make_harness, mock):
    """Rules escalate, Laya says 'log only': serial drops it, parallel catches it at the rules' priority."""
    raw, canon = _pick(True)
    system1_route(mock, lambda it: laya_answers(it["kind"], escalate=False, priority="P4", event=it["event"]))
    serial = make_harness(triage_mode="serial")
    res = await _post(serial, raw)
    assert res["decision"]["escalated"] is False and res["decision"]["agreement"] == "rules_only_escalate"
    assert res["flag"] is None and not serial.repo.flags
    parallel = make_harness(triage_mode="parallel")
    res = await _post(parallel, raw)
    assert res["decision"]["escalated"] is True
    assert res["decision"]["priority"] == V.rules_verdict("action", canon)["priority"]


async def test_serial_vs_parallel_when_only_laya_escalates(make_harness, mock):
    raw, _ = _pick(False)
    system1_route(mock, lambda it: laya_answers(it["kind"], escalate=True, priority="P1", category="equipment",
                                                event=it["event"]))
    serial = make_harness(triage_mode="serial")
    assert (await _post(serial, raw))["decision"]["escalated"] is False
    parallel = make_harness(triage_mode="parallel")
    res = await _post(parallel, raw)
    assert res["decision"]["escalated"] is True and res["decision"]["priority"] == "P1"
    assert res["decision"]["agreement"] == "rules_rejected"


@pytest.mark.parametrize("mode", ["serial", "parallel"])
@pytest.mark.parametrize("failure", ["connect", "http500", "timeout"])
async def test_system1_down_gives_rules_only_verdict(make_harness, mock, mode, failure):
    h = make_harness(triage_mode=mode)
    raw, canon = _pick(True)
    route = mock.post(S1 + "/v1/predict")
    if failure == "connect":
        route.mock(side_effect=httpx.ConnectError("refused"))
    elif failure == "timeout":
        route.mock(side_effect=httpx.ReadTimeout("slow"))
    else:
        route.mock(return_value=httpx.Response(500, text="boom"))
    res = await _post(h, raw)
    assert res["system1"]["status"] == "laya_unavailable"
    rv = V.rules_verdict("action", canon)
    assert res["decision"]["escalated"] is rv["escalate"] and res["decision"]["priority"] == rv["priority"]
    assert res["decision"]["agreement"] == "laya_unavailable"
    flag = h.repo.flags[res["flag"]["flag_id"]]
    assert flag["reasons"][0]["rule"] == "laya_unavailable"
    assert h.sink.model_calls[0]["ok"] is False


async def test_system1_unset_is_rules_only(make_harness):
    h = make_harness(system1_url="")
    raw, _ = _pick(True)
    res = await _post(h, raw)
    assert res["system1"]["status"] == "laya_unavailable" and res["flag"]["created"]


async def test_database_down_still_returns_verdict(h, mock):
    system1_route(mock, lambda it: laya_answers(it["kind"], event=it["event"]))
    h.repo.down = True
    raw, _ = _pick(True)
    res = await _post(h, raw)
    assert res["persisted"] is False and res["decision"]["escalated"] is True and res["flag"] is None
    assert len(res["warnings"]) == 1 and res["warnings"][0].startswith("db unavailable")


async def test_pre_action_is_a_forecast_not_a_flag(h, mock):
    system1_route(mock, lambda it: laya_answers(it["kind"]))
    res = await _post(h, replay_event("E00002"))
    assert res["kind"] == "pre_action" and res["forecast"]["p_fail"] == 0.64
    assert res.get("decision") is None and not h.repo.flags
    assert {p["question"] for p in h.repo.predictions_rows} == {"will_fail", "failure_mode", "anomaly_expected"}


async def test_mission_started_is_stored_not_triaged(h, mock):
    res = await _post(h, replay_event("E00001"))
    assert res["kind"] is None and "MISSION_STARTED" in res["skipped"]
    assert "E00001" in h.repo.events and h.repo.raw[0]["source"] == "api"


async def test_mission_completed_green_but_broken(h, mock):
    system1_route(mock, lambda it: laya_answers(it["kind"], event=it["event"]))
    res = await _post(h, replay_event("E00022"))      # M001: COMPLETED with failures + a HIGH anomaly
    assert res["kind"] == "mission" and res["decision"]["escalated"] is True
    assert "mission_misleading_serious" in res["rules"]["fired"]
    assert res["decision"]["title"] == "Mission M001 marked complete, but 2 of 10 actions failed and 1 serious anomaly"


async def test_raw_is_idempotent_and_enriched_payload(h, mock):
    system1_route(mock, lambda it: laya_answers(it["kind"], event=it["event"]))
    raw, canon = _pick(True)
    first = await _post(h, raw)
    second = await _post(h, raw)
    assert first["raw_duplicate"] is False and second["raw_duplicate"] is True
    assert second["flag"]["created"] is False                       # upsert on triage_event_id
    payload = h.repo.events[raw["event_id"]]["payload"]
    assert payload["action_id"] == canon["action_id"] and "enriched" in payload


async def test_batch_gives_each_event_its_own_request_id(h, mock):
    system1_route(mock, lambda it: laya_answers(it["kind"], event=it["event"]))
    evs = [replay_event(i) for i in ("E00001", "E00002", "E00003")]
    r = await h.client.post("/api/v1/events/batch", json={"events": evs}, headers={"X-Request-ID": "r-b"})
    body = r.json()
    assert r.status_code == 200 and body["count"] == 3
    assert [x["request_id"] for x in body["results"]] == ["r-b-000", "r-b-001", "r-b-002"]


async def test_invalid_event_is_422_not_500(h):
    r = await h.client.post("/api/v1/events", json={"event_id": "X", "timestamp": "t", "event_type": "NOPE"})
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"


# ── integration fixes 2026-10-10 (docs/integration-log-2026-10-10.md; audit H1, H2) ────────────────
def test_breaker_cooldown_is_at_least_each_timeout(make_harness):
    """H1: a breaker that reopens before a hung call times out stalls the replay once per cooldown."""
    h = make_harness(breaker_cooldown_s=10.0, system1_timeout_s=30.0, embed_timeout_s=20.0, mcp_timeout_s=60.0,
                     system2_timeout_s=60.0, system1_ingest_timeout_s=8.0)
    s = h.svc
    assert s.system1.breaker.cooldown_s == 30.0 and s.system1.ingest_timeout_s == 8.0
    assert s.embed.breaker.cooldown_s == 20.0 and s.mcp.breaker.cooldown_s == 60.0
    assert s.system2.breaker.cooldown_s == 60.0
    g = make_harness(breaker_cooldown_s=10.0, system1_timeout_s=5.0)     # GPU defaults: unchanged
    assert g.svc.system1.breaker.cooldown_s == 10.0 and g.svc.system1.ingest_timeout_s == 5.0


async def test_ingest_does_not_queue_behind_a_batch_call(h, mock):
    """H2: while a forecast/batch call holds Laya, ingest goes rules-only (marked) instead of waiting."""
    raw, canon = _pick(True)
    route = system1_route(mock, lambda it: laya_answers(it["kind"], event=it["event"]))
    h.svc.system1.batch_inflight = 1
    res = await _post(h, raw)
    assert route.call_count == 0
    assert res["system1"]["status"] == "laya_unavailable" and res["system1"]["error"].startswith("busy")
    assert res["decision"]["agreement"] == "laya_unavailable"
    assert h.svc.system1.breaker.allow()                     # busy is not a failure: the breaker stays closed
    h.svc.system1.batch_inflight = 0
    res = await _post(h, raw)
    assert route.call_count == 1 and res["system1"]["status"] == "ok"
