"""Graph RAG unit tests (contract §7): fake graph + fake embedder. Run from amd/mcp:  python -m pytest -q"""
from __future__ import annotations

import json

import pytest
from mcp import Client

from spot_mcp import graph as G
from spot_mcp.repo import GraphUnavailable, PgRepo
from spot_mcp.server import build_server
from spot_mcp.service import ContextService, est_tokens

from fakes import FakeEmbedder, FakeRepo, FakeSink, settings

pytestmark = pytest.mark.anyio

FK_ARROWS = ("—inspects→", "—triggered→", "—flags→", "—part_of→", "—at→", "—during→", "—raised_on→")


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


# ── the packet's graph section ──────────────────────────────────────────────────────────────────
async def test_graph_section_bounded_cited_ranked_and_placed():
    p = await svc().get_flag_context(1)
    keys = list(p)
    assert keys.index("facts") < keys.index("graph") < keys.index("allowed_actions") and keys[-1] == "question"
    g = p["graph"]
    assert 3 <= len(g) and est_tokens(g) <= 300 and est_tokens(p) <= 1500
    for it in g:
        assert it["id"] in p["citation_ids"]                     # every graph fact is citable
        assert " —" in it["fact"] and "→ " in it["fact"]           # a readable triple
    # statistics first, on-point (this asset's own anomaly type), with n and rate from the edge props
    assert g[0]["fact"] == "GAUGE_OUT_OF_RANGE —common_at→ Heat Exchanger 04 (HX-204) (n=3, rate 50%)"
    # one item per cited id: the lower-ranked 'about' fact of the same node is left out (System 2 rejects a repeated
    # id with different content)
    assert not any("—about→ GAUGE_OUT_OF_RANGE" in it["fact"] for it in g)
    ids = [it["id"] for it in g]
    assert len(ids) == len(set(ids))
    assert g[0]["id"] == "anomaly_type:GAUGE_OUT_OF_RANGE"
    assert g[0]["ev"] == "stat:anomaly_type=GAUGE_OUT_OF_RANGE,asset_id=HX-204"
    assert "fails_more_in" in g[1]["fact"] and "INDOOR_DRY" in g[1]["fact"]   # this action type x this environment
    # the manual page the graph walk reaches is also a manual-search hit: it is cited once, by the manual section
    assert "manual:p30-2" in [it["id"] for it in p.get("manual", [])]
    assert not any(it["id"] == "manual:p30-2" for it in g)
    facts = [it["fact"] for it in g]
    assert len(facts) == len(set(facts))
    assert not any(a in f for f in facts for a in FK_ARROWS)        # the flag/event sections already say these
    assert not any("A10" in f for f in facts)                       # instance long tail stays out of the packet
    assert "graph" not in " ".join(p.get("gaps", []))


async def test_graph_section_seeds_follow_the_flag():
    repo = FakeRepo()
    await svc(repo=repo).get_flag_context(1)
    walks = [c for c in repo.graph_calls if c[0] == "graph_neighborhood" and len(c) > 1]
    assert walks and all(set(w[1]) == {"action:A0136", "asset:HX-204", "location:WP07",
                                       "anomaly_type:GAUGE_OUT_OF_RANGE", "policy_rule:gauge_out_of_range"}
                         for w in walks)
    assert all(w[2] == 2 for w in walks)                            # a 2-hop neighbourhood


async def test_mission_flag_gets_graph_from_its_mission():
    p = await svc().get_flag_context(4)
    assert any("common_at" in it["fact"] and "HX-204" in it["fact"] for it in p["graph"])
    assert list(p)[-1] == "question"


async def test_graph_token_bound_and_key_facts_survive_tight_budgets():
    small = await svc(graph_token_budget=90).get_flag_context(1)
    assert small["graph"] and est_tokens(small["graph"]) <= 90
    full = await svc(packet_token_budget=99999).get_flag_context(1)
    tight = await svc(packet_token_budget=1200).get_flag_context(1)
    for key in ("flag", "event", "facts", "question"):              # never trimmed
        assert tight[key] == full[key]
    assert len(full["graph"]) >= 5                                  # the fixture leaves a tail to trim
    assert 3 <= len(tight["graph"]) < len(full["graph"])
    assert tight["graph"] == full["graph"][:len(tight["graph"])]    # trimmed from the tail: best facts kept
    assert est_tokens(full["graph"]) <= 300
    assert est_tokens(tight) <= 1200 and not tight.get("gaps")
    # the cut is recorded (informational, never blocking) and the untrimmed packet has no such note
    assert any(n.startswith("trimmed to the 1200-token packet budget:") and "graph %d->%d" % (
        len(full["graph"]), len(tight["graph"])) in n for n in tight.get("notes", []))
    assert not any(n.startswith("trimmed") for n in full.get("notes", []))


