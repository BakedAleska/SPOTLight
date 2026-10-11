"""Data-quality fixes in what the MCP server returns (DB audit 2026-10-11, package PKG-E).

- Verdict rows (laya_verdict / rules_verdict / laya_unavailable) are not policy rules: never a policy lookup, a policy
  query term or a graph seed, and they never push a fired rule out of the flag view.
- A search that found nothing relevant (or an empty graph) is a packet `note`, not a `gap`: System 2 treats any gap
  as blocking. Real failures stay in `gaps`.
- Self-exclusion uses the flag's triage event doc (event:act-A0001 / event:mis-M001).
- get_raw_event_log returns each event once (the seed's replay.ndjson copy wins); the asset profile only looks back in robot time; the mission view's
  worst anomaly carries asset_name / asset_criticality; embed log rows carry device, tokens/s, flag_id and a
  path-only endpoint; serverless requests drain their log inserts.

Unit tests use the in-memory fakes. The DB tests run the real SQL on a throwaway database spot_mcptest_<hex> on a
local Postgres with pgvector (MCP_TEST_ADMIN_URL, default the compose `db` on DB_PORT 5433), built from
db/migrations 001-005 and 009; they skip when no server answers.

    cd amd/mcp && python -m pytest -q tests/test_data_quality.py
    MCP_TEST_ADMIN_URL=postgresql://postgres:pw@127.0.0.1:55495/postgres python -m pytest -q tests/test_data_quality.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

from spot_mcp.db import NullLogSink, PgDatabase, PgLogSink
from spot_mcp.repo import PgRepo
from spot_mcp.service import KG_EMPTY, NO_GRAPH_FACTS, VERDICT_RULES, ContextService, fired_rules

from fakes import FLAGS, FakeEmbedder, FakeRepo, FakeSink, bow, cos, settings

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "db" / "migrations"
ASGI_PY = Path(__file__).resolve().parent.parent / "asgi.py"

VERDICTS = [{"rule": "laya_unavailable", "detail": "System 1 (Laya) unavailable: circuit open after: ConnectError: "
                                                   "All connection attempts failed; rules-only verdict"},
            {"rule": "rules_verdict", "detail": "Laya: unavailable · rules: escalate"}]
FIRED = ["gauge_out_of_range", "battery_low", "anomaly_high", "anomaly_present", "reading_untrusted",
         "action_failed", "battery_watch", "asset_critical", "mission_misleading", "waypoint_unreliable"]
REASONS = VERDICTS + [{"rule": r, "detail": "%s fired" % r} for r in FIRED]


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ── fakes ───────────────────────────────────────────────────────────────────────────────────────
class DQRepo(FakeRepo):
    """FakeRepo whose flag 1 has the live shape (verdict rows first, 10 fired rules, triage_event_id) and which
    records the exact policy docs it was asked for. `kg_nodes`: None = no emptiness check (older repos), else the
    check's answer."""

    def __init__(self, kg_nodes=None, broken_hybrid=False, **kw):
        super().__init__(**kw)
        self.policy_ids, self.kg_checks, self.broken_hybrid = [], 0, broken_hybrid
        if kg_nodes is not None:
            async def kg_has_nodes():
                self.kg_checks += 1
                return kg_nodes
            self.kg_has_nodes = kg_has_nodes

    async def flag(self, flag_id):
        row = FLAGS.get(flag_id)
        if row is None:
            return None
        row = json.loads(json.dumps(row))
        if flag_id == 1:
            row["flag"].update(reasons=REASONS, triage_event_id="act-A0136")
        return row

    async def documents_by_id(self, doc_ids):
        self.policy_ids.append(list(doc_ids))
        return await super().documents_by_id(doc_ids)

    async def match_hybrid(self, vec, text, kinds, asset, location, mission, k):
        if self.broken_hybrid and kinds == ["incident"]:
            raise RuntimeError("statement timeout")
        return await super().match_hybrid(vec, text, kinds, asset, location, mission, k)


def make(repo=None, embedder=None, sink=None, **kw):
    return ContextService(repo or DQRepo(), embedder or FakeEmbedder(), settings(**kw), sink or FakeSink())


def graph_seeds(repo):
    return [set(c[1]) for c in repo.graph_calls if c[0] == "graph_neighborhood" and len(c) > 1]


# ── verdict rules ───────────────────────────────────────────────────────────────────────────────
def test_fired_rules_skips_verdicts_and_parses_json_text():
    assert fired_rules({"reasons": REASONS})[:4] == FIRED[:4]
    assert fired_rules({"reasons": json.dumps(REASONS)}) == FIRED
    assert fired_rules({"reasons": None}) == [] and fired_rules({"reasons": "not json"}) == []


