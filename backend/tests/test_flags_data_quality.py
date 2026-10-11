"""Data quality of what /context, /explain and /ack write and send (data audit 2026-10-11, package PKG-B).

Unit part (FakeRepo): the SQL fallback packet passes System 2's own request validation and packet normalization
(asset_history is a list, allowed_actions are set, the MCP notice is a non-blocking note), System 2 provenance is
stored, blank notes are no notes, the debrief breaker is separate from the assess breaker, and so on.

Database part (`integration` marker, the local Docker pgvector DB, like test_integration.py): blank notes stored as
NULL, asset history bounded to robot time, a valid packet for every seeded flag, the guarded assessment columns,
and reset-demo removing flag graph nodes but keeping live ones. Set INTEGRATION_DATABASE_URL to use a running DB.
"""
import asyncio
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import psycopg
import pytest

from app.schemas.flags import AckIn
from app.services import validator as V
from app.services.flags import FlagService, collect_ids
from app.services.http import ServiceUnavailable
from app.services.system2 import ALLOWED_ACTIONS, normalize_assessment, suggested_action

from .conftest import EMBED, MCP, REPO_ROOT, S2, system1_route, laya_answers
from .test_fallbacks import _action_flag, _flag

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "amd/system2"))
from analyst.backend_contract import BackendAssessRequest  # noqa: E402
from analyst.packet import normalize_packet  # noqa: E402

HISTORY = {"anomalies": [{"anomaly_id": "AN016", "action_id": "A0168", "mission_id": "M017",
                          "timestamp": "2026-09-07T02:24:48", "anomaly_type": "GAUGE_OUT_OF_RANGE", "severity": "LOW"}],
           "prior_flags": [{"flag_id": 9001, "title": "Earlier gauge flag", "severity": "HIGH", "status": "OPEN",
                            "created_at": "2026-10-10T23:00:00+00:00", "notes": "valve checked"},
                           {"flag_id": 9002, "title": "Another", "severity": "CRITICAL", "status": "RESOLVED",
                            "notes": None}]}


def _prepared(fid, flag, packet):
    """System 2's view of the body the backend would send (same JSON round trip as System2Client._post)."""
    body = json.loads(json.dumps({"flag_id": fid, "flag": flag, "context": packet}, default=str))
    return normalize_packet(BackendAssessRequest.model_validate(body))


# ── SQL fallback packet → System 2 contract ─────────────────────────────────────────────────────
async def test_sql_packet_passes_system2_normalize_for_action_and_mission_flags(h, mock):
    async def history(asset_id, exclude_flag_id):
        return HISTORY
    h.repo.asset_history = history
    mock.post(MCP).mock(side_effect=httpx.ConnectError("refused"))
    for fid in (await _action_flag(h, mock), await _flag(h, mock, "E00022")):
        packet = (await h.client.get("/api/v1/flags/%d/context" % fid)).json()
        flag = h.repo.flags[fid]
        assert packet["source"] == "sql_fallback" and "gaps" not in packet
        assert packet["notes"][0].startswith("MCP unavailable") and packet["question"]
        category = "mission" if not flag.get("action_id") else flag["category"]
        assert packet["allowed_actions"] == ALLOWED_ACTIONS[category]
        assert packet["suggested_action"] in packet["allowed_actions"]
        p = _prepared(fid, flag, packet)
        assert not p.insufficient_context and p.suggested_action == packet["suggested_action"]
        assert set(packet["citation_ids"]) <= set(p.content) and "flag:%d" % fid in packet["citation_ids"]
        if flag.get("action_id"):
            assert isinstance(packet["asset_history"], list)
            hist = {i["id"]: i for i in packet["asset_history"]}
            assert hist["anomaly:AN016"]["severity"] == "LOW"
            assert hist["flag:9001"]["note"] == "valve checked" and "note" not in hist["flag:9002"]
            assert "anomaly:AN016" in p.content and "flag:9001" in p.content
        else:
            assert packet["suggested_action"] == "REVIEW_MISSION"