async def test_budget_keeps_the_last_asset_history_and_manual_item_before_the_graph_tail():
    """With a populated graph, the last asset_history / manual item outlives the graph tail down to 4 facts
    (RE-03: the old order left 0 of 58 production packets with any asset history)."""
    full = await svc(packet_token_budget=99999).get_flag_context(1)
    assert full.get("asset_history") and full.get("manual") and len(full["graph"]) > 4
    p = await svc(packet_token_budget=1550).get_flag_context(1)     # the old order dropped asset_history here
    assert len(p["graph"]) == 4 < len(full["graph"]) and est_tokens(p) <= 1550
    assert len(p["asset_history"]) == 1 and len(p["manual"]) == 1


# ── rendering, orientation, ranking, dedup ──────────────────────────────────────────────────────
def test_triple_rendering_orientation_and_evidence():
    assert G.node_text("location:WP09", "Turbine Hall South") == "Turbine Hall South (WP09)"
    assert G.node_text("environment:HIGH_NOISE", "High noise") == "HIGH_NOISE"   # vocabulary renders as its key
    assert G.node_text("asset:HX-204", "HX-204") == "HX-204"
    # reached against the edge direction, no edge lookup: orientation comes from the relation table
    rows = [{"node_id": "location:WP09", "kind": "location", "label": "Turbine Hall South", "hop": 1,
             "via_rel": "has_environment", "via_node": "environment:HIGH_NOISE", "weight": 1.0, "evidence": None}]
    cands, nodes = G.candidates(rows, ["environment:HIGH_NOISE"])
    (c,) = G.rank(cands, {"environment:HIGH_NOISE"})
    assert (c["src"], c["dst"]) == ("location:WP09", "environment:HIGH_NOISE")
    assert G.render(c, nodes) == {"id": "location:WP09",
                                  "fact": "Turbine Hall South (WP09) —has_environment→ HIGH_NOISE"}
    assert G.stat_suffix("common_at", 0.5, {"n": 6, "rate": 0.5}) == " (n=6, rate 50%)"
    assert G.stat_suffix("common_at", 0.27, {"n": 6, "of": 22, "rate": 0.2727}) == " (n=6 of 22, rate 27.3%)"
    assert G.stat_suffix("fails_more_in", 0.64, {"n": 14, "rate": 0.64, "overall_rate": 0.81}) == \
        " (n=14, success 64% vs 81% overall)"
    assert G.stat_suffix("common_at", 0.43, {}) == " (rate 43%)"     # weight is the rate when props are missing
    assert G.norm_evidence("AN002") == "anomaly:AN002" and G.norm_evidence("A0136") == "action:A0136"
    assert G.norm_evidence("manual:p30-2") == "manual:p30-2" and G.norm_evidence("") is None
    assert G.cite_id("operator_note:5", "operator_note", "note:5") == "note:5"   # doc-backed: cite the doc id
    assert G.cite_id("policy_rule:battery_low", "policy_rule", "policy:battery_low") == "policy:battery_low"
    assert G.cite_id("action:A0136", "action", "event:act-A0136") == "action:A0136"   # the packet's row id


def _cand(src, rel, dst, node, hop=1, weight=1.0):
    return {"src": src, "rel": rel, "dst": dst, "node": node, "via": dst if node == src else src,
            "kind": G.kind_of(node), "doc_id": node if G.kind_of(node) == "manual" else None, "summary": None,
            "hop": hop, "weight": weight, "props": {}, "evidence": None}