async def test_verdict_rules_never_reach_policy_lookups_queries_or_graph_seeds():
    repo, emb = DQRepo(), FakeEmbedder()
    p = await make(repo, emb).get_flag_context(1)
    asked = repo.policy_ids[0]
    assert asked == ["policy:%s" % r for r in FIRED[:4]]                      # rules[:4] are real policy rules
    assert not any(v in i for i in asked for v in VERDICT_RULES)
    policy_query = emb.calls[0][2]                                            # incident, manual, policy, history
    assert policy_query.startswith("triage rule: gauge_out_of_range")
    assert not any(v in policy_query for v in VERDICT_RULES)
    seeds = graph_seeds(repo)
    assert seeds and all("policy_rule:%s" % v not in s for s in seeds for v in VERDICT_RULES)
    assert "policy_rule:gauge_out_of_range" in seeds[0]
    # the flag view: fired rules first (up to 8), verdicts in their own compact key
    flag = p["flag"]
    assert [r["rule"] for r in flag["reasons"]] == FIRED[:8]
    assert [v["rule"] for v in flag["verdicts"]] == ["laya_unavailable", "rules_verdict"]
    assert all(len(v["detail"]) <= 160 for v in flag["verdicts"])
    # an old laya_unavailable row's raw connection error is never passed on
    assert flag["verdicts"][0]["detail"] == "System 1 (Laya) unavailable; rules-only verdict"
    assert "ConnectError" not in json.dumps(p)
    assert p["policy"][0]["id"] == "policy:gauge_out_of_range"


async def test_get_flag_view_matches_packet_view():
    out = await make().get_flag(1)
    assert [r["rule"] for r in out["reasons"]] == FIRED[:8] and len(out["verdicts"]) == 2


# ── notes vs gaps ───────────────────────────────────────────────────────────────────────────────
async def test_retrieval_misses_and_empty_graph_are_notes_not_gaps():
    repo = DQRepo(kg_nodes=False)
    p = await make(repo, min_similarity=0.99).get_flag_context(1)
    assert "gaps" not in p and p["insufficient_context"] is False
    notes = p["notes"]
    assert "no relevant similar incidents above similarity 0.99" in notes
    assert "no relevant manual above similarity 0.99" in notes
    assert "%s: %s" % (NO_GRAPH_FACTS, KG_EMPTY) in notes and "graph" not in p
    assert list(p)[-1] == "question"
    # the graph check is one cached EXISTS query, and no traversal runs on an empty graph
    assert repo.kg_checks == 1 and not graph_seeds(repo)


async def test_no_linked_graph_facts_is_a_note():
    class NoFacts(DQRepo):
        async def graph_neighborhood(self, seeds, hops, rels, kinds, max_nodes):
            self.graph_calls.append(("graph_neighborhood", tuple(seeds), hops, (), max_nodes))
            return []

        async def kg_details(self, node_ids):
            return {"nodes": [], "edges": []}
    p = await make(NoFacts(kg_nodes=True)).get_flag_context(1)
    assert NO_GRAPH_FACTS in p["notes"] and NO_GRAPH_FACTS not in p.get("gaps", [])


async def test_unknown_category_is_a_note():
    p = await make().get_flag_context(2)                     # action A9999 is missing: no validator, no category
    assert "category unknown; all actions allowed" in p["notes"]
    assert "category unknown; all actions allowed" not in p.get("gaps", [])
    assert p["insufficient_context"] is True                 # the missing action is what makes it insufficient


async def test_real_failures_stay_in_gaps():
    p = await make(DQRepo(broken_hybrid=True)).get_flag_context(1)
    assert any(g.startswith("similar_incidents search failed") for g in p["gaps"])
    assert not any("similar incidents" in n for n in p.get("notes", []))
    p = await make(embedder=FakeEmbedder(fail=True)).get_flag_context(1)
    assert any(g.startswith("retrieval skipped") for g in p["gaps"])
    assert not any(n.startswith("no relevant") for n in p.get("notes", []))
    p = await make(DQRepo(graph="missing")).get_flag_context(1)
    assert any(g.startswith("graph unavailable") for g in p["gaps"])


def _system2_normalize():
    sys.path.insert(0, str(ROOT / "amd" / "system2"))
    try:
        from analyst.backend_contract import BackendAssessRequest
        from analyst.packet import normalize_packet
    except ImportError as exc:  # pragma: no cover - System 2 not importable here
        pytest.skip("System 2 analyst package not importable: %s" % exc)
    finally:
        sys.path.remove(str(ROOT / "amd" / "system2"))
    return lambda ctx: normalize_packet(BackendAssessRequest.model_validate(
        {"flag_id": 1, "flag": ctx["flag"], "context": ctx}))


async def test_system2_accepts_a_packet_with_only_notes():
    """System 2 blocks on any gap (amd/system2/analyst/packet.py); it ignores unknown keys like `notes`."""
    normalize = _system2_normalize()
    p = await make(DQRepo(kg_nodes=False), min_similarity=0.99).get_flag_context(1)
    ctx = json.loads(json.dumps(p, default=str))
    assert ctx["notes"] and normalize(ctx).insufficient_context is False
    blocked = dict(ctx, gaps=ctx["notes"])                   # the old shape: the same misses as gaps
    assert normalize(blocked).insufficient_context is True