async def test_sql_packet_with_gap_is_still_insufficient(h, mock):
    """Only real lookup failures go to `gaps`, and those still make System 2 refuse (no silent evidence loss)."""
    fid = await _action_flag(h, mock)
    h.repo.down = True
    row = {"flag": dict(h.repo.flags[fid]), "acks": []}
    packet = await h.svc.flags.minimal_packet(row)
    h.repo.down = False
    assert packet["gaps"] and packet["gaps"][0].startswith("some SQL lookups failed")
    assert _prepared(fid, row["flag"], packet).insufficient_context


async def test_flag_service_context_budget_note_on_deadline(make_harness, mock):
    """MCP slower than the deadline: the SQL packet carries the deadline notice as a note, not a gap."""
    h = make_harness(request_deadline_s=1.0, mcp_timeout_s=60.0)
    fid = await _action_flag(h, mock)

    async def hang(request):
        await asyncio.sleep(30)
        return httpx.Response(200)
    mock.post(MCP).mock(side_effect=hang)
    body = (await h.client.get("/api/v1/flags/%d/context" % fid)).json()
    assert body["fallback_reason"].startswith("deadline:") and body["notes"][0].startswith("deadline:")
    assert "gaps" not in body


def test_allowed_actions_mirror_the_mcp_server():
    """services/system2.ALLOWED_ACTIONS is a copy of amd/mcp/spot_mcp/domain.py's: they must not drift."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("spot_mcp_domain_copy", REPO_ROOT / "amd/mcp/spot_mcp/domain.py")
    domain = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(domain)
    assert ALLOWED_ACTIONS == domain.ALLOWED_ACTIONS
    for category in ALLOWED_ACTIONS:   # every validator suggestion is allowed for its own category
        for priority in ("P1", "P2", "P3", "P4"):
            for facts in ({}, {"battery_pct": 5, "failure_type": "COMMUNICATION_LOSS"}, {"severity": "CRITICAL"},
                          {"failure_type": "PATH_BLOCKED"}, {"environment": "LOW_LIGHT"}, {"battery_pct": 10}):
                if category != "none":
                    assert suggested_action(category, priority, facts) in ALLOWED_ACTIONS[category]


def test_collect_ids_skips_objects_with_only_an_id():
    packet = {"flag": {"id": "flag:1", "severity": "HIGH"}, "event": {"id": "action:A1", "asset": {"id": "asset:X"}},
              "citation_ids": ["old"]}
    assert collect_ids(packet) == ["flag:1", "action:A1"]


async def test_old_laya_unavailable_detail_is_not_sent_as_evidence(h, mock):
    fid = await _action_flag(h, mock)
    flag = dict(h.repo.flags[fid])
    flag["reasons"] = [{"rule": "laya_unavailable", "detail": "System 1 (Laya) unavailable: circuit open after: "
                                                              "ConnectError: All connection attempts failed"}]
    packet = await h.svc.flags.minimal_packet({"flag": flag, "acks": []})
    assert packet["flag"]["reasons"][0]["detail"] == V.LAYA_UNAVAILABLE_DETAIL


def test_laya_unavailable_reason_has_stable_text_without_the_raw_error():
    rules = {"escalate": True, "priority": "P2", "category": "equipment", "rules": [], "policy_version": "t"}
    d = V.combine("serial", None, rules, "action")
    r = V.build_reasons("serial", None, "ConnectError: All connection attempts failed", rules, d, None)
    assert r[0] == {"rule": "laya_unavailable", "source": "system1", "weight": 0,
                    "detail": "System 1 (Laya) unavailable; rules-only verdict"}


# ── explain: provenance ─────────────────────────────────────────────────────────────────────────
def test_normalize_assessment_defaults_severity_and_reasoning():
    out = {"answer": "Insufficient supplied context; request manual review.", "evidence": [],
           "recommended_action": "MANUAL_REVIEW", "confidence": "low", "model": "stub", "latency_ms": 0}
    a = normalize_assessment(out, default_severity="HIGH")
    assert a["severity_assessment"] == "HIGH" and a["reasoning"] == out["answer"]
    assert normalize_assessment({**out, "severity_assessment": "LOW"}, "HIGH")["severity_assessment"] == "LOW"


async def test_explain_stores_system2_template_provenance_and_cache_returns_it(make_harness, mock):
    h = make_harness(system2_url=S2)
    stored = {}
    real_upsert = h.repo.upsert_assessment

    async def upsert(flag_id, a, state="DONE"):   # the SQL Repo writes these columns after migration 014
        row = await real_upsert(flag_id, a, state)
        row.update({k: a.get(k) for k in ("source", "fallback_reason")})
        stored.update(row)
        return row
    h.repo.upsert_assessment = upsert
    fid = await _action_flag(h, mock)
    mock.post(MCP).mock(side_effect=httpx.ConnectError("refused"))
    mock.post(S2 + "/v1/assess").mock(return_value=httpx.Response(200, json={
        "flag_id": fid, "question": "q?", "answer": "Insufficient supplied context; request manual review.",
        "evidence": [], "recommended_action": "MANUAL_REVIEW", "confidence": "low", "insufficient_context": True,
        "model": "stub", "latency_ms": 0, "tokens_per_sec": None, "path": "insufficient_context"}))
    body = (await h.client.post("/api/v1/flags/%d/explain" % fid)).json()
    assert body["source"] == "system2_template" and stored["state"] == "TEMPLATE"
    assert stored["source"] == "system2_template" and stored["fallback_reason"].startswith("System 2 answered")
    assert stored["latency_ms"] is None and stored["severity_assessment"] == h.repo.flags[fid]["severity"]
    assert stored["reasoning"] == body["answer"]
    h.svc.system2.base_url = None                   # nothing to retry with: the stored template is served
    again = (await h.client.post("/api/v1/flags/%d/explain" % fid)).json()
    assert again["cached"] and again["source"] == "system2_template"
    assert again["fallback_reason"] == stored["fallback_reason"]


async def test_system2_explanation_doc_has_completed_at_and_is_stored_when_embed_is_down(make_harness, mock):
    """A System 2 answer is indexed with ts = assessments.completed_at; embedder down -> stored with a NULL vector."""
    h = make_harness(system2_url=S2)
    done_at = dt.datetime(2026, 10, 11, 8, 0, tzinfo=dt.timezone.utc)
    real_upsert = h.repo.upsert_assessment

    async def upsert(flag_id, a, state="DONE"):   # the SQL Repo returns completed_at (RETURNING *)
        return {**await real_upsert(flag_id, a, state), "completed_at": done_at}
    h.repo.upsert_assessment = upsert
    fid = await _action_flag(h, mock)
    mock.post(MCP).mock(side_effect=httpx.ConnectError("refused"))
    mock.post(EMBED + "/v1/embeddings").mock(side_effect=httpx.ConnectError("refused"))
    mock.post(S2 + "/v1/assess").mock(return_value=httpx.Response(200, json={
        "question": "q?", "answer": "No, escalate.", "evidence": [{"claim": "gauge high", "ids": ["F1"]}],
        "recommended_action": "DISPATCH_TECHNICIAN", "confidence": "high", "model": "qwen-test", "path": "llm",
        "latency_ms": 900}))
    body = (await h.client.post("/api/v1/flags/%d/explain" % fid)).json()
    assert body["source"] == "system2" and body["reasoning"] == "gauge high [F1]"
    assert body["severity_assessment"] == h.repo.flags[fid]["severity"]
    await h.sink.drain()
    doc = h.repo.documents["explanation:%d" % body["assessment_id"]]
    assert doc["ts"] == done_at and doc["embedding"] is None and doc["embed_model"] is None


# ── System 2 breaker ────────────────────────────────────────────────────────────────────────────
async def test_debrief_breaker_is_independent_of_the_assess_breaker(make_harness, mock):
    h = make_harness(system2_url=S2, breaker_cooldown_s=60.0)
    s2 = h.svc.system2
    mock.post(S2 + "/v1/debrief").mock(return_value=httpx.Response(503, text="down"))
    body = (await h.client.get("/api/v1/missions/M017/debrief")).json()
    assert body["source"] == "template"
    assert s2.debrief_breaker.state == "open" and s2.breaker.state == "closed"
    mock.post(S2 + "/v1/debrief").mock(return_value=httpx.Response(404, text="Not Found"))
    s2.debrief_breaker.reset()
    with pytest.raises(ServiceUnavailable):
        await s2.debrief({"mission_id": "M017"}, "r1")
    assert s2.debrief_breaker.state == "closed"      # a 404 route is not an outage


@pytest.mark.parametrize("status,opens", [(422, False), (401, False), (404, False), (429, True), (408, True),
                                          (503, True)])
async def test_assess_4xx_does_not_open_the_breaker(make_harness, mock, status, opens):
    h = make_harness(system2_url=S2, breaker_cooldown_s=60.0)
    mock.post(S2 + "/v1/assess").mock(return_value=httpx.Response(status, text="nope"))
    with pytest.raises(ServiceUnavailable):
        await h.svc.system2.assess({"flag_id": 1}, request_id="r1", flag_id=1, action_id=None)
    assert (h.svc.system2.breaker.state == "open") is opens
    assert h.sink.model_calls[-1]["ok"] is False and "HTTP %d" % status in h.sink.model_calls[-1]["error"]


# ── ack ─────────────────────────────────────────────────────────────────────────────────────────
def test_ackin_blank_note_is_none():
    assert AckIn(note="").note is None and AckIn(note=" \n\t ").note is None and AckIn().note is None
    assert AckIn(note="  gauge recalibrated \n").note == "gauge recalibrated"


async def test_blank_ack_note_is_stored_as_null_and_not_indexed(h, mock):
    fid = await _action_flag(h, mock)
    body = (await h.client.post("/api/v1/flags/%d/ack" % fid, json={"action": "RESOLVE", "note": "   "})).json()
    assert body["ack"]["note"] is None and body["rag_error"] == "no note: nothing to index"
    assert not h.repo.documents


async def test_ack_note_is_stored_without_a_vector_when_embed_is_down(h, mock):
    fid = await _action_flag(h, mock)
    mock.post(EMBED + "/v1/embeddings").mock(side_effect=httpx.ConnectError("refused"))
    body = (await h.client.post("/api/v1/flags/%d/ack" % fid, json={"note": "looked at it"})).json()
    assert body["rag_indexed"] is False and "embedding service unavailable" in body["rag_error"]
    doc = h.repo.documents[body["doc_id"]]
    assert body["doc_id"] == "note:%d" % body["ack"]["ack_id"] and doc["embedding"] is None
    assert doc["embed_model"] is None and doc["ts"] == h.repo.acks[-1]["created_at"]


# ── search / replay ─────────────────────────────────────────────────────────────────────────────
async def test_search_blank_filters_mean_no_filter(h, mock):
    seen = []

    async def fulltext(q, kinds, asset, location, mission, k):
        seen.append((asset, location, mission))
        return []
    h.repo.search_fulltext = fulltext
    mock.post(EMBED + "/v1/embeddings").mock(side_effect=httpx.ConnectError("refused"))
    r = await h.client.get("/api/v1/search", params={"q": "gauge", "asset_id": "", "location_id": " ",
                                                      "mission_id": "M001"})
    assert r.status_code == 200 and seen == [(None, None, "M001")]


async def test_replay_reset_is_logged_as_a_reset_demo_job(h, mock):
    system1_route(mock, lambda it: laya_answers(it["kind"], event=it.get("event")))
    r = await h.client.post("/api/v1/replay/start", json={"reset": True, "speed": 0, "limit": 1})
    assert r.status_code == 200
    await h.client.post("/api/v1/replay/stop")
    reset = h.sink.jobs[0]
    assert reset["job"] == "reset-demo" and reset["status"] == "done" and "flags" in reset["counts"]
    assert reset["metadata"]["via"] == "replay/start"


async def test_replay_reset_failure_is_logged_as_an_error_job_and_the_replay_still_runs(h, mock):
    system1_route(mock, lambda it: laya_answers(it["kind"], event=it.get("event")))

    async def broken(clear_predictions):
        raise psycopg.OperationalError("connection lost")
    h.repo.reset_demo = broken
    r = await h.client.post("/api/v1/replay/start", json={"reset": True, "speed": 0, "limit": 1})
    assert r.status_code == 200 and "connection lost" in r.json()["reset_error"]
    await h.client.post("/api/v1/replay/stop")
    reset = h.sink.jobs[0]
    assert reset["job"] == "reset-demo" and reset["status"] == "error" and "connection lost" in reset["error"]


# ── database (local Docker pgvector) ────────────────────────────────────────────────────────────
DB_DIR = REPO_ROOT / "db"
LOCAL_URL = "postgresql://postgres:postgres@localhost:5433/spot"


def _skip_reason():
    if os.environ.get("SKIP_INTEGRATION") == "1":
        return "SKIP_INTEGRATION=1"
    if os.environ.get("INTEGRATION_DATABASE_URL"):
        return None
    if not (DB_DIR / "setup.py").exists() or not (DB_DIR / "local_pgvector.sh").exists():
        return "db/setup.py or db/local_pgvector.sh not present"
    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        return "Docker is not available"
    return None


def _setup(url, *cmd):
    env = {**os.environ, "DATABASE_URL": url, "LOGS_DATABASE_URL": url}
    r = subprocess.run([sys.executable, str(DB_DIR / "setup.py"), *cmd], env=env, capture_output=True, text=True,
                       timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.fixture(scope="module")
def db_url():
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)
    url = os.environ.get("INTEGRATION_DATABASE_URL")
    if not url:
        r = subprocess.run(["bash", str(DB_DIR / "local_pgvector.sh")], capture_output=True, text=True, timeout=180)
        assert r.returncode == 0, r.stdout + r.stderr
        url = LOCAL_URL
    _setup(url, "migrate")
    _setup(url, "seed", "--force-reseed")   # the 58 seeded flags
    yield url
    _setup(url, "seed", "--force-reseed")   # the reset-demo test deletes flags and events


@pytest.fixture
async def repo(db_url):
    from app.db.pool import Database
    from app.db.queries import Repo
    db = Database(db_url, pool_max=2)
    await db.open()
    yield Repo(db)
    await db.close()


@pytest.mark.integration
async def test_db_blank_ack_note_is_stored_as_null(repo):
    fid = (await repo.db.fetchone("SELECT min(flag_id) AS f FROM flags"))["f"]
    blank = await repo.insert_ack(fid, "NOTE", " \n\t ", "test")
    empty = await repo.insert_ack(fid, "NOTE", "", "test")
    real = await repo.insert_ack(fid, "NOTE", "  valve checked ", "test")
    try:
        assert blank["note"] is None and empty["note"] is None and real["note"] == "valve checked"
    finally:
        await repo.db.execute("DELETE FROM acknowledgements WHERE ack_id = ANY(%s)",
                              ([blank["ack_id"], empty["ack_id"], real["ack_id"]],))


@pytest.mark.integration
async def test_db_note_document_without_a_vector_is_found_by_full_text(repo):
    """Embedder down at ack time: the note document is stored with a NULL vector, and full-text search finds it."""
    doc = {"doc_id": "note:t-fulltext", "kind": "operator_note", "source_id": "t", "asset_id": "HX-208",
           "ts": dt.datetime.now(dt.timezone.utc), "title": "Operator note",
           "content": "Operator tj resolved the flag. Note: zebracorn valve recalibrated", "metadata": {"flag_id": 1}}
    await repo.upsert_document(doc, None, None)
    try:
        row = await repo.db.fetchone("SELECT embedding IS NULL AS no_vec, embed_model, ts FROM documents "
                                     "WHERE doc_id = %s", (doc["doc_id"],))
        assert row["no_vec"] and row["embed_model"] is None and row["ts"] is not None
        hits = await repo.search_fulltext("zebracorn valve", ["operator_note"], "HX-208", None, None, 5)
        assert [h["doc_id"] for h in hits] == [doc["doc_id"]]
    finally:
        await repo.db.execute("DELETE FROM documents WHERE doc_id = %s", (doc["doc_id"],))


@pytest.mark.integration
async def test_db_asset_history_is_bounded_to_robot_time(repo):
    """Every seeded action flag: history strictly before its action, never its own action, no blank notes."""
    flags = await repo.db.fetch("SELECT f.flag_id, f.asset_id, f.action_id, a.timestamp AS ts FROM flags f "
                                "JOIN actions a ON a.action_id = f.action_id WHERE f.asset_id IS NOT NULL")
    assert flags
    ts_of = {r["flag_id"]: r["ts"] for r in flags}
    seen_prior = 0
    for f in flags:
        hist = await repo.asset_history(f["asset_id"], f["flag_id"])
        for n in hist["anomalies"]:
            assert n["action_id"] != f["action_id"] and dt.datetime.fromisoformat(n["timestamp"]) < f["ts"], (f, n)
        for p in hist["prior_flags"]:
            assert p["flag_id"] != f["flag_id"] and ts_of[p["flag_id"]] < f["ts"], (f, p)
            seen_prior += 1
    assert seen_prior, "the seed has assets with several flags"
    # notes: blank ones are skipped, real ones aggregated
    later = max((f for f in flags if any(g["asset_id"] == f["asset_id"] and g["ts"] < f["ts"] for g in flags)),
                key=lambda f: f["ts"])
    prior = max((g for g in flags if g["asset_id"] == later["asset_id"] and g["ts"] < later["ts"]),
                key=lambda g: g["ts"])
    ids = [r["ack_id"] for r in await repo.db.fetch(
        "INSERT INTO acknowledgements (flag_id, action, note, operator) VALUES (%s, 'NOTE', '', 't'), "
        "(%s, 'NOTE', '  ', 't'), (%s, 'NOTE', E'\\n\\t ', 't'), (%s, 'NOTE', E' valve checked\\n', 't') "
        "RETURNING ack_id", (prior["flag_id"],) * 4)]
    try:
        hist = await repo.asset_history(later["asset_id"], later["flag_id"])
        assert {p["flag_id"]: p["notes"] for p in hist["prior_flags"]}[prior["flag_id"]] == "valve checked"
    finally:
        await repo.db.execute("DELETE FROM acknowledgements WHERE ack_id = ANY(%s)", (ids,))
    unbounded = await repo.asset_history(later["asset_id"], None)       # no flag: plain recent history
    assert len(unbounded["anomalies"]) >= len(hist["anomalies"])


@pytest.mark.integration
async def test_db_sql_packet_for_every_seeded_flag_passes_system2(repo):
    svc = FlagService(repo, mcp=None, system2=None, embed=None, sink=None)
    ids = [r["flag_id"] for r in await repo.db.fetch("SELECT flag_id FROM flags ORDER BY flag_id")]
    categories = set()
    for fid in ids:
        row = await repo.get_flag(fid)
        packet = await svc.minimal_packet(row, budget_s=30)
        p = _prepared(fid, row["flag"], packet)          # raises ValueError (System 2's 422) on a bad shape
        assert not p.insufficient_context and len(p.allowed_actions) > 1, (fid, packet.get("gaps"))
        assert isinstance(packet.get("asset_history", []), list)
        categories.add(packet["flag"].get("category"))
    assert "mission" in categories and len(categories) >= 3, categories


@pytest.mark.integration
async def test_db_assessment_provenance_columns_are_guarded(repo):
    fid = (await repo.db.fetchone("SELECT min(flag_id) AS f FROM flags"))["f"]
    a = {"question": "q", "answer": "a", "reasoning": "r", "model": "template", "source": "system2_template",
         "fallback_reason": "System 2 answered with its checked template", "severity_assessment": "HIGH"}
    try:
        row = await repo.upsert_assessment(fid, a, state="TEMPLATE")
        assert row["source"] == "system2_template" and row["fallback_reason"].startswith("System 2 answered")
        assert row["latency_ms"] is None
        await repo.db.execute("ALTER TABLE assessments DROP COLUMN source, DROP COLUMN fallback_reason")
        repo.db.invalidate_columns()                   # a database before migration 014
        row = await repo.upsert_assessment(fid, {**a, "answer": "b"}, state="TEMPLATE")
        assert row["answer"] == "b" and "source" not in row
    finally:
        await repo.db.execute("ALTER TABLE assessments ADD COLUMN IF NOT EXISTS fallback_reason text, "
                              "ADD COLUMN IF NOT EXISTS source text")
        repo.db.invalidate_columns()
        await repo.db.execute("DELETE FROM assessments WHERE flag_id = %s", (fid,))


@pytest.mark.integration
async def test_db_recent_model_calls_names_its_columns(repo):
    await repo.db.execute("INSERT INTO app.model_calls (system, ok, error) VALUES ('embed', false, 'test row')")
    try:
        rows = await repo.recent_model_calls(1)
        assert set(rows[0]) == {"call_id", "ts", "request_id", "system", "model", "endpoint", "flag_id", "action_id",
                                "input_tokens", "output_tokens", "latency_ms", "tokens_per_sec", "device", "ok",
                                "error", "metadata"}
    finally:
        await repo.db.execute("DELETE FROM app.model_calls WHERE error = 'test row'")


@pytest.mark.integration
async def test_db_reset_demo_removes_flag_graph_nodes_but_keeps_live_ones(repo):
    """Runs last: reset-demo deletes every flag and event (the module teardown reseeds)."""
    nodes = [("flag:t-1", "flag", "{}"), ("operator_note:t-2", "operator_note", "{}"),
             ("explanation:t-3", "explanation", "{}"), ("flag:t-live", "flag", '{"source": "live"}'),
             ("asset:t-asset", "asset", "{}")]
    before = (await repo.db.fetchone(
        "SELECT count(*)::int AS n FROM kg_nodes WHERE kind IN ('flag', 'operator_note', 'explanation') "
        "AND coalesce(props->>'source', '') <> 'live'"))["n"]
    for node_id, kind, props in nodes:
        await repo.db.execute("INSERT INTO kg_nodes (node_id, kind, key, label, summary, props) "
                              "VALUES (%s, %s, %s, %s, 'test', %s::jsonb)", (node_id, kind, node_id, node_id, props))
    await repo.db.execute("INSERT INTO kg_edges (src, rel, dst) VALUES ('flag:t-1', 'about', 'asset:t-asset'), "
                          "('flag:t-live', 'about', 'asset:t-asset')")
    try:
        counts = await repo.reset_demo(clear_predictions=False)
        assert counts["kg_nodes"] == before + 3 and counts["flags"] > 0
        left = {r["node_id"] for r in await repo.db.fetch("SELECT node_id FROM kg_nodes WHERE node_id LIKE '%%:t-%%'")}
        assert left == {"flag:t-live", "asset:t-asset"}
        edges = await repo.db.fetch("SELECT src FROM kg_edges WHERE dst = 'asset:t-asset'")
        assert [e["src"] for e in edges] == ["flag:t-live"]              # the deleted node's edge cascaded
    finally:
        await repo.db.execute("DELETE FROM kg_nodes WHERE node_id = ANY(%s)", ([n[0] for n in nodes],))