def test_ranking_tiers_on_point_and_dedup():
    ctx = {"asset:HX-204", "location:WP07", "failure_type:F", "environment:E", "action_type:T"}
    cands = [
        _cand("action:A1", "inspects", "asset:HX-204", "action:A1"),                         # instance: last
        _cand("asset:HX-204", "located_at", "location:WP07", "location:WP07", hop=0),       # FK among context: dropped
        _cand("location:WP07", "has_environment", "environment:Z", "environment:Z"),        # structure
        _cand("failure_type:F", "relates_to", "topic:t", "topic:t"),                        # topic
        _cand("manual:p1-1", "mentions", "topic:t", "manual:p1-1", hop=2),                  # manual (off-point)
        _cand("manual:p2-1", "mentions", "failure_type:F", "manual:p2-1"),                  # manual (on-point)
        _cand("failure_type:F", "common_at", "location:WP99", "location:WP99", weight=0.9),  # stat, off-point
        _cand("action_type:T", "fails_more_in", "environment:E", "action_type:T", weight=0.4),  # stat, on-point
        _cand("action_type:T", "fails_more_in", "environment:E", "action_type:T", weight=0.4),  # duplicate
    ]
    ranked = G.rank(cands, ctx, drop_fk_within=ctx)
    order = [(c["src"], c["rel"]) for c in ranked]
    # on-point stat, on-point manual, on-point topic, off-point stat, off-point manual, structure, instances
    assert order == [("action_type:T", "fails_more_in"), ("manual:p2-1", "mentions"), ("failure_type:F", "relates_to"),
                     ("failure_type:F", "common_at"), ("manual:p1-1", "mentions"),
                     ("location:WP07", "has_environment"), ("action:A1", "inspects")]


def test_merge_shared_end_and_tier_caps():
    nodes = {"manual:p1-1": {"label": "Docking"}, "asset:HX-204": {"label": "Heat Exchanger 04"}}
    cands = [_cand("manual:p1-1", "mentions", "topic:battery", "manual:p1-1"),
             _cand("manual:p1-1", "mentions", "topic:docking", "manual:p1-1")] + \
            [_cand("action:A%d" % i, "inspects", "asset:HX-204", "action:A%d" % i) for i in range(7)]
    ranked = G.rank(cands, {"asset:HX-204"})
    out = G.pack(ranked, nodes, est_tokens)
    assert out[0] == {"id": "manual:p1-1", "fact": "Docking —mentions→ battery, docking"}
    assert out[1]["fact"] == "A0, A1, A2, A3, … —inspects→ Heat Exchanger 04 (HX-204)"
    # caps: first pass takes caps[tier] per tier, second pass fills by rank
    many = [_cand("failure_type:F%d" % i, "common_at", "location:WP0%d" % i, "failure_type:F%d" % i) for i in range(6)]
    assert len(G.pack(G.rank(many, set()), {}, est_tokens, caps=G.TIER_CAPS, max_items=5)) == 5    # caps, then fill
    assert len(G.pack(G.rank(many, set()), {}, est_tokens, caps=G.TIER_CAPS, fill=False)) == 2     # 2 off-point stats
    on_point = {"failure_type:F%d" % i for i in range(6)} | {"location:WP0%d" % i for i in range(6)}
    assert len(G.pack(G.rank(many, on_point), {}, est_tokens, caps=G.TIER_CAPS, fill=False)) == 4  # 4 on-point
    assert len(G.pack(G.rank(many, set()), {}, est_tokens, token_budget=40)) < 6


# ── tools: entity resolution, search, validation ────────────────────────────────────────────────
@pytest.mark.parametrize("entity,how,node,embeds", [
    ("asset:HX-204", "node_id", "asset:HX-204", 0),
    ("HX-204", "text", "asset:HX-204", 0),
    ("hx204", "text", "asset:HX-204", 0),
    ("Turbine Hall South", "text", "location:WP09", 0),
    ("robot gets lost near loud turbines", "vector", None, 1),
])
async def test_entity_resolution_order(entity, how, node, embeds):
    repo, emb = FakeRepo(), FakeEmbedder()
    out = await svc(repo=repo, embedder=emb, min_similarity=0.0).graph_neighborhood(entity, hops=2, k=10)
    assert out["resolved_by"] == how and len(emb.calls) == embeds
    if node:
        assert out["entity"]["id"] == node
    names = [c[0] for c in repo.graph_calls]
    if how == "node_id":
        assert "match_kg_nodes_text" not in names and "match_kg_nodes" not in names
    if how == "vector":
        assert names.index("match_kg_nodes_text") < names.index("match_kg_nodes")   # text first, then vector
    assert out["facts"] and all(" —" in f["fact"] for f in out["facts"])
    assert out["result_count"] == len(out["facts"])


