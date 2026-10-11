"""Unit tests: fake DB + fake embedder. Run from amd/mcp:  python -m pytest -q"""
from __future__ import annotations

import json

import pytest
from mcp import Client

from spot_mcp import domain as D
from spot_mcp.repo import reliability_sql
from spot_mcp.server import build_server
from spot_mcp.service import ContextService, est_tokens

from fakes import FakeEmbedder, FakeRepo, FakeSink, settings

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def svc(**kw):
    sink = kw.pop("sink", FakeSink())
    return ContextService(kw.pop("repo", FakeRepo()), kw.pop("embedder", FakeEmbedder()), settings(**kw), sink)


async def call(service, tool, args, meta=None):
    async with Client(build_server(service)) as c:
        r = await c.call_tool(tool, args, meta=meta)
        text = r.content[0].text
        return r.is_error, (text if r.is_error else json.loads(text))


# ── parameter validation ────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("tool,args", [
    ("search_spot_manual", {"query": "battery", "k": 50}),
    ("search_spot_manual", {"query": "x"}),
    ("search_asset_history", {"asset_id": "HX-204'; DROP TABLE flags;--"}),
    ("get_reliability_stats", {"group_by": "1; SELECT"}),
    ("get_reliability_stats", {"action_type": "TELEPORT"}),
    ("get_battery_risk", {"battery_pct": 140}),
    ("list_allowed_actions", {"category": "bogus"}),
    ("get_flag_context", {"flag_id": 0}),
    ("get_raw_event_log", {"mission_id": "M014", "limit": 500}),
])
async def test_schema_rejects_bad_params(tool, args):
    is_err, _ = await call(svc(), tool, args)
    assert is_err


async def test_service_level_validation():
    s = svc()
    with pytest.raises(ValueError, match="flag_id .* or text"):
        await s.search_similar_incidents()
    with pytest.raises(ValueError):
        await s.search_spot_manual("battery", k=True)  # bool is not an int here
    with pytest.raises(ValueError, match="around_ts"):
        await s.get_raw_event_log("M014", around_ts="yesterday")
    with pytest.raises(ValueError, match="no flag"):
        await s.get_flag_context(999)


def test_reliability_sql_is_parameterized():
    sql, params = reliability_sql({"action_type": "GAUGE_READING", "location_id": "WP07'--"}, "battery_band")
    assert "WP07" not in sql and "GAUGE_READING" not in sql
    assert params == ["GAUGE_READING", "WP07'--"]
    with pytest.raises(ValueError):
        reliability_sql({}, "a.status; DROP")
    with pytest.raises(ValueError):
        reliability_sql({"status": "x"}, None)


# ── packet assembly + citation ids ──────────────────────────────────────────────────────────────
async def test_flag_context_packet_shape_and_citations():
    is_err, p = await call(svc(), "get_flag_context", {"flag_id": 1})
    assert not is_err
    keys = list(p)
    assert keys[1] == "flag" and keys[-1] == "question"          # key facts first, question last
    assert p["insufficient_context"] is False
    assert p["flag"]["id"] == "flag:1"
    ev = p["event"]
    assert ev["id"] == "action:A0136" and ev["asset"]["id"] == "asset:HX-204"
    r = ev["readings"][0]
    assert r["id"] == "reading:R0002" and r["trust"] == "trusted" and r["assessment"] == "above safe band"
    assert ev["validator"]["priority"] == "P1" and ev["validator"]["category"] == "equipment"
    assert p["suggested_action"] == "DISPATCH_TECHNICIAN" and p["suggested_action"] in p["allowed_actions"]
    # derived facts are numbered F1..Fn and mention real numbers from the data
    assert [f["id"] for f in p["facts"]] == ["F%d" % (i + 1) for i in range(len(p["facts"]))]
    assert any("146.8" in f["fact"] for f in p["facts"])
    assert any("33.3%" in f["fact"] and "(3/9)" in f["fact"] for f in p["facts"])  # battery band <=25
    # every cited thing is in citation_ids, no duplicates
    ids = p["citation_ids"]
    assert len(ids) == len(set(ids))
    for must in ("flag:1", "action:A0136", "reading:R0002", "anomaly:AN002", "asset:HX-204", "F1", "s1:501"):
        assert must in ids
    # the flag's own incident and its own anomaly doc are never returned as "similar"
    sim_ids = [i["id"] for i in p.get("similar_incidents", [])]
    assert "incident:flag-1" not in sim_ids and "incident:AN002" not in sim_ids
    assert "incident:AN007" in sim_ids
    assert p["policy"][0]["id"] == "policy:gauge_out_of_range"     # exact rule doc first
    assert est_tokens(p) <= 1500
    assert "big" not in json.dumps(p)                              # raw payload never leaks into the packet