# ── self-exclusion ──────────────────────────────────────────────────────────────────────────────
async def test_self_ids_use_the_triage_event_doc():
    s = make()
    ids = s._self_ids(await s._load_flag(1))
    assert "event:act-A0136" in ids and "event:E00300" not in ids and "incident:AN002" in ids
    ids = s._self_ids(await s._load_flag(4))                 # mission flag without triage_event_id: derived
    assert "event:mis-M014" in ids


async def test_own_event_doc_never_comes_back_as_history():
    own = {"doc_id": "event:act-A0136", "kind": "event", "asset_id": "HX-204", "action_id": None,
           "title": "M014 step 6: gauge reading HX-204", "content": "SPOT read the pressure gauge on heat exchanger "
                                                                    "HX-204: 146.8 psi GOOD, gauge out of range"}

    class WithOwnDoc(DQRepo):
        def _scored(self, vec, kinds, asset, location, mission):
            rows = super()._scored(vec, kinds, asset, location, mission)
            if (not kinds or "event" in kinds) and asset in (None, "HX-204"):
                rows.append({**own, "similarity": cos(vec, bow(own["title"] + " " + own["content"]))})
            return sorted(rows, key=lambda r: -r["similarity"])
    p = await make(WithOwnDoc(), min_similarity=0.0).get_flag_context(1)
    assert "event:act-A0136" not in [i["id"] for i in p.get("asset_history", [])]
    assert "event:act-A0136" not in p["citation_ids"]


# ── mission view ────────────────────────────────────────────────────────────────────────────────
async def test_mission_view_worst_anomaly_carries_asset_fields():
    class Named(DQRepo):
        async def mission_bundle(self, mission_id):
            b = await super().mission_bundle(mission_id)
            if b and b.get("worst_anomaly"):
                b["worst_anomaly"].update(asset_name="Heat Exchanger 04", asset_criticality="HIGH")
            return b
    w = (await make(Named()).get_mission_summary("M014"))["worst_anomaly"]
    assert w["asset_name"] == "Heat Exchanger 04" and w["asset_criticality"] == "HIGH" and w["id"] == "anomaly:AN002"


# ── empty graph: the graph tools say so ─────────────────────────────────────────────────────────
async def test_graph_tools_report_an_empty_graph():
    repo, emb = DQRepo(kg_nodes=False), FakeEmbedder()
    s = make(repo, emb)
    for out in (await s.graph_neighborhood("asset:HX-208"), await s.search_graph("localization failure")):
        assert out["graph_empty"] is True and out["facts"] == [] and out["result_count"] == 0
        assert KG_EMPTY in out["message"] and "No graph node matches" not in out["message"]
    assert repo.kg_checks == 1 and emb.calls == []              # cached; nothing embedded for an empty graph
    full = await make(DQRepo(kg_nodes=True)).graph_neighborhood("asset:HX-204")
    assert "graph_empty" not in full and full["facts"]


# ── embedding telemetry (app.model_calls) ───────────────────────────────────────────────────────
def _mock_embedder(sink, dim, timing=True, delay_s=0.0):
    import httpx2
    from spot_mcp.embed import HttpEmbedder

    def handler(req):
        if delay_s:
            time.sleep(delay_s)
        n = len(json.loads(req.content)["input"])
        body = {"data": [{"index": i, "embedding": [0.1] * dim} for i in range(n)],
                "usage": {"prompt_tokens": 40 * n, "total_tokens": 40 * n}}
        if timing:
            body["timing"] = {"ms": 12.5, "tokens_per_sec": 3200.0, "device": "cuda", "dtype": "bfloat16",
                              "gpu": "AMD Radeon Pro W7900D"}
        return httpx2.Response(200, json=body)
    return HttpEmbedder("https://user:secret@fairly-cup.trycloudflare.com:8443/", "Qwen/Qwen3-Embedding-0.6B", dim,
                        sink=sink, transport=httpx2.MockTransport(handler))


async def test_embed_row_has_device_tokens_per_sec_and_path_only_endpoint():
    sink = FakeSink()
    e = _mock_embedder(sink, 4)
    await e.embed_queries(["battery low"], request_id="r1", flag_id=7, action_id="A0007")
    row = sink.models[-1]
    assert row["endpoint"] == "/v1/embeddings" and row["system"] == "embed" and row["ok"] is True
    assert row["device"] == "AMD Radeon Pro W7900D" and row["tokens_per_sec"] == 3200.0
    assert row["flag_id"] == 7 and row["action_id"] == "A0007" and row["input_tokens"] == 40
    meta = row["metadata"]
    assert meta["base_host"] == "fairly-cup.trycloudflare.com:8443" and meta["server_ms"] == 12.5
    assert "secret" not in json.dumps(row) and "user" not in meta["base_host"]
    # no timing block: tokens/s from the measured latency, device unknown
    e2 = _mock_embedder(sink, 4, timing=False, delay_s=0.02)
    await e2.embed_queries(["dock"])
    row = sink.models[-1]
    assert row["device"] is None and row["tokens_per_sec"] and row["tokens_per_sec"] <= 40 / 0.02
    assert row["flag_id"] is None and row["endpoint"] == "/v1/embeddings"
    await e.close()
    await e2.close()