async def test_graph_neighborhood_facts_and_no_match():
    out = await svc().graph_neighborhood("location:WP09", hops=2, k=10)
    facts = [f["fact"] for f in out["facts"]]
    assert facts[0] == "LOCALIZATION_FAILURE —common_at→ Turbine Hall South (WP09) (n=6 of 14, rate 43%)"
    assert "Turbine Hall South (WP09) —has_environment→ HIGH_NOISE" in facts
    assert "NAVIGATION —fails_more_in→ HIGH_NOISE (n=14, success 64% vs 81% overall)" in facts   # 2 hops out
    one = await svc().graph_neighborhood("location:WP09", hops=1, k=10)
    assert not any("fails_more_in" in f["fact"] for f in one["facts"])
    # an asset's own actions come last, merged, and never crowd out the knowledge
    hx = [f["fact"] for f in (await svc().graph_neighborhood("HX-204", k=30))["facts"]]
    inspects = [i for i, f in enumerate(hx) if "—inspects→ Heat Exchanger 04" in f]
    assert "common_at" in hx[0] and len(inspects) == 1 and ", …" in hx[inspects[0]]
    assert inspects[0] > max(i for i, f in enumerate(hx) if "mentions" in f or "relates_to" in f)
    none = await svc().graph_neighborhood("zebra migration patterns savanna")
    assert none["no_relevant_results"] is True and none["facts"] == [] and "Do not cite" in none["message"]


async def test_search_graph_merges_text_and_vector():
    out = await svc().search_graph("localization", k=3)
    ids = [e["id"] for e in out["entry_nodes"]]
    assert len(ids) == len(set(ids)) and len(ids) <= 3
    assert out["entry_nodes"][0]["id"] == "topic:localization" and out["entry_nodes"][0]["match"] == "text+vector"
    assert any("common_at" in f["fact"] for f in out["facts"]) or any("mentions" in f["fact"] for f in out["facts"])
    assert out["result_count"] == len(ids) + len(out["facts"]) and out["top_similarity"] > 0
    junk = await svc().search_graph("zebra migration patterns savanna")
    assert junk["no_relevant_results"] is True and "Do not cite" in junk["message"]


@pytest.mark.parametrize("tool,args", [
    ("graph_neighborhood", {"entity": "HX-204", "hops": 0}),
    ("graph_neighborhood", {"entity": "HX-204", "hops": 4}),
    ("graph_neighborhood", {"entity": "HX-204", "k": 0}),
    ("graph_neighborhood", {"entity": "HX-204", "k": 31}),
    ("graph_neighborhood", {"entity": ""}),
    ("graph_neighborhood", {"entity": "x" * 121}),
    ("search_graph", {"query": "battery", "k": 11}),
    ("search_graph", {"query": "battery", "hops": 4}),
    ("search_graph", {"query": "x"}),
])
async def test_graph_schema_rejects_bad_params(tool, args):
    is_err, _ = await call(svc(), tool, args)
    assert is_err


async def test_graph_service_level_validation():
    s = svc()
    for kw in ({"hops": True}, {"hops": 5}, {"k": 99}):
        with pytest.raises(ValueError):
            await s.graph_neighborhood("HX-204", **kw)
    with pytest.raises(ValueError):
        await s.search_graph("battery", hops=0)
    with pytest.raises(ValueError):
        await s.graph_neighborhood("   ")


# ── graceful degradation ────────────────────────────────────────────────────────────────────────
async def test_db_without_graph_degrades_everywhere():
    s = svc(repo=FakeRepo(graph="missing"))
    p = await s.get_flag_context(1)
    assert "graph" not in p and p["facts"] and p["insufficient_context"] is False
    assert any(g.startswith("graph unavailable") for g in p["gaps"])
    for tool, args in (("graph_neighborhood", {"entity": "HX-204"}), ("search_graph", {"query": "battery"})):
        is_err, out = await call(s, tool, args)
        assert not is_err and out["graph_unavailable"] is True and out["result_count"] == 0


async def test_broken_traversal_never_breaks_the_packet():
    p = await svc(repo=FakeRepo(graph="broken")).get_flag_context(1)
    assert "graph" not in p and "graph lookup failed (RuntimeError)" in p["gaps"]
    assert list(p)[-1] == "question" and p["similar_incidents"]


async def test_edge_lookup_failure_falls_back_to_traversal_edges():
    p = await svc(repo=FakeRepo(graph="no_details")).get_flag_context(1)
    assert p["graph"] and all(it["id"] in p["citation_ids"] for it in p["graph"])
    assert all(" —" in it["fact"] for it in p["graph"])


