"""REPLAY_MODE=stepped (serverless): the run lives in replay_runs (FakeRepo here) and POST /replay/step advances it.
Contract: docs/backend-services-contract.md "Serverless deployment and stepped replay"."""
import datetime as dt

import pytest

from app.services.replay import load_replay, paced_offsets

from .conftest import KIT, laya_answers, system1_route


@pytest.fixture
async def hs(make_harness, mock):
    system1_route(mock, lambda it: laya_answers(it["kind"], event=it.get("event")))
    harness = make_harness(replay_mode="stepped")
    yield harness
    await harness.client.aclose()


async def _step(h, **body):
    r = await h.client.post("/api/v1/replay/step", json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def test_status_is_idle_before_any_run(hs):
    r = await hs.client.get("/api/v1/replay/status")
    assert r.json() == {"state": "idle", "mode": "stepped"}
    st = await _step(hs)
    assert st["state"] == "idle" and st["step"]["done"] and st["step"]["ingested"] == 0


async def test_start_does_nothing_until_stepped_then_finishes(hs):
    r = await hs.client.post("/api/v1/replay/start", json={"speed": 0, "limit": 22})
    st = r.json()
    assert r.status_code == 200 and st["state"] == "running" and st["mode"] == "stepped"
    assert st["total"] == 22 and st["sent"] == 0 and st["cursor"] == 0 and st["next_due_in_s"] == 0
    assert hs.repo.events == {}                                  # no background task: nothing ingested yet
    st = await _step(hs)
    assert st["state"] == "finished" and st["step"] == {**st["step"], "ingested": 22, "busy": False, "done": True}
    assert st["sent"] == 22 and st["errors"] == 0 and st["escalated"] >= 1 and st["next_due_in_s"] is None
    assert st["last_event"]["event_id"] == "E00022"
    assert len(hs.repo.events) == 22 and hs.repo.raw[0]["request_id"] == "rp1-E00001"
    assert {r["route"] for r in hs.sink.requests if r["request_id"].startswith("rp1-")} == {"replay"}
    assert hs.sink.jobs[0]["job"] == "replay" and hs.sink.jobs[0]["status"] == "finished"
    assert hs.sink.jobs[0]["counts"]["sent"] == 22
    assert (await hs.client.get("/api/v1/replay/status")).json()["state"] == "finished"


async def test_steps_resume_from_the_cursor_without_duplicates(hs):
    await hs.client.post("/api/v1/replay/start", json={"speed": 0, "limit": 25})
    st = await _step(hs, max_events=10)
    assert st["state"] == "running" and st["cursor"] == 10 and st["sent"] == 10 and not st["step"]["done"]
    st = await _step(hs, max_events=10)
    assert st["cursor"] == 20 and st["sent"] == 20
    st = await _step(hs, max_events=10)
    assert st["state"] == "finished" and st["sent"] == 25 and st["step"]["ingested"] == 5
    ids = [r["request_id"] for r in hs.repo.raw]
    assert len(ids) == len(set(ids)) == 25 and hs.repo.replay_advances == 25   # progress saved after every event
    st = await _step(hs)                                       # stepping a finished run is a no-op
    assert st["step"]["ingested"] == 0 and st["step"]["done"] and len(hs.repo.raw) == 25


async def test_pacing_follows_robot_time(hs):
    await hs.client.post("/api/v1/replay/start", json={"speed": 1, "max_gap_s": 5, "limit": 6})
    st = await _step(hs)
    assert st["step"]["ingested"] == 1 and st["state"] == "running"   # E00002 is due 5 s (capped) after E00001
    assert 0 < st["next_due_in_s"] <= 5
    hs.repo.replay_clock_skew = dt.timedelta(hours=1)                  # time passes
    st = await _step(hs)
    assert st["state"] == "finished" and st["sent"] == 6


def test_paced_offsets_match_the_background_sleeps():
    events = load_replay(KIT / "replay.ndjson")[:5]   # 18:00:00, 18:03:06, 18:03:06, 18:06:12, 18:06:12
    assert paced_offsets(events, 60, 2) == [0.0, 2.0, 2.0, 4.0, 4.0]
    assert paced_offsets(events, 186, 10) == [0.0, 1.0, 1.0, 2.0, 2.0]
    assert paced_offsets(events, 0, 2) == [0.0] * 5


async def test_second_start_is_409_and_stop_ends_the_run(hs):
    assert (await hs.client.post("/api/v1/replay/start", json={"speed": 0})).status_code == 200
    assert (await hs.client.post("/api/v1/replay/start", json={})).status_code == 409
    await _step(hs, max_events=3)
    st = (await hs.client.post("/api/v1/replay/stop")).json()
    assert st["state"] == "stopped" and st["sent"] == 3
    st = await _step(hs)
    assert st["step"]["ingested"] == 0 and st["step"]["done"] and len(hs.repo.raw) == 3
    assert hs.sink.jobs[0]["status"] == "stopped"
    assert (await hs.client.post("/api/v1/replay/start", json={"speed": 0, "limit": 1})).json()["run"] == 2


async def test_a_held_lease_makes_other_steps_busy(hs):
    await hs.client.post("/api/v1/replay/start", json={"speed": 0, "limit": 5})
    assert await hs.repo.replay_claim("another-instance", 60) is not None
    st = await _step(hs)
    assert st["step"]["busy"] and not st["step"]["done"] and st["step"]["ingested"] == 0 and hs.repo.raw == []
    hs.repo.replay_runs[0]["lease_until"] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)  # expired
    st = await _step(hs)
    assert st["state"] == "finished" and st["sent"] == 5