async def test_packet_trims_to_budget():
    full = await svc().get_flag_context(1)
    small = await svc(packet_token_budget=700).get_flag_context(1)
    n = lambda p: sum(len(p.get(k, [])) for k in ("similar_incidents", "asset_history", "manual", "policy"))
    assert n(small) < n(full)
    assert list(small)[-1] == "question"


async def test_mission_flag_packet():
    p = await svc().get_flag_context(4)
    m = p["event"]["mission"]
    assert m["true_health"]["status_misleading"] is True
    assert "allowed_actions" in p and "REVIEW_MISSION" in p["allowed_actions"]
    assert p["suggested_action"] == "REVIEW_MISSION"


# ── insufficient context ────────────────────────────────────────────────────────────────────────
async def test_insufficient_context_when_action_missing():
    p = await svc().get_flag_context(2)
    assert p["insufficient_context"] is True
    assert any("A9999" in m for m in p["missing"])
    assert list(p)[-1] == "question"


async def test_insufficient_context_when_no_subject():
    p = await svc().get_flag_context(3)
    assert p["insufficient_context"] is True and "neither action_id nor mission_id" in p["missing"][0]


async def test_embedding_outage_degrades_to_sql_only():
    p = await svc(embedder=FakeEmbedder(fail=True)).get_flag_context(1)
    assert p["insufficient_context"] is False            # SQL facts still answer the question
    assert any("retrieval skipped" in g for g in p["gaps"])
    assert "similar_incidents" not in p and p["facts"]


# ── similarity cutoff ───────────────────────────────────────────────────────────────────────────
async def test_cutoff_returns_explicit_no_results():
    out = await svc().search_spot_manual("zebra migration patterns savanna")
    assert out["results"] == [] and out["no_relevant_results"] is True
    assert "Do not cite" in out["message"]


async def test_cutoff_keeps_relevant():
    out = await svc().search_spot_manual("battery low return to dock charging")
    assert out["results"][0]["id"] == "manual:p42-1"
    assert all(r["sim"] >= 0.40 for r in out["results"])


@pytest.mark.parametrize("has_sim", [False, True])
async def test_hybrid_exact_id_survives_cutoff(has_sim):
    s = svc(repo=FakeRepo(hybrid_has_similarity=has_sim), min_similarity=0.99)
    out = await s.search_similar_incidents(text="anything about PANEL-212")
    assert [r["id"] for r in out["results"]] == ["incident:AN010"]   # exact id match kept despite low similarity


async def test_asset_history_without_query_has_no_cutoff():
    out = await svc(min_similarity=0.99).search_asset_history("HX-204")
    assert out["results"] and all(r["asset_id"] == "HX-204" for r in out["results"])


# ── SQL tools ───────────────────────────────────────────────────────────────────────────────────
async def test_reliability_and_small_sample_warning():
    out = await svc().get_reliability_stats(action_type="GAUGE_READING", group_by="battery_band")
    rows = {r["battery_band"]: r for r in out["rows"]}
    assert rows["<=25"]["success_rate_pct"] == 33.3 and "small sample" in rows["<=25"]["warning"]
    assert rows["<=25"]["id"] == "stat:action_type=GAUGE_READING,battery_band=<=25"


async def test_battery_risk():
    out = await svc().get_battery_risk(22)
    assert out["policy_status"] == "LOW" and out["band"] == "<=25"
    assert out["this_band"]["n"] == 9 and len(out["all_bands"]) >= 2


async def test_system1_forecast_vs_actual():
    out = await svc().get_system1_forecast("A0136")
    by_q = {r["question"]: r for r in out["forecast"]}
    assert by_q["will_fail"]["correct"] is False             # predicted fail, action succeeded
    assert by_q["anomaly_expected"]["correct"] is True
    assert by_q["priority"]["agrees_with_validator"] is True
    assert by_q["will_fail"]["id"] == "s1:501"


async def test_allowed_actions_and_raw_log():
    acts = await svc().list_allowed_actions("robot_health")
    assert "RETURN_TO_DOCK" in [a["action"] for a in acts["actions"]]
    log = await svc().get_raw_event_log("M014")
    assert [e["id"] for e in log["events"]] == ["log:11", "log:12"]