async def test_embed_errors_and_odd_server_fields_never_leak_or_break_the_row():
    import httpx2
    from spot_mcp.embed import EmbeddingError, HttpEmbedder

    sink = FakeSink()

    def down(req):
        raise httpx2.ConnectError("All connection attempts failed", request=req)
    e = HttpEmbedder("https://user:secret@fairly-cup.trycloudflare.com:8443", "m", 4, sink=sink,
                     transport=httpx2.MockTransport(down))
    with pytest.raises(EmbeddingError) as ei:
        await e.embed_queries(["x"], flag_id=3)
    msg = str(ei.value)
    assert "secret" not in msg and "user" not in msg and "fairly-cup.trycloudflare.com:8443/v1/embeddings" in msg
    assert sink.models[-1]["ok"] is False and "secret" not in json.dumps(sink.models[-1])
    await e.close()

    def odd(req):   # a non-dict usage and a non-string gpu must not fail the call or the log insert
        return httpx2.Response(200, json={"data": [{"index": 0, "embedding": [0.1] * 4}], "usage": "n/a",
                                          "timing": {"gpu": {"name": "x"}, "device": " cuda ",
                                                     "tokens_per_sec": "fast"}})
    e = HttpEmbedder("http://localhost:18081", "m", 4, sink=sink, transport=httpx2.MockTransport(odd))
    assert len((await e.embed_queries(["y"]))[0]) == 4
    row = sink.models[-1]
    assert row["ok"] is True and row["device"] == "cuda" and row["tokens_per_sec"] is None
    assert row["input_tokens"] is None and row["metadata"]["base_host"] == "localhost:18081"
    await e.close()


async def test_flag_context_labels_its_embed_call_with_the_flag():
    sink = FakeSink()
    s = make(embedder=_mock_embedder(sink, 64), sink=sink)
    await s.get_flag_context(1)
    assert sink.models and all(m["flag_id"] == 1 and m["action_id"] == "A0136" for m in sink.models)
    n = len(sink.models)
    await s.search_similar_incidents(flag_id=1, text="pressure creeping up on the exchanger")
    assert len(sink.models) == n + 1 and sink.models[-1]["flag_id"] == 1


async def test_embedders_without_call_ids_still_work():
    emb = FakeEmbedder()                                   # embed_queries(texts, request_id=None) only
    p = await make(embedder=emb).get_flag_context(1)
    assert emb.calls and "similar_incidents" in p


# ── serverless log drain ────────────────────────────────────────────────────────────────────────
class SlowSink:
    def __init__(self):
        self._tasks, self.written = set(), []

    def tool_call(self, row):
        async def write():
            await asyncio.sleep(0.05)
            self.written.append(row)
        t = asyncio.get_running_loop().create_task(write())
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def model_call(self, row):
        pass

    async def drain(self, timeout_s=None):
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=timeout_s)

    async def close(self):
        await self.drain()


