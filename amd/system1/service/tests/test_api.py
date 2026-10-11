"""Request validation and response shape against docs/backend-services-contract.md (no model needed)."""
import copy
import logging

import pytest
from conftest import FakeEngine, events_for
from fastapi.testclient import TestClient

from service.app import create_app
from service.config import Settings
from service.schemas import EVENT_MODELS
from triage.questions import QUESTION_SETS, QUESTIONS
from triage.render import render_json


def client_for(engine=None, **settings):
    app = create_app(Settings(**settings), engine=engine or FakeEngine())
    return TestClient(app)


@pytest.fixture()
def client():
    with client_for() as c:
        yield c


def test_health_and_questions(client):
    assert client.get("/health").json()["model_loaded"] is True
    body = client.get("/v1/questions").json()
    assert body == {"questions": QUESTIONS, "question_sets": QUESTION_SETS}
    info = client.get("/v1/model").json()
    assert {"checkpoint", "fine_tuned", "temperatures", "device"} <= set(info)


def test_predict_shape(client, sample_items):
    r = client.post("/v1/predict", json={"request_id": "r-1", "items": sample_items})
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"model", "fine_tuned", "device", "latency_ms", "items"} <= set(body)
    assert isinstance(body["latency_ms"], int) and body["request_id"] == "r-1"
    assert [i["id"] for i in body["items"]] == [i["id"] for i in sample_items]
    for item in body["items"]:
        assert list(item["answers"]) == QUESTION_SETS[item["kind"]]
        for qid, a in item["answers"].items():
            opts = list(QUESTIONS[qid]["criteria"])
            assert set(a) == {"answer", "probs", "confidence"}
            assert a["answer"] in opts and list(a["probs"]) == opts
            assert abs(sum(a["probs"].values()) - 1) < 1e-6
            assert a["confidence"] == pytest.approx(a["probs"][a["answer"]])


def test_one_batch_per_request():
    eng = FakeEngine()
    with client_for(eng) as c:
        items = [{"id": "x%d" % i, "kind": "mission", "event": e} for i, e in enumerate([MISSION] * 5)]
        assert c.post("/v1/predict", json={"items": items}).status_code == 200
    assert len(eng.calls) == 1 and len(eng.calls[0]) == 5


def test_question_subset(client, sample_items):
    item = dict(sample_items[1], questions=["priority", "escalate", "priority"])
    body = client.post("/v1/predict", json={"items": [item]}).json()
    assert list(body["items"][0]["answers"]) == ["priority", "escalate"]


def test_validation_names_the_field(client, sample_items):
    bad = copy.deepcopy(sample_items[1])
    bad["event"]["asset"]["criticality"] = "SEVERE"
    r = client.post("/v1/predict", json={"items": [sample_items[0], bad]})
    assert r.status_code == 422
    body = r.json()
    assert body["detail"][0]["loc"] == ["body", "items", 1, "event", "asset", "criticality"]
    assert body["error"].startswith("items.1.event.asset.criticality")


@pytest.mark.parametrize("mutate, loc", [
    (lambda it: it["event"].pop("battery_pct"), ["body", "items", 0, "event", "battery_pct"]),
    (lambda it: it["event"].update(status="DONE"), ["body", "items", 0, "event", "status"]),
    (lambda it: it["event"]["reading"].update(value="hot") if it["event"]["reading"] else
        it["event"].update(reading={"measurement_type": "T", "value": "hot", "unit": "C", "quality": "GOOD"}),
     ["body", "items", 0, "event", "reading", "value"]),
    (lambda it: it.update(questions=["will_fail"]), ["body", "items", 0, "questions", 0]),
    (lambda it: it.update(questions=[]), ["body", "items", 0, "questions"]),
    (lambda it: it.update(kind="mission"), None),            # an action event sent as a mission
    (lambda it: it["event"].update(kind="mission"), ["body", "items", 0, "event", "kind"]),
    (lambda it: it.update(kind="telemetry"), ["body", "items", 0, "kind"]),
    (lambda it: it.pop("id"), ["body", "items", 0, "id"]),
])
def test_validation_cases(client, sample_items, mutate, loc):
    item = copy.deepcopy(sample_items[1])
    mutate(item)
    r = client.post("/v1/predict", json={"items": [item]})
    assert r.status_code == 422, r.text
    locs = [e["loc"] for e in r.json()["detail"]]
    if loc is not None:
        assert loc in locs, locs
    else:
        assert all(l[:4] == ["body", "items", 0, "event"] for l in locs)