# ── logging ─────────────────────────────────────────────────────────────────────────────────────
async def test_tool_calls_are_logged_with_request_id():
    sink = FakeSink()
    is_err, _ = await call(svc(sink=sink), "get_flag_context", {"flag_id": 1}, meta={"request_id": "req-7"})
    assert not is_err
    row = sink.tools[-1]
    assert row["tool"] == "get_flag_context" and row["request_id"] == "req-7" and row["flag_id"] == 1
    assert row["ok"] is True and row["result_bytes"] > 0 and row["top_similarity"] is not None


async def test_failed_call_logged_and_broken_sink_never_breaks():
    sink = FakeSink()
    is_err, msg = await call(svc(sink=sink), "get_flag", {"flag_id": 999})
    assert is_err and "no flag" in msg and sink.tools[-1]["ok"] is False
    is_err, _ = await call(svc(sink=FakeSink(broken=True)), "get_flag", {"flag_id": 1})
    assert not is_err


# ── embedder + policy drift ─────────────────────────────────────────────────────────────────────
async def test_http_embedder_prefix_dim_and_logging():
    import httpx2
    from spot_mcp.embed import EmbeddingError, HttpEmbedder
    seen = {}

    def handler(req):
        body = json.loads(req.content)
        seen.update(body)
        return httpx2.Response(200, json={"data": [{"index": i, "embedding": [0.1] * 4} for i in range(len(body["input"]))],
                                          "usage": {"prompt_tokens": 7}})
    sink = FakeSink()
    e = HttpEmbedder("http://x", "m", 4, "Instruct: q\nQuery:", sink=sink, transport=httpx2.MockTransport(handler))
    v = await e.embed_queries(["battery low"], request_id="r1")
    assert len(v[0]) == 4 and seen["input"] == ["Instruct: q\nQuery:battery low"] and seen["model"] == "m"
    assert sink.models[-1]["system"] == "embed" and sink.models[-1]["ok"] and sink.models[-1]["request_id"] == "r1"
    await e.embed_queries(["battery low"])                 # cached: no second HTTP call
    assert len(sink.models) == 1
    bad = HttpEmbedder("http://x", "m", 8, sink=sink, transport=httpx2.MockTransport(handler))
    with pytest.raises(EmbeddingError, match="dimension"):
        await bad.embed_queries(["q"])


@pytest.mark.parametrize("first,retried", [("disconnect", True), (503, True), (429, True), ("timeout", False),
                                            (400, False)])
async def test_http_embedder_retries_once_on_a_dropped_connection_only(first, retried):
    """One dropped tunnel connection must not turn a flag packet into insufficient_context (RE-04); a timeout or a
    4xx is not retried. Every HTTP attempt is its own app.model_calls row."""
    import httpx2
    from spot_mcp.embed import EmbeddingError, HttpEmbedder
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            if first == "disconnect":
                raise httpx2.RemoteProtocolError("Server disconnected without sending a response.", request=req)
            if first == "timeout":
                raise httpx2.ReadTimeout("slow", request=req)
            return httpx2.Response(first, text="busy")
        return httpx2.Response(200, json={"data": [{"index": 0, "embedding": [0.1] * 4}]})
    sink = FakeSink()
    e = HttpEmbedder("http://x", "m", 4, sink=sink, transport=httpx2.MockTransport(handler), retry_delay_s=0.01)
    if retried:
        assert len((await e.embed_queries(["q"]))[0]) == 4
        assert [m["ok"] for m in sink.models] == [False, True]
    else:
        with pytest.raises(EmbeddingError):
            await e.embed_queries(["q"])
        assert len(calls) == 1 and [m["ok"] for m in sink.models] == [False]


def test_vendored_policy_matches_repo():
    """The Docker image only has amd/mcp, so spot_mcp/vendor/ carries copies. Re-copy them when this fails:
    cp amd/system1/triage/{policy,questions}.py amd/mcp/spot_mcp/vendor/"""
    from pathlib import Path
    repo = Path(D.__file__).resolve().parents[2] / "system1" / "triage"
    vendor = Path(D.__file__).resolve().parent / "vendor"
    for name in ("policy.py", "questions.py"):
        assert (vendor / name).read_text() == (repo / name).read_text(), "vendored %s is stale" % name
    assert D.POLICY_SOURCE.endswith(D.policy.POLICY_VERSION)