async def test_a_stop_during_a_step_is_never_overwritten(hs):
    await hs.client.post("/api/v1/replay/start", json={"speed": 0, "limit": 10})
    ingest = hs.svc.pipeline.ingest

    async def stop_after_two(raw, **kw):
        out = await ingest(raw, **kw)
        if len(hs.repo.raw) == 2:
            await hs.repo.replay_stop()                        # e.g. POST /replay/stop on another instance
        return out
    hs.svc.pipeline.ingest = stop_after_two
    st = await _step(hs)
    assert st["state"] == "stopped" and st["step"]["ingested"] == 2 and len(hs.repo.raw) == 2
    assert hs.repo.replay_runs[0]["next_index"] == 1           # the lost-lease write was refused


async def test_reset_demo_stops_a_stepped_run(hs):
    await hs.client.post("/api/v1/replay/start", json={"speed": 0, "limit": 5})
    r = await hs.client.post("/api/v1/ops/reset-demo", json={"confirm": True})
    assert r.status_code == 200 and r.json()["replay"]["state"] == "stopped"
    st = (await hs.client.get("/api/v1/replay/status")).json()
    assert st["state"] == "stopped" and st["mode"] == "stepped" and hs.sink.jobs[0]["status"] == "stopped"


async def test_stepped_mode_without_a_database_is_503(make_harness):
    h = make_harness(replay_mode="stepped")
    h.repo.down = True
    r = await h.client.post("/api/v1/replay/start", json={"speed": 0})
    assert r.status_code == 503 and r.json()["error"] == "database_unavailable"
    assert (await h.client.post("/api/v1/replay/step", json={})).status_code == 503
    health = (await h.client.get("/health", params={"deep": "false"})).json()
    assert health["replay"] == {"state": "idle", "mode": "stepped"}
    await h.client.aclose()


async def test_step_is_409_in_background_mode(h):
    r = await h.client.post("/api/v1/replay/step", json={})
    assert r.status_code == 409 and "background" in r.json()["detail"]


@pytest.mark.parametrize("body", [{"budget_s": 0}, {"budget_s": 61}, {"max_events": 0}, {"max_events": 501}])
async def test_step_validates_its_budget(hs, body):
    assert (await hs.client.post("/api/v1/replay/step", json=body)).status_code == 422