def load_asgi(monkeypatch, **env):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    monkeypatch.setenv("MCP_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("MCP_LOG_CALLS", "0")
    for k in ("SERVERLESS", "VERCEL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("vercel_asgi_dq_%s" % uuid.uuid4().hex[:6], ASGI_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def test_asgi_drains_logs_only_on_serverless(monkeypatch):
    assert type(load_asgi(monkeypatch).app).__name__ == "BearerAuth"
    mod = load_asgi(monkeypatch, VERCEL="1")
    assert isinstance(mod.app, mod.DrainLogs) and isinstance(mod.app.sink, NullLogSink)
    assert type(load_asgi(monkeypatch, SERVERLESS="1").app).__name__ == "DrainLogs"
    assert type(load_asgi(monkeypatch, SERVERLESS="0", VERCEL="1").app).__name__ == "BearerAuth"


async def test_drain_middleware_waits_for_the_request_log_rows(monkeypatch):
    mod = load_asgi(monkeypatch, SERVERLESS="1")
    sink, sent = SlowSink(), []

    async def app(scope, receive, send):
        sink.tool_call({"tool": "get_flag_context"})          # fire-and-forget, like ContextService.logged
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{", "more_body": True})
        await send({"type": "http.response.body", "body": b"}"})

    async def send(msg):
        sent.append((msg.get("more_body", False), len(sink.written)))
    await mod.DrainLogs(app, sink, 1.0)({"type": "http", "path": "/mcp"}, None, send)
    assert sink.written == [{"tool": "get_flag_context"}] and len(sent) == 3
    # the row lands BEFORE the final chunk (the instance may freeze once the response is complete)
    assert sent[1] == (True, 0) and sent[2] == (False, 1)

    # a request that fails before its final chunk still drains on the way out
    failing = SlowSink()

    async def boom(scope, receive, send):
        failing.tool_call({"tool": "get_flag", "ok": False})
        raise RuntimeError("tool crashed")
    with pytest.raises(RuntimeError):
        await mod.DrainLogs(boom, failing, 1.0)({"type": "http", "path": "/mcp"}, None, send)
    assert failing.written == [{"tool": "get_flag", "ok": False}]
    # lifespan and other scopes pass straight through, with no drain
    seen = []

    async def lifespan(scope, receive, send):
        seen.append(scope["type"])
    await mod.DrainLogs(lifespan, SlowSink())({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]


async def test_serverless_app_writes_the_tool_call_row_within_the_request(monkeypatch):
    """The real SDK app behind asgi.py (SERVERLESS=1): a tools/call returns normally, and its fire-and-forget
    app.mcp_tool_calls row has landed by the time the response is complete."""
    import httpx2
    import spot_mcp.server as server_mod

    sink = SlowSink()
    svc = ContextService(DQRepo(), FakeEmbedder(), settings(), sink)
    monkeypatch.setattr(server_mod, "service_from_env", lambda settings=None: svc)
    app = load_asgi(monkeypatch, SERVERLESS="1").app
    to_app, from_app = asyncio.Queue(), asyncio.Queue()
    life = asyncio.create_task(app({"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}},
                                   to_app.get, from_app.put))
    await to_app.put({"type": "lifespan.startup"})
    assert (await from_app.get())["type"] == "lifespan.startup.complete"
    hdrs = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
            "Authorization": "Bearer test-token"}
    try:
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                      base_url="https://spot-mcp.vercel.app") as c:
            r = await c.post("/mcp", headers=hdrs, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "get_flag", "arguments": {"flag_id": 1}}})
            written = list(sink.written)
    finally:
        await to_app.put({"type": "lifespan.shutdown"})
        await from_app.get()
        await life
    assert r.status_code == 200 and "flag:1" in r.text and not r.json()["result"].get("isError")
    assert [w["tool"] for w in written] == ["get_flag"] and written[0]["ok"] is True


# ── DB tests: the real SQL on a throwaway database ──────────────────────────────────────────────
ADMIN_URL = os.environ.get("MCP_TEST_ADMIN_URL") or "postgresql://postgres:%s@localhost:%s/postgres" % (
    os.environ.get("POSTGRES_PASSWORD", "postgres"), os.environ.get("DB_PORT", "5433"))

FIXTURE_SQL = """
INSERT INTO locations (location_id, location_name, zone_type, environment) VALUES
  ('WP01', 'Cooling Loop', 'PROCESS', 'INDOOR_DRY'), ('WP02', 'Turbine Hall South', 'TURBINE', 'HIGH_NOISE');
INSERT INTO assets VALUES ('HX-1', 'Heat Exchanger 01', 'Heat Exchanger', 'WP01', 'HIGH'),
                          ('PUMP-2', 'Pump 02', 'Pump', 'WP02', 'MEDIUM');
INSERT INTO missions (mission_id, mission_name, configuration_version, start_time, end_time, duration_min,
                      start_battery_pct, end_battery_pct, mission_status) VALUES
  ('M001', 'Route 01', 'V1', '2026-09-01 18:00', '2026-09-01 18:31', 31, 82, 40, 'COMPLETED'),
  ('M002', 'Route 02', 'V1', '2026-09-02 02:00', '2026-09-02 02:31', 31, 80, 30, 'COMPLETED'),
  ('M003', 'Route 03', 'V1', '2026-09-03 02:00', '2026-09-03 02:31', 31, 80, 30, 'COMPLETED');
INSERT INTO actions (action_id, mission_id, sequence, timestamp, location_id, asset_id, action_type, battery_pct,
                     status, failure_type, duration_sec, configuration_version) VALUES
  ('A0001', 'M001', 1, '2026-09-01 18:10', 'WP01', 'HX-1', 'GAUGE_READING', 70, 'SUCCESS', NULL, 60, 'V1'),
  ('A0002', 'M002', 1, '2026-09-02 02:10', 'WP01', 'HX-1', 'GAUGE_READING', 60, 'SUCCESS', NULL, 60, 'V1'),
  ('A0003', 'M003', 1, '2026-09-03 02:10', 'WP01', 'HX-1', 'GAUGE_READING', 50, 'SUCCESS', NULL, 60, 'V1'),
  ('A0004', 'M002', 2, '2026-09-02 02:20', 'WP02', 'PUMP-2', 'NAVIGATION', 55, 'FAILED', 'LOCALIZATION_FAILURE',
   60, 'V1');
INSERT INTO readings (reading_id, action_id, mission_id, asset_id, location_id, timestamp, measurement_type, value,
                      unit, quality) VALUES
  ('R0001', 'A0001', 'M001', 'HX-1', 'WP01', '2026-09-01 18:10', 'PRESSURE_PSI', 120, 'psi', 'GOOD'),
  ('R0002', 'A0002', 'M002', 'HX-1', 'WP01', '2026-09-02 02:10', 'PRESSURE_PSI', 146.8, 'psi', 'GOOD'),
  ('R0003', 'A0003', 'M003', 'HX-1', 'WP01', '2026-09-03 02:10', 'PRESSURE_PSI', 160, 'psi', 'GOOD');
INSERT INTO anomalies (anomaly_id, action_id, mission_id, location_id, asset_id, timestamp, anomaly_type, severity,
                       operator_review_required, description) VALUES
  ('AN001', 'A0001', 'M001', 'WP01', 'HX-1', '2026-09-01 18:10', 'GAUGE_OUT_OF_RANGE', 'MEDIUM', true, 'earlier'),
  ('AN002', 'A0002', 'M002', 'WP01', 'HX-1', '2026-09-02 02:10', 'GAUGE_OUT_OF_RANGE', 'HIGH', true, 'own'),
  ('AN003', 'A0003', 'M003', 'WP01', 'HX-1', '2026-09-03 02:10', 'VISUAL_DEFECT', 'CRITICAL', true, 'later');
INSERT INTO events (event_id, mission_id, ts, event_type, action_id, location_id, asset_id, battery_pct) VALUES
  ('E00001', 'M001', '2026-09-01 18:10', 'ACTION_COMPLETED', 'A0001', 'WP01', 'HX-1', 70),
  ('E00002', 'M002', '2026-09-02 02:10', 'ACTION_COMPLETED', 'A0002', 'WP01', 'HX-1', 60),
  ('E00003', 'M003', '2026-09-03 02:10', 'ACTION_COMPLETED', 'A0003', 'WP01', 'HX-1', 50),
  ('E00004', 'M002', '2026-09-02 02:31', 'MISSION_COMPLETED', NULL, NULL, NULL, 30);
-- created_at runs against robot time (a replay re-creates older flags later), as on Neon
INSERT INTO flags (flag_id, event_id, triage_event_id, mission_id, action_id, location_id, asset_id, score, severity,
                   title, reasons, escalated, priority, category, created_at) VALUES
  (1, 'E00001', 'act-A0001', 'M001', 'A0001', 'WP01', 'HX-1', 50, 'MEDIUM', 'earlier flag on HX-1',
   '[{"rule": "gauge_out_of_range", "detail": "120 psi"}]', true, 'P3', 'equipment', '2026-10-11 01:00+00'),
  (2, 'E00002', 'act-A0002', 'M002', 'A0002', 'WP01', 'HX-1', 80, 'HIGH', 'this flag on HX-1', '{reasons}', true,
   'P1', 'equipment', '2026-10-10 23:00+00'),
  (3, 'E00003', 'act-A0003', 'M003', 'A0003', 'WP01', 'HX-1', 90, 'CRITICAL', 'later flag on HX-1',
   '[{"rule": "anomaly_critical", "detail": "x"}]', true, 'P1', 'equipment', '2026-10-10 22:00+00'),
  (4, 'E00004', 'mis-M002', 'M002', NULL, NULL, NULL, 70, 'HIGH', 'mission M002 hides a failure',
   '[{"rule": "mission_misleading", "detail": "1 of 2 actions failed"}]', true, 'P2', 'mission',
   '2026-10-10 23:30+00');
INSERT INTO acknowledgements (flag_id, action, note, operator) VALUES
  (1, 'ACKNOWLEDGE', '', 'op'), (1, 'NOTE', '   ', 'op'), (1, 'RESOLVE', 'recalibrated the gauge', 'op');
-- the seed import and a replay log the same events (same sha256) under two sources; two rows have no event_id
INSERT INTO raw.spot_event_log (source, event_id, mission_id, event_type, event_ts, payload, sha256, received_at) VALUES
  ('replay.ndjson', 'E00005', 'M002', 'ACTION_STARTED', '2026-09-02 02:05', '{"event_id": "E00005"}', 's5',
   '2026-10-10 21:00+00'),
  ('replay.ndjson', 'E00002', 'M002', 'ACTION_COMPLETED', '2026-09-02 02:10', '{"event_id": "E00002"}', 's2',
   '2026-10-10 21:00+00'),
  ('replay.ndjson', 'E00004', 'M002', 'MISSION_COMPLETED', '2026-09-02 02:31', '{"event_id": "E00004"}', 's4',
   '2026-10-10 21:00+00'),
  ('replay', 'E00002', 'M002', 'ACTION_COMPLETED', '2026-09-02 02:10', '{"event_id": "E00002"}', 's2',
   '2026-10-10 23:00+00'),
  ('replay', 'E00004', 'M002', 'MISSION_COMPLETED', '2026-09-02 02:31', '{"event_id": "E00004"}', 's4',
   '2026-10-10 23:00+00'),
  ('live', NULL, 'M002', 'OPERATOR_NOTE', '2026-09-02 02:15', '{"n": 1}', 'sa', '2026-10-10 23:01+00'),
  ('live', NULL, 'M002', 'OPERATOR_NOTE', '2026-09-02 02:16', '{"n": 2}', 'sb', '2026-10-10 23:02+00');
"""


def _render(sql: str) -> str:
    return (sql.replace("{{EMBED_DIM}}", "1024").replace("{{VEC_TYPE}}", "vector")
            .replace("{{VEC_OPS}}", "vector_cosine_ops").replace("{{HNSW_SET}}", ""))


@pytest.fixture(scope="module")
def pg_url():
    psycopg = pytest.importorskip("psycopg")
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001
        pytest.skip("no local Postgres for the DB tests (%s): %s" % (ADMIN_URL.rsplit("@", 1)[-1], type(exc).__name__))
    name = "spot_mcptest_%s" % uuid.uuid4().hex[:10]
    admin.execute("CREATE DATABASE %s" % name)
    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + name
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            conn.execute("SELECT '[0]'::vector")
            for f in ("001_core.sql", "002_system1_predictions.sql", "003_documents.sql", "004_search_functions.sql", "005_raw_and_app_logs.sql",
                      "009_graph.sql"):
                conn.execute(_render((MIGRATIONS / f).read_text()))
            reasons = json.dumps(REASONS[:2] + [
                {"rule": "gauge_out_of_range", "detail": "PRESSURE_PSI 146.8 > 140"},
                {"rule": "anomaly_high", "detail": "HIGH gauge out of range on HX-1"}])
            conn.execute(FIXTURE_SQL.replace("{reasons}", reasons.replace("'", "''")))
        yield url
    finally:
        admin.execute("DROP DATABASE IF EXISTS %s WITH (FORCE)" % name)
        admin.close()


class Dim1024:
    """Unit vectors of the documents' width; embed_queries takes the call ids like HttpEmbedder."""

    def __init__(self):
        self.ids = []

    async def embed_queries(self, texts, request_id=None, *, flag_id=None, action_id=None):
        self.ids.append((flag_id, action_id))
        return [[1.0] + [0.0] * 1023 for _ in texts]


async def _with_db(url, fn):
    db = PgDatabase(url, max_size=2)
    try:
        return await fn(PgRepo(db))
    finally:
        await db.close()


async def test_db_raw_event_log_returns_each_event_once(pg_url):
    async def run(repo):
        return (await repo.raw_event_log("M002", None, 50), await repo.raw_event_log("M002", None, 2),
                await repo.raw_event_log("M002", "2026-09-02T02:31:00", 2))
    full, head, near = await _with_db(pg_url, run)
    ids = [r["event_id"] for r in full]
    assert ids == ["E00005", "E00002", None, None, "E00004"]                   # robot-time order, no copies
    # contract §5: the seed's replay.ndjson copy wins, so a later replay never changes an event's log:<id>
    assert [r["source"] for r in full if r["event_id"]] == ["replay.ndjson"] * 3
    assert len({r["log_id"] for r in full}) == 5
    assert [r["event_id"] for r in head] == ["E00005", "E00002"]               # the limit counts distinct events
    assert [r["event_id"] for r in near] == [None, "E00004"]
    svc = await _with_db(pg_url, lambda repo: ContextService(repo, None, settings(), FakeSink())
                         .get_raw_event_log("M002", limit=50))
    eids = [e["event_id"] for e in svc["events"] if e["event_id"]]
    assert len(eids) == len(set(eids)) == 3


async def test_db_asset_profile_only_looks_back_in_robot_time(pg_url):
    async def run(repo):
        return (await repo.asset_profile("HX-1", 2), await repo.asset_profile("HX-1", 3),
                await repo.asset_profile("HX-1", None))
    mine, later, unbounded = await _with_db(pg_url, run)
    # flag 2 (A0002, 2026-09-02): flag 3 is later in robot time though created earlier; its own action is excluded
    assert [p["flag_id"] for p in mine["prior_flags"]] == [1]
    assert mine["prior_flags"][0]["notes"] == "recalibrated the gauge"         # blank notes skipped
    assert [r["reading_id"] for r in mine["recent_readings"]] == ["R0001"]
    assert mine["anomaly_count"] == 1 and mine["anomalies_by_type"] == {"GAUGE_OUT_OF_RANGE": 1}
    assert [p["flag_id"] for p in later["prior_flags"]] == [2, 1]              # newest robot time first
    assert {r["reading_id"] for r in later["recent_readings"]} == {"R0001", "R0002"}
    assert unbounded["anomaly_count"] == 3 and len(unbounded["prior_flags"]) == 3


async def test_db_mission_worst_anomaly_has_asset_name_and_criticality(pg_url):
    b = await _with_db(pg_url, lambda repo: repo.mission_bundle("M002"))
    w = b["worst_anomaly"]
    assert w["anomaly_id"] == "AN002" and w["asset_name"] == "Heat Exchanger 01" and w["asset_criticality"] == "HIGH"
    view = await _with_db(pg_url, lambda repo: ContextService(repo, None, settings(), FakeSink())
                          .get_mission_summary("M002"))
    assert view["worst_anomaly"]["asset_name"] == "Heat Exchanger 01"
    assert view["worst_anomaly"]["asset_criticality"] == "HIGH"


async def test_db_packet_with_empty_documents_and_graph_is_sufficient(pg_url):
    """The live state the audit found (no documents, no graph): every miss is a note, so System 2 can answer."""
    emb = Dim1024()

    async def run(repo):
        s = ContextService(repo, emb, settings(embed_dim=1024), FakeSink())
        return (await s.get_flag_context(2), await s.get_flag_context(4), await s.graph_neighborhood("HX-1"),
                await repo.kg_has_nodes())
    p, mission, nb, has_nodes = await _with_db(pg_url, run)
    assert has_nodes is False and nb["graph_empty"] is True and KG_EMPTY in nb["message"]
    for pk in (p, mission):
        assert pk["insufficient_context"] is False and "gaps" not in pk, pk.get("gaps")
        assert "%s: %s" % (NO_GRAPH_FACTS, KG_EMPTY) in pk["notes"]
        assert any(n.startswith("no relevant") for n in pk["notes"])
    assert [r["rule"] for r in p["flag"]["reasons"]] == ["gauge_out_of_range", "anomaly_high"]
    assert {v["rule"] for v in p["flag"]["verdicts"]} == {"laya_unavailable", "rules_verdict"}
    facts = " ".join(f["fact"] for f in p["facts"])
    assert "flag:1" in facts and "flag:3" not in facts                        # no future flag in "prior flags"
    assert "recalibrated the gauge" in facts and " |  | " not in facts
    assert mission["event"]["mission"]["worst_anomaly"]["asset_criticality"] == "HIGH"
    assert (2, "A0002") in emb.ids and (4, None) in emb.ids                    # embed rows carry the flag
    normalize = _system2_normalize()
    for pk in (p, mission):
        assert normalize(json.loads(json.dumps(pk, default=str))).insufficient_context is False


async def test_db_kg_has_nodes_after_graph_build(pg_url):
    import psycopg
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("INSERT INTO kg_nodes (node_id, kind, key, label, summary) "
                     "VALUES ('asset:HX-1', 'asset', 'HX-1', 'Heat Exchanger 01', 'Heat Exchanger 01 at WP01')")
    try:
        assert await _with_db(pg_url, lambda repo: repo.kg_has_nodes()) is True
        out = await _with_db(pg_url, lambda repo: ContextService(repo, Dim1024(), settings(embed_dim=1024),
                                                                  FakeSink()).graph_neighborhood("asset:HX-1"))
        assert "graph_empty" not in out and out["entity"]["id"] == "asset:HX-1"
    finally:
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute("DELETE FROM kg_nodes")


async def test_db_log_sink_drain_lands_the_rows(pg_url):
    import psycopg
    sink = PgLogSink(pg_url)
    sink.tool_call({"request_id": "dq-drain", "flag_id": 2, "tool": "get_flag_context", "args": {"flag_id": 2},
                    "result_count": 3, "result_bytes": 100, "top_similarity": None, "latency_ms": 5, "ok": True,
                    "error": None})
    sink.model_call({"request_id": "dq-drain", "system": "embed", "model": "m", "endpoint": "/v1/embeddings",
                     "flag_id": 2, "action_id": "A0002", "input_tokens": 40, "latency_ms": 12,
                     "tokens_per_sec": 3200.0, "device": "AMD Radeon Pro W7900D", "ok": True, "error": None,
                     "metadata": {"base_host": "localhost:18081"}})
    await sink.drain(5.0)
    assert not sink._tasks
    with psycopg.connect(pg_url) as conn:
        n_tool = conn.execute("SELECT count(*) FROM app.mcp_tool_calls WHERE request_id = 'dq-drain'").fetchone()[0]
        row = conn.execute("SELECT endpoint, device, tokens_per_sec, flag_id, action_id FROM app.model_calls "
                           "WHERE request_id = 'dq-drain'").fetchone()
    await sink.close()
    assert n_tool == 1 and row == ("/v1/embeddings", "AMD Radeon Pro W7900D", 3200.0, 2, "A0002")
