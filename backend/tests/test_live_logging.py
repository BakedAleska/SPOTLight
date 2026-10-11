"""Live runner calls in app.model_calls (audit RW-07) and the live-row invariants other jobs rely on.

The runner has no database credentials, so app/services/live.py logs the System 1, System 2 and embedding calls the
runner made. Most tests run against the in-memory fakes. The two `integration` tests write to a real Postgres and
need INTEGRATION_DATABASE_URL to point at a local database (db/local_pgvector.sh or any pgvector container;
`db/setup.py migrate` applied). They delete the rows they write.
"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import pytest

from app.db.live import LiveRepo
from app.db.pool import Database
from app.services import live as L
from app.services import validator as V
from app.services.calllog import LogSink

from .conftest import REPO_ROOT
from .test_live import laya, lh  # noqa: F401  (lh is a fixture)

RAW_LAYA_ERROR = "Laya: 503 ConnectError: All connection attempts failed (upstream 10.9.8.7:8082)"


def qwen_answer(res, path="llm", model="qwen3.8-27b-q8"):
    return {"answer": "Gauge read failed on a critical asset.", "question": "q", "recommended_action": "MONITOR",
            "confidence": "medium", "model": model, "path": path, "latency_ms": 900, "tokens_per_sec": 21.5,
            "evidence": [{"claim": "x", "ids": ["F1"]}], "flag_id": res["system2_request"]["flag_id"],
            "insufficient_context": False}


async def run_route(h, route="R2", laya_for=None, analysis=None, embed=False):
    """Walk a route like scripts/live_runner.py; `analysis(res)` returns the System 2 part of the analysis body."""
    run = (await h.client.post("/api/v1/live/runs", json={"route_id": route})).json()["run"]
    claim = (await h.client.post("/api/v1/live/claim", json={"runner": "test"})).json()
    out = []
    for i, stop in enumerate(claim["route"]["stops"]):
        ev = (await h.client.post("/api/v1/live/runs/%d/stops/%d/start" % (run["run_id"], i))).json()["action_event"]
        lv = laya_for(stop) if laya_for else laya(stop["planned_verdict"] == "red")
        res = await h.client.post("/api/v1/live/runs/%d/stops/%d/result" % (run["run_id"], i),
                                  json={"timestamp": ev["timestamp"], "laya": lv})
        assert res.status_code == 200, res.text
        res = res.json()
        body = {"system2_request": {**res["system2_request"], "request_id": "live-s2-%s" % stop["stop"]},
                "photo": {"sha256": "ab" * 32, "bytes": 10}, "vision": {"pairing": "demo", "rows": []},
                "system2_ms": 1000, "image_status": "sent",
                "timings": {"vision_ms": 250, "system2_ms": 1000, "analysis_ms": 1300}}
        body.update(analysis(res) if analysis else {"system2": qwen_answer(res)})
        rid = "r-test-%s-%s" % (run["run_id"], stop["stop"])
        an = await h.client.post("/api/v1/live/runs/%d/stops/%d/analysis" % (run["run_id"], i), json=body,
                                 headers={"X-Request-ID": rid})
        assert an.status_code == 200, an.text
        an = an.json()
        if embed:
            items = [{"key": x["key"], "text": x["text"], "vector": [0.1, 0.2, 0.3, 0.4]}
                     for x in res["embed"] + an["embed"]]
            r = await h.client.post("/api/v1/live/runs/%d/embeddings" % run["run_id"],
                                    json={"model": "Qwen/Qwen3-Embedding-0.6B", "items": items})
            assert r.status_code == 200, r.text
        out.append((stop, res, an, rid))
    await h.client.post("/api/v1/live/runs/%d/finish" % run["run_id"], json={"status": "done"})
    return run, out


def calls(h, system):
    return [c for c in h.sink.model_calls if c["system"] == system and (c.get("metadata") or {}).get("via")
            == "live_runner"]


# ── System 2 ─────────────────────────────────────────────────────────────────────────────────────
async def test_qwen_call_is_one_system2_row_per_stop(lh):
    run, stops = await run_route(lh)
    rows = calls(lh, "system2")
    assert len(rows) == len(stops) == 8
    for (stop, res, an, rid), row in zip(stops, rows):
        assert row["ok"] is True and row["endpoint"] == "/v1/assess" and row["model"] == "qwen3.8-27b-q8"
        assert row["latency_ms"] == 1000 and row["tokens_per_sec"] == 21.5 and row.get("error") is None
        assert row["request_id"] == rid                       # ties to app.request_log of the analysis POST
        assert row["action_id"] == "LIVE-%d-%s" % (run["run_id"], stop["stop"])
        assert row["flag_id"] == res["flag_id"]               # real flag on red stops, NULL on green ones
        assert row["metadata"] == {"via": "live_runner", "run_id": run["run_id"], "stop": stop["stop"],
                                   "path": "llm", "service_latency_ms": 900, "vision_ms": 250,
                                   "image_status": "sent", "system2_request_id": "live-s2-%s" % stop["stop"]}
    assert sum(1 for r in rows if r["flag_id"]) == 2         # R2 has two red stops; green stops never get a fake id


async def test_qwen_failure_and_template_answer_are_logged(lh):
    def analysis(res):
        if res["capture"]["stop"] == "A":
            return {"system2_error": "System 2: 504 upstream timed out"}
        if res["capture"]["stop"] == "B":
            return {"system2": qwen_answer(res, path="fallback", model="template")}
        return {}                                             # no System 2 call at all: nothing to log

    _, stops = await run_route(lh, analysis=analysis)
    rows = {r["metadata"]["stop"]: r for r in calls(lh, "system2")}
    assert set(rows) == {"A", "B"}
    a, b = rows["A"], rows["B"]
    assert a["ok"] is False and a["model"] is None and a["latency_ms"] == 1000
    assert a["error"] == "System 2: 504 upstream timed out" and "path" not in a["metadata"]
    assert b["ok"] is True and b["model"] == "template" and b["metadata"]["path"] == "fallback"
    assert all(an["qwen_source"] == "template" for _, _, an, _ in stops)


async def test_bad_numbers_from_the_runner_do_not_break_the_stop(lh):
    def analysis(res):
        s2 = qwen_answer(res)
        s2.update(tokens_per_sec="fast", latency_ms=-5, usage={"prompt_tokens": 812, "completion_tokens": 96})
        return {"system2": s2}

    _, stops = await run_route(lh, analysis=analysis)
    row = calls(lh, "system2")[0]
    assert row["tokens_per_sec"] is None and "service_latency_ms" not in row["metadata"]
    assert (row["input_tokens"], row["output_tokens"]) == (812, 96)


def test_runner_numbers_outside_the_column_range_become_null():
    # a value Postgres rejects would fail LogSink's fire-and-forget insert and silently drop the whole row
    assert L._int(2 ** 31) is None and L._int(2 ** 31 - 1) == 2 ** 31 - 1 and L._int(12.9) == 12
    assert L._int(True) is None and L._int(float("nan")) is None and L._int("7") is None
    assert L._num(1e39) is None and L._num(float("inf")) is None and L._num(21.5) == 21.5


# ── System 1 ─────────────────────────────────────────────────────────────────────────────────────
async def test_laya_failure_is_logged_and_its_raw_error_stays_out_of_reasons(lh):
    def laya_for(stop):
        return {"status": "unavailable", "error": RAW_LAYA_ERROR}

    _, stops = await run_route(lh, laya_for=laya_for, analysis=lambda res: {"system2_error": "System 2: 503"})
    s1 = calls(lh, "system1")
    assert len(s1) == 8 and all(r["ok"] is False and r["error"] == RAW_LAYA_ERROR for r in s1)
    for stop, res, an, _ in stops:
        req = res["system2_request"]
        verdicts = [r for r in req["context"]["flag"]["reasons"] if r["rule"] == "laya_unavailable"]
        assert verdicts and all(r["detail"] == L.LAYA_UNAVAILABLE_DETAIL for r in verdicts)
        assert L.LAYA_UNAVAILABLE_DETAIL in req["reasons"]
        assert "ConnectError" not in json.dumps(req)          # packet, facts and reasons System 2 reads
        assert "ConnectError" not in json.dumps(an["capture"]["qwen"])   # the stored template's reasoning
        assert res["capture"]["laya"]["error"] == RAW_LAYA_ERROR         # the diagnostic stays on the capture
        fired = [f["fact"] for f in req["context"]["facts"] if f["fact"].startswith("Rule ")]
        assert not [f for f in fired if any(v in f for v in L.VERDICT_RULES)]
    for a in lh.repo.assessments.values():
        assert "ConnectError" not in json.dumps(a, default=str)


def test_stable_detail_matches_the_validator():
    reasons = V.build_reasons("parallel", None, "ConnectError: boom", {"escalate": True, "priority": "P2",
                              "category": "equipment", "policy_version": "t", "rules": []},
                              {"agreement": "laya_unavailable", "summary": "s"}, None)
    live = L._reasons({"reasons": reasons})
    assert live[0] == {**reasons[0], "detail": L.LAYA_UNAVAILABLE_DETAIL} and live[1:] == reasons[1:]
    # once the validator stores the stable text itself, both must say the same thing
    assert getattr(V, "LAYA_UNAVAILABLE_DETAIL", L.LAYA_UNAVAILABLE_DETAIL) == L.LAYA_UNAVAILABLE_DETAIL


def test_reasons_rewrite_a_raw_laya_detail():
    # with the validator's stable text the rewrite above is a no-op, so check it on a raw detail directly
    raw = [{"rule": "laya_unavailable", "detail": "System 1 (Laya) unavailable: " + RAW_LAYA_ERROR},
           "not a reason", {"rule": "battery_low", "detail": "battery 12%"}]
    out = L._reasons({"reasons": raw})
    assert out == [{"rule": "laya_unavailable", "detail": L.LAYA_UNAVAILABLE_DETAIL},
                   {"rule": "battery_low", "detail": "battery 12%"}]
    assert raw[0]["detail"].endswith(RAW_LAYA_ERROR)          # the ingest's own list is not mutated


async def test_an_answered_laya_call_is_left_to_the_system1_service_log(lh):
    _, stops = await run_route(lh, laya_for=lambda stop: laya(stop["planned_verdict"] == "red"))
    assert calls(lh, "system1") == []


async def test_no_laya_result_is_not_a_logged_call(lh):
    _, stops = await run_route(lh, laya_for=lambda stop: None)
    assert calls(lh, "system1") == []
    assert all(s[1]["capture"]["laya"]["status"] == "laya_unavailable" for s in stops)


# ── embeddings ───────────────────────────────────────────────────────────────────────────────────
async def test_each_embedding_batch_is_one_embed_row(lh):
    run, stops = await run_route(lh, embed=True)
    rows = calls(lh, "embed")
    assert len(rows) == 8                                     # one /embeddings POST per stop in run_route
    for row in rows:
        m = row["metadata"]
        assert row["ok"] is True and row["endpoint"] == "/v1/embeddings"
        assert row["model"] == "Qwen/Qwen3-Embedding-0.6B" and row["latency_ms"] is None
        assert m["run_id"] == run["run_id"] and m["purpose"] == "document"
        assert m["inputs"] == m["embedded"] + m["skipped"] and m["embedded"] >= 1 and m["skipped"] == 0

    r = await lh.client.post("/api/v1/live/runs/%d/embeddings" % run["run_id"],
                             json={"items": [{"key": "doc:nope", "text": "x", "vector": [0.1, 0.2]}]})
    assert r.status_code == 200 and len(r.json()["skipped"]) == 1
    bad = calls(lh, "embed")[-1]
    assert bad["ok"] is False and bad["error"] == "vector has 2 dims, EMBED_DIM is 4"
    assert bad["model"] == lh.svc.live.embed_model
    before = len(calls(lh, "embed"))
    await lh.client.post("/api/v1/live/runs/%d/embeddings" % run["run_id"], json={"items": []})
    assert len(calls(lh, "embed")) == before                  # an empty batch made no call


async def test_embed_timing_is_used_when_the_runner_sends_it(lh):
    run, _ = await run_route(lh)
    lh.sink.model_calls.clear()
    await lh.svc.live.embeddings(run["run_id"], {"model": "m", "latency_ms": 200, "input_tokens": 50,
                                                 "device": "AMD Radeon Pro W7900D", "items": [
                                                     {"key": "doc:nope", "text": "x", "vector": [0.1] * 4}]})
    row = calls(lh, "embed")[0]
    assert (row["latency_ms"], row["input_tokens"], row["tokens_per_sec"]) == (200, 50, 250.0)
    assert row["device"] == "AMD Radeon Pro W7900D"


# ── props.source = 'live' (db/graph_build.py and amd/embed/ingest.py keep live rows on it) ────────
async def test_every_live_node_edge_and_document_is_tagged_live(lh):
    await run_route(lh)
    assert lh.live.nodes and lh.live.edges
    assert all(n["props"]["source"] == "live" for n in lh.live.nodes.values())
    assert all(e["props"]["source"] == "live" for e in lh.live.edges.values())
    docs = [d for d in lh.repo.documents.values() if "LIVE-" in (d.get("action_id") or "")]
    assert {d["kind"] for d in docs} == {"event", "explanation"}
    assert all(d["metadata"]["source"] == "live" for d in docs)


class _Cursor:
    def __init__(self):
        self.calls, self.rowcount = [], 1

    async def execute(self, sql, params):
        self.calls.append((sql, params))


class _Db:
    def __init__(self):
        self.cur = _Cursor()

    @asynccontextmanager
    async def transaction(self):
        yield self.cur


async def test_kg_upsert_tags_rows_live_even_if_the_caller_forgot():
    db = _Db()
    repo = LiveRepo(db)
    node = {"node_id": "mission:LIVE-x", "kind": "mission", "key": "LIVE-x", "label": "l", "summary": "s",
            "props": {"run_id": "1", "source": "kit"}}
    await repo.kg_upsert([node, {**node, "node_id": "action:LIVE-x-A", "props": None}],
                         [{"src": "action:LIVE-x-A", "rel": "part_of", "dst": "mission:LIVE-x"}])
    (_, n1), (_, n2), (_, e1) = db.cur.calls
    assert n1[5].obj == {"run_id": "1", "source": "live"} and n2[5].obj == {"source": "live"}
    assert e1[4].obj == {"source": "live"}
    assert node["props"]["source"] == "kit"                    # the caller's dict is not mutated


# ── real Postgres (local only) ───────────────────────────────────────────────────────────────────
def _local_db_url():
    url = os.environ.get("INTEGRATION_DATABASE_URL")
    if os.environ.get("SKIP_INTEGRATION") == "1" or not url:
        pytest.skip("INTEGRATION_DATABASE_URL not set (a local pgvector DB)")
    if urlparse(url).hostname not in ("localhost", "127.0.0.1", "::1", "db", "postgres"):
        pytest.skip("these tests write rows; INTEGRATION_DATABASE_URL must be a local database")
    return url


async def _open(url):
    db = Database(url, name="test", pool_min=0, pool_max=2)
    await db.open()
    return db


@pytest.mark.integration
async def test_live_runner_rows_insert_into_app_model_calls(lh):
    """The rows pass the real CHECK and column types (system1/system2/embed, int latency, jsonb metadata)."""
    url = _local_db_url()
    db = await _open(url)
    try:
        if not (await db.fetchone("SELECT to_regclass('app.model_calls') AS t"))["t"]:
            for f in ("005_raw_and_app_logs.sql", "012_chat_logs.sql"):
                await db.execute(Path(REPO_ROOT, "db", "migrations", f).read_text())
        start = (await db.fetchone("SELECT coalesce(max(call_id), 0) AS m FROM app.model_calls"))["m"]
        sink = LogSink(db)
        lh.svc.live.sink = sink

        def laya_for(stop):
            return laya(stop["planned_verdict"] == "red") if stop["stop"] < "E" else \
                {"status": "unavailable", "error": RAW_LAYA_ERROR}

        def analysis(res):
            return {"system2": qwen_answer(res)} if res["capture"]["stop"] != "H" else {"system2_error": "timeout"}

        run, stops = await run_route(lh, laya_for=laya_for, analysis=analysis, embed=True)
        await sink.drain()
        rows = await db.fetch("SELECT * FROM app.model_calls WHERE call_id > %s AND metadata->>'via' = 'live_runner' "
                              "ORDER BY call_id", (start,))
        try:
            by = {s: [r for r in rows if r["system"] == s] for s in ("system1", "system2", "embed")}
            # system1: only the 4 failed calls; the System 1 service logs the calls it answers itself
            assert [len(by[s]) for s in ("system1", "system2", "embed")] == [4, 8, 8]
            assert [r["ok"] for r in by["system1"]] == [False] * 4
            assert [r["ok"] for r in by["system2"]] == [True] * 7 + [False]
            s2 = by["system2"][0]
            assert s2["latency_ms"] == 1000 and abs(s2["tokens_per_sec"] - 21.5) < 1e-6
            assert s2["metadata"]["vision_ms"] == 250 and s2["metadata"]["caller"] == "backend"
            assert s2["request_id"] == stops[0][3] and s2["action_id"] == "LIVE-%d-A" % run["run_id"]
            assert by["system2"][-1]["error"] == "timeout" and by["system2"][-1]["model"] is None
            assert all(r["metadata"]["run_id"] == run["run_id"] for r in rows)
        finally:
            await db.execute("DELETE FROM app.model_calls WHERE call_id > %s AND metadata->>'via' = 'live_runner' "
                             "AND metadata->>'run_id' = %s", (start, str(run["run_id"])))
    finally:
        await db.close()


@pytest.mark.integration
async def test_kg_upsert_writes_source_live_on_real_tables():
    url = _local_db_url()
    db = await _open(url)
    tag = uuid.uuid4().hex[:8]
    a, m = "action:LIVE-T%s-A" % tag, "mission:LIVE-T%s" % tag
    try:
        if not (await db.fetchone("SELECT to_regclass('kg_nodes') AS t"))["t"]:
            pytest.skip("kg_nodes missing: run db/setup.py migrate on INTEGRATION_DATABASE_URL")
        node = {"kind": "mission", "key": tag, "label": "l", "summary": "s", "props": {"run_id": tag}}
        out = await LiveRepo(db).kg_upsert([{**node, "node_id": m}, {**node, "node_id": a, "kind": "action"}],
                                           [{"src": a, "rel": "part_of", "dst": m}])
        assert out == {"nodes": 2, "edges": 1}
        nodes = await db.fetch("SELECT props->>'source' AS s FROM kg_nodes WHERE node_id IN (%s, %s)", (a, m))
        edges = await db.fetch("SELECT props->>'source' AS s FROM kg_edges WHERE src = %s", (a,))
        assert [r["s"] for r in nodes] == ["live", "live"] and [r["s"] for r in edges] == ["live"]
    finally:
        await db.execute("DELETE FROM kg_nodes WHERE node_id IN (%s, %s)", (a, m))
        await db.close()