def test_batch_limits(client, sample_items):
    assert client.post("/v1/predict", json={"items": []}).status_code == 422
    r = client.post("/v1/predict", json={"items": [sample_items[2]] * 65})
    assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", "items"]
    assert client.post("/v1/predict", json={"items": [sample_items[2]] * 64}).status_code == 200


def test_validation_preserves_rendering(kit):
    """Validated events render to exactly the state the model was trained on, for every kit event."""
    n = 0
    for mid in kit.missions:
        if not kit.actions_of(mid):
            continue
        for ev in events_for(kit, mid):
            clean = EVENT_MODELS[ev["kind"]].model_validate(ev).model_dump()
            assert render_json(dict(clean, kind=ev["kind"])) == render_json(ev), ev["event_id"]
            n += 1
    assert n > 500


def test_logging_failure_never_fails_a_request(sample_items):
    with client_for(logs_database_url="postgresql://nobody:x@127.0.0.1:1/none") as c:
        r = c.post("/v1/predict", json={"items": sample_items})
        assert r.status_code == 200


def test_inference_error_is_500(sample_items):
    with client_for(FakeEngine(fail=True)) as c:
        r = c.post("/v1/predict", json={"items": sample_items})
        assert r.status_code == 500 and "inference failed" in r.json()["error"]


MISSION = {
    "kind": "mission", "mission_id": "M999", "configuration_version": "V2", "timestamp": "2026-09-03T10:31:00",
    "reported_status": "COMPLETED", "duration_min": 31, "start_battery_pct": 80, "end_battery_pct": 22,
    "actions_total": 10, "actions_failed": 2, "failure_types": ["OBSTACLE_DETECTION"],
    "anomalies_by_severity": {"LOW": 1, "MEDIUM": 0, "HIGH": 1, "CRITICAL": 0},
    "worst_anomaly": {"anomaly_type": "THERMAL_HOTSPOT", "severity": "HIGH", "asset_name": "Pump 01",
                      "asset_criticality": "CRITICAL"},
}


# ── /health `logging` and the startup level (ADDED 2026-10-11) ──────────────────────────────────────
def _ready_records(caplog):
    return [r for r in caplog.records if r.name == "system1" and "System 1 ready" in r.getMessage()]


def test_health_reports_logging_off_and_warns(caplog):
    caplog.set_level(logging.INFO)
    with client_for() as c:
        assert c.get("/health").json()["logging"] is False
    (rec,) = _ready_records(caplog)
    assert rec.levelno == logging.WARNING and "OFF: set LOGS_DATABASE_URL" in rec.getMessage()


def test_health_reports_logging_on_without_leaking_the_dsn(caplog):
    caplog.set_level(logging.INFO)
    with client_for(logs_database_url="postgresql://nobody:SECRETPW@127.0.0.1:1/none") as c:
        h = c.get("/health").json()
        assert h["logging"] is True and h["model_loaded"] is True
    (rec,) = _ready_records(caplog)
    assert rec.levelno == logging.INFO and rec.getMessage().endswith("(model_calls logging on)")
    assert all("SECRETPW" not in r.getMessage() for r in caplog.records)


def test_health_before_lifespan_says_logging_false():
    c = TestClient(create_app(Settings(), engine=FakeEngine()))   # no `with`: the lifespan never ran
    assert c.get("/health").json()["logging"] is False