async def test_embedding_outage_graph_still_works():
    s = svc(embedder=FakeEmbedder(fail=True))
    p = await s.get_flag_context(1)
    assert p["graph"] and any("retrieval skipped" in g for g in p["gaps"])   # graph needs no embedding
    out = await s.search_graph("HX-204")
    assert out["entry_nodes"][0]["id"] == "asset:HX-204" and out["entry_nodes"][0]["match"] == "text"
    assert "vector search skipped" in out["note"]
    v = await s.graph_neighborhood("robot gets lost near loud turbines")
    assert v["no_relevant_results"] is True and "vector fallback was skipped" in v["note"]
    by_name = await s.graph_neighborhood("Turbine Hall South")
    assert by_name["resolved_by"] == "text" and by_name["facts"]


# ── logging + SQL safety ────────────────────────────────────────────────────────────────────────
async def test_graph_tools_are_logged():
    sink = FakeSink()
    for tool, args in (("graph_neighborhood", {"entity": "WP09", "hops": 2}), ("search_graph", {"query": "gauge"})):
        is_err, out = await call(svc(sink=sink), tool, args, meta={"request_id": "req-g"})
        assert not is_err
        row = sink.tools[-1]
        assert row["tool"] == tool and row["request_id"] == "req-g" and row["ok"] is True
        assert row["result_count"] == out["result_count"] > 0 and row["args"] == {"k": row["args"]["k"], **args,
                                                                                    "hops": row["args"]["hops"]}


class _RecDB:
    def __init__(self, exc=None):
        self.exc, self.seen = exc, []

    async def fetch(self, sql, params=()):
        self.seen.append((sql, params))
        if self.exc:
            raise self.exc
        return [{"nodes": [], "edges": []}]


async def test_graph_sql_is_parameterized_capped_and_translates_missing_db():
    db = _RecDB()
    repo = PgRepo(db)
    await repo.match_kg_nodes_text("HX-204'; DROP TABLE kg_nodes;--", None, 999)
    await repo.graph_neighborhood(["asset:HX-204"] * 50, 9, None, ["topic"], 999)
    await repo.kg_details(["asset:HX-204'--"])
    for sql, params in db.seen:
        assert "DROP" not in sql and "HX-204" not in sql
    assert db.seen[0][1][-1] == 20                                   # entry nodes capped
    assert db.seen[1][1][0] == ["asset:HX-204"] * 20 and db.seen[1][1][1] == 3 and db.seen[1][1][-1] == 60

    class Missing(Exception):
        sqlstate = "42883"
    with pytest.raises(GraphUnavailable):
        await PgRepo(_RecDB(Missing("function graph_neighborhood does not exist"))).graph_neighborhood(["x"], 2, None,
                                                                                                        None, 40)
    with pytest.raises(RuntimeError, match="boom"):                  # other errors are not masked
        await PgRepo(_RecDB(RuntimeError("boom"))).match_kg_nodes_text("x", None, 5)


def test_pack_keeps_one_fact_per_cited_node():
    """Two stat edges of one failure_type cite the same id: only the better-ranked fact is packed (rehearsal
    2026-10-11: 12 of 58 flag packets repeated an id and System 2 answered 422 'conflicting content for duplicate id')."""
    nodes = {"failure_type:READING_UNRELIABLE": {"kind": "failure_type", "label": "READING_UNRELIABLE"},
             "location:WP09": {"kind": "location", "label": "Turbine Hall South"},
             "location:WP04": {"kind": "location", "label": "Pump Bay"}}
    ranked = [{"src": "failure_type:READING_UNRELIABLE", "rel": "common_at", "dst": dst, "node": "failure_type:READING_UNRELIABLE",
               "via": dst, "kind": "failure_type", "tier": 0, "on_point": True, "weight": w, "edge_weight": w,
               "props": {"n": n, "of": of}, "evidence": None}
              for dst, w, n, of in (("location:WP09", 0.136, 3, 22), ("location:WP04", 0.111, 2, 18))]
    out = G.pack(ranked, nodes, lambda x: len(str(x)) // 4)
    assert [it["id"] for it in out] == ["failure_type:READING_UNRELIABLE"]
    assert "Turbine Hall South (WP09)" in out[0]["fact"] and "Pump Bay" not in out[0]["fact"]
