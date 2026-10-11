"""Graph RAG (contract §7): db/graph_build.py and the 009_graph.sql functions.

- Unit tests (always run): the mention extractor and the curated maps.
- Integration tests: a throwaway database spot_graphtest_<hex> on the compose Postgres (DB_PORT, default 5433;
  or GRAPH_TEST_ADMIN_URL), migrated and seeded, with the stack's RAG `documents` (and their vectors) copied in from
  GRAPH_TEST_SOURCE_URL (default: the `spot` database on the same server), then `graph_build.py --no-embed` twice.
  The stack's own database is only read. Skips without Postgres. GRAPH_TEST_EMBED=1 also checks the embedding path
  against EMBED_URL (~20 s on CPU).
- Also covered: flag summaries and flag incident documents list policy rules only (no laya_unavailable /
  rules_verdict text), live-simulation nodes, edges and documents (props/metadata source = 'live') survive a
  graph rebuild and an ingest prune, and a failed embedding batch keeps the vectors of the batches before it.

    make test-graph        # = DB_PORT=<.env> backend/.venv/bin/python -m pytest db/tests -q
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "db"))
sys.path.insert(0, str(ROOT / "amd" / "embed"))
psycopg = pytest.importorskip("psycopg")

SETUP, BUILD = ROOT / "db" / "setup.py", ROOT / "db" / "graph_build.py"
ADMIN_URL = os.environ.get("GRAPH_TEST_ADMIN_URL") or "postgresql://postgres:%s@localhost:%s/postgres" % (
    os.environ.get("POSTGRES_PASSWORD", "postgres"), os.environ.get("DB_PORT", "5433"))
SOURCE_URL = os.environ.get("GRAPH_TEST_SOURCE_URL") or ADMIN_URL.rsplit("/", 1)[0] + "/spot"
CONTRACT_RELS = {"part_of", "at", "inspects", "of_type", "failed_with", "located_at", "has_environment", "raised_on",
                 "during", "flags", "triggered", "common_at", "fails_more_in", "about", "relates_to", "mentions",
                 "annotates", "explains"}


# ── unit: extractor + curated maps (no database) ──────────────────────────────────────────────────
def test_extractor_ids_synonyms_and_topics():
    import graph_build as G
    ex = G.Extractor([{"asset_id": "HX-204", "asset_name": "Heat Exchanger 04"},
                      {"asset_id": "PANEL-212", "asset_name": "Electrical Panel 12"}],
                     [{"location_id": "WP05", "location_name": "Pipe Gallery"},
                      {"location_id": "WP09", "location_name": "Turbine Hall South"}])
    got = ex("Spot lost its localization near hx 204 in the Pipe Gallery (WP5) again; battery low, sent back to the "
             "dock. PANEL-212 fine. HX-999 is not an asset.")
    assert got["asset:HX-204"][0] == 1 and got["asset:PANEL-212"][0] == 1 and "asset:HX-999" not in got
    assert got["location:WP05"][0] == 2                     # 'WP5' and the name 'Pipe Gallery'
    for node in ("failure_type:LOCALIZATION_FAILURE", "topic:localization", "topic:battery", "topic:docking"):
        assert node in got, node
    assert "topic:acoustic" not in ex("signal noise on the reading")          # 'signal noise' is data quality
    assert "failure_type:SIGNAL_NOISE" in ex("signal noise on the reading")
    assert ex("") == {} and ex(None) == {}


def test_curated_maps_cover_policy():
    import graph_build as G
    from triage import policy as P
    assert set(G.RELATES_TO["failure_type"]) == P.NAV_FAILURES | P.DATA_FAILURES | P.COMMS_FAILURES
    assert set(G.RELATES_TO["anomaly_type"]) == P.EQUIPMENT_ANOMALIES | P.NAV_ANOMALIES | P.DATA_ANOMALIES
    assert {t[len("topic:"):] for ts in G.ABOUT.values() for t in ts if t.startswith("topic:")} <= set(G.TOPICS)


def test_embed_keeps_the_batches_that_succeeded(monkeypatch):
    import graph_build as G
    import spotdb
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")   # model-call logging: unreachable
    monkeypatch.delenv("LOGS_DATABASE_URL", raising=False)
    g = G.Graph({})
    for i in range(5):
        g.node("topic", "t%d" % i, "T%d" % i, "summary %d" % i)

    class FlakyClient:
        batch_size, model, on_call, calls = 2, "test-model", None, 0

        def embed(self, texts, kind):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("bridge dropped the request")
            return [[0.5, 0.5, 0.5] for _ in texts]

    run = spotdb.JobRun(None)
    todo = sorted(g.nodes)
    out = G._embed(FlakyClient(), g, todo, run, lambda v: "[0.5,0.5,0.5]")
    assert sorted(out) == todo[:2]                          # batch 1 kept; batch 2 failed; batch 3 never sent
    assert "bridge dropped" in run.counts["embed_error"]
    assert all(g.nodes[n]["props"].get("embedded_hash") for n in todo[:2])
    assert not any(g.nodes[n]["props"].get("embedded_hash") for n in todo[2:])


# ── integration fixture ───────────────────────────────────────────────────────────────────────────
def _env(url):
    env = {k: v for k, v in os.environ.items() if k != "LOGS_DATABASE_URL"}
    env["DATABASE_URL"] = url
    return env


def _run(script, url, *args):
    r = subprocess.run([sys.executable, str(script), *args], env=_env(url), capture_output=True, text=True,
                       timeout=900)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    return r.stdout


def _copy_documents(src_url, dst_url) -> int:
    cols = ["doc_id", "kind", "source_id", "mission_id", "action_id", "asset_id", "location_id", "action_type",
            "status", "anomaly_type", "severity", "ts", "title", "content", "metadata", "embedding", "embed_model"]
    try:
        with psycopg.connect(src_url, connect_timeout=3) as s:
            rows = s.execute("SELECT %s FROM documents WHERE kind IN ('event','incident','manual','policy')" % ", ".join(
                c + "::text" if c == "embedding" else c for c in cols)).fetchall()
    except psycopg.Error:
        return 0
    with psycopg.connect(dst_url) as d, d.cursor() as cur:
        cur.executemany("INSERT INTO documents (%s) VALUES (%s) ON CONFLICT DO NOTHING" % (
            ", ".join(cols), ", ".join("%s::vector" if c == "embedding" else "%s" for c in cols)),
            [tuple(psycopg.types.json.Jsonb(v) if c == "metadata" else v for c, v in zip(cols, r)) for r in rows])
    return len(rows)


def _q(url, sql, *args):
    with psycopg.connect(url, autocommit=True) as c:
        cur = c.execute(sql, args)
        return cur.fetchall() if cur.description else []


def _snapshot(url):
    return {
        "nodes": _q(url, "SELECT kind, count(*), count(embedding) FROM kg_nodes GROUP BY 1 ORDER BY 1"),
        "edges": _q(url, "SELECT rel, count(*), round(sum(weight)::numeric, 3) FROM kg_edges GROUP BY 1 ORDER BY 1"),
        "node_md5": _q(url, "SELECT md5(string_agg(node_id || label || summary || props::text || coalesce(doc_id, ''), "
                            "'|' ORDER BY node_id)), max(updated_at) FROM kg_nodes")[0],
        "edge_md5": _q(url, "SELECT md5(string_agg(src || rel || dst || weight || props::text || coalesce(evidence, ''), "
                            "'|' ORDER BY src, rel, dst)) FROM kg_edges")[0][0],
    }


@pytest.fixture(scope="module")
def db():
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3)
    except psycopg.Error as exc:
        pytest.skip("no Postgres at %s (start the stack: make up): %s" % (ADMIN_URL.split("@")[-1], exc))
    name = "spot_graphtest_" + uuid.uuid4().hex[:8]
    admin.execute("CREATE DATABASE %s" % name)
    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + name
    try:
        _run(SETUP, url, "migrate")
        _run(SETUP, url, "seed")
        n_docs = _copy_documents(SOURCE_URL, url)
        # one operator note and one (non-template) System 2 explanation, so those node kinds and edges exist
        flag_id = _q(url, "SELECT flag_id FROM flags WHERE action_id = 'A0061' ORDER BY flag_id LIMIT 1")[0][0]
        _q(url, "INSERT INTO acknowledgements (flag_id, action, note) VALUES (%s, 'NOTE', %s)", flag_id,
           "PANEL-203 again: Spot lost its localization in the Pipe Gallery, relocalized at the fiducial.")
        _q(url, "INSERT INTO assessments (flag_id, state, answer, reasoning, recommended_action, model) "
                "VALUES (%s, 'DONE', 'Yes, escalate.', 'Localization failure at WP05 on a navigation step.', "
                "'Re-record the WP05 route.', 'test-model')", flag_id)
        out1 = _run(BUILD, url, "--no-embed")
        snap1 = _snapshot(url)
        out2 = _run(BUILD, url, "--no-embed")
        snap2 = _snapshot(url)
        yield {"url": url, "docs": n_docs, "out": (out1, out2), "snap": (snap1, snap2), "flag_id": flag_id}
    finally:
        admin.execute("DROP DATABASE IF EXISTS %s WITH (FORCE)" % name)
        admin.close()


def _needs_docs(db):
    if not db["docs"]:
        pytest.skip("no documents in %s (run ingest in the stack first)" % SOURCE_URL.split("@")[-1])


def _nb(db, seeds, hops=2, rels=None, kinds=None, max_nodes=40):
    return _q(db["url"], "SELECT node_id, kind, hop, via_rel, via_node, path, weight, evidence "
                         "FROM graph_neighborhood(%s::text[], %s, %s::text[], %s::text[], %s)",
              seeds, hops, rels, kinds, max_nodes)


# ── build ─────────────────────────────────────────────────────────────────────────────────────────
def test_build_is_idempotent(db):
    s1, s2 = db["snap"]
    assert s1 == s2                                  # same nodes, edges, weights, props; no updated_at bump
    assert "wrote 0 changed nodes (0 stale deleted)" in db["out"][1]
    runs = _q(db["url"], "SELECT status, counts->'nodes', counts->'edges' FROM app.job_runs WHERE job = 'graph' "
                         "ORDER BY run_id")
    assert len(runs) == 2 and {r[0] for r in runs} == {"ok"} and runs[0][1:] == runs[1][1:]


def test_graph_shape(db):
    url = db["url"]
    rels = {r[0] for r in _q(url, "SELECT DISTINCT rel FROM kg_edges")}
    assert rels == CONTRACT_RELS
    kinds = dict(_q(url, "SELECT kind, count(*) FROM kg_nodes GROUP BY 1"))
    assert kinds["action"] == 270 and kinds["location"] == 15 and kinds["asset"] == 20 and kinds["topic"] == 11
    assert kinds["operator_note"] == 1 and kinds["explanation"] == 1
    assert _q(url, "SELECT count(*) FROM kg_edges WHERE NOT (props ? 'source')")[0][0] == 0
    # fk weights are 1/n of the dst's in-edges of that rel; stat common_at weight = n/of with n >= 2
    assert _q(url, "SELECT count(*) FROM kg_edges WHERE props->>'source' = 'fk' "
                   "AND abs(weight - 1.0 / (props->>'of')::int) > 0.001")[0][0] == 0
    bad = _q(url, "SELECT count(*) FROM kg_edges WHERE rel = 'common_at' AND ((props->>'n')::int < 2 "
                  "OR abs(weight - (props->>'n')::real / (props->>'of')::int) > 0.001)")
    assert bad[0][0] == 0
    (n, of, ev), = _q(url, "SELECT (props->>'n')::int, (props->>'of')::int, evidence FROM kg_edges "
                           "WHERE src = 'failure_type:LOCALIZATION_FAILURE' AND rel = 'common_at' AND dst = 'location:WP05'")
    assert (n, of) == (5, 43) and ev in ("event:act-A0061", "A0061")
    assert _q(url, "SELECT count(*) FROM kg_edges WHERE rel = 'fails_more_in' "
                   "AND (props->>'rate')::real >= (props->>'overall')::real")[0][0] == 0
    # triggered edges only point at real policy rules (not laya_verdict / rules_verdict)
    assert _q(url, "SELECT count(*) FROM kg_edges e JOIN kg_nodes n ON n.node_id = e.dst "
                   "WHERE e.rel = 'triggered' AND n.kind <> 'policy_rule'")[0][0] == 0
    note_edges = _q(url, "SELECT rel, dst FROM kg_edges WHERE src LIKE 'operator_note:%%' ORDER BY 1, 2")
    assert ("annotates", "flag:%d" % db["flag_id"]) in note_edges
    assert ("mentions", "asset:PANEL-203") in note_edges and ("mentions", "topic:localization") in note_edges


def test_stale_nodes_are_removed(db):
    url = db["url"]
    _q(url, "INSERT INTO flags (score, severity, title, reasons, triage_event_id, action_id) VALUES "
            "(50, 'HIGH', 'graph test flag', '[{\"rule\": \"battery_low\"}]', 'act-GRAPHTEST', 'A0002')")
    fid = _q(url, "SELECT flag_id FROM flags WHERE triage_event_id = 'act-GRAPHTEST'")[0][0]
    _run(BUILD, url, "--no-embed")
    assert _q(url, "SELECT count(*) FROM kg_edges WHERE src = %s", "flag:%d" % fid)[0][0] == 2   # flags + triggered
    _q(url, "DELETE FROM flags WHERE flag_id = %s", fid)
    out = _run(BUILD, url, "--no-embed")
    assert "(1 stale deleted)" in out and not _q(url, "SELECT 1 FROM kg_nodes WHERE node_id = %s", "flag:%d" % fid)
    assert _snapshot(url)["edges"] == db["snap"][1]["edges"]


NOISY_REASONS = ('[{"rule": "laya_unavailable", "source": "system1", "weight": 0, "detail": "System 1 (Laya) unavailable: '
                 'circuit open after: ConnectError: All connection attempts failed; rules-only verdict"}, '
                 '{"rule": "rules_verdict", "source": "validator", "weight": 0, "detail": "Laya: unavailable \u00b7 '
                 'rules: escalate"}, {"rule": "battery_low", "priority": "P2", "weight": 20, "detail": "battery 22%"}]')


def test_flags_list_policy_rules_only(db):
    """Graph flag summaries and ingest's flag incident docs drop the verdict entries (laya_* / rules_verdict)."""
    import ingest
    url = db["url"]
    _q(url, "INSERT INTO flags (score, severity, title, reasons, triage_event_id, action_id) VALUES "
            "(60, 'HIGH', 'graph test noisy flag', %s::jsonb, 'act-GRAPHNOISE', 'A0061')", NOISY_REASONS)
    fid = _q(url, "SELECT flag_id FROM flags WHERE triage_event_id = 'act-GRAPHNOISE'")[0][0]
    try:
        _run(BUILD, url, "--no-embed")
        (summary,), = _q(url, "SELECT summary FROM kg_nodes WHERE node_id = %s", "flag:%d" % fid)
        assert "Rules: battery_low." in summary
        assert not re.search(r"laya_|rules_verdict|ConnectError", summary), summary
        assert _q(url, "SELECT rel, dst FROM kg_edges WHERE src = %s ORDER BY 1", "flag:%d" % fid) == [
            ("flags", "action:A0061"), ("triggered", "policy_rule:battery_low")]
        with psycopg.connect(url, autocommit=True) as c:
            doc = {d["doc_id"]: d for d in ingest.build_incidents(c)}["incident:flag-%d" % fid]
        assert "Rules fired: battery_low (battery 22%)." in doc["content"]
        assert not re.search(r"laya_|rules_verdict|ConnectError", doc["content"]), doc["content"]
        m = doc["metadata"]
        assert m["rules"] == ["battery_low"] and m["laya_status"] == "unavailable"
        assert m["failure_type"] == "LOCALIZATION_FAILURE" and m["action_type"] == "NAVIGATION"
    finally:
        _q(url, "DELETE FROM flags WHERE flag_id = %s", fid)
        _run(BUILD, url, "--no-embed")


def test_live_rows_survive_a_rebuild_and_a_prune(db):
    """kg rows and documents the live simulation wrote (source = 'live') are never taken over or deleted."""
    import ingest
    url = db["url"]
    _q(url, "INSERT INTO flags (score, severity, title, reasons, triage_event_id, action_id) VALUES "
            "(55, 'HIGH', 'live test flag', '[{\"rule\": \"battery_low\"}]', 'act-LIVE-9-3', 'A0002')")
    fid = _q(url, "SELECT flag_id FROM flags WHERE triage_event_id = 'act-LIVE-9-3'")[0][0]
    node = "flag:%d" % fid
    live = '{"source": "live", "built_by": "live", "run_id": "9", "stop": 3}'
    try:
        _q(url, "INSERT INTO kg_nodes (node_id, kind, key, label, summary, props) VALUES "
                "(%s, 'flag', %s, 'Flag (live)', 'live summary', %s::jsonb)", node, str(fid), live)
        _q(url, "INSERT INTO kg_edges (src, rel, dst, props, evidence) VALUES "
                "(%s, 'triggered', 'policy_rule:battery_low', %s::jsonb, 'event:act-LIVE-9-3')", node, live)
        _run(BUILD, url, "--no-embed")
        assert _q(url, "SELECT summary, props->>'source', props->>'built_by' FROM kg_nodes WHERE node_id = %s",
                  node) == [("live summary", "live", "live")]
        assert _q(url, "SELECT props->>'source', evidence FROM kg_edges WHERE src = %s AND rel = 'triggered'",
                  node) == [("live", "event:act-LIVE-9-3")]
        _run(BUILD, url, "--no-embed")                       # and again: still not deleted, still live
        assert _q(url, "SELECT props->>'source' FROM kg_nodes WHERE node_id = %s", node) == [("live",)]

        _q(url, "INSERT INTO documents (doc_id, kind, title, content, metadata) VALUES "
                "('explanation:live-77', 'explanation', 'live', 'Qwen on a live stop', '{\"source\": \"live\"}'), "
                "('explanation:graphtest-stale', 'explanation', 'stale', 'no longer built', '{}')")
        with psycopg.connect(url, autocommit=True) as c:
            pruned = ingest.prune(c, "explanation", ingest.build_explanations(c))
        assert pruned == 1
        assert [r[0] for r in _q(url, "SELECT doc_id FROM documents WHERE kind = 'explanation'")] == [
            "explanation:live-77"]
    finally:
        _q(url, "DELETE FROM documents WHERE doc_id IN ('explanation:live-77', 'explanation:graphtest-stale')")
        _q(url, "DELETE FROM kg_nodes WHERE node_id = %s", node)
        _q(url, "DELETE FROM flags WHERE flag_id = %s", fid)
        _run(BUILD, url, "--no-embed")


# ── match_kg_nodes_text / match_kg_nodes ──────────────────────────────────────────────────────────
def test_text_match(db):
    url = db["url"]
    rows = _q(url, "SELECT node_id, score FROM match_kg_nodes_text('HX-204')")
    assert rows[0] == ("asset:HX-204", 1.0) and all(s < 1.0 for _, s in rows[1:])
    assert _q(url, "SELECT node_id FROM match_kg_nodes_text('hx204', ARRAY['asset'], 1)") == [("asset:HX-204",)]
    assert _q(url, "SELECT node_id, score FROM match_kg_nodes_text('WP09', NULL, 1)") == [("location:WP09", 1.0)]
    top2 = {r[0] for r in _q(url, "SELECT node_id FROM match_kg_nodes_text('Turbine Hall', NULL, 2)")}
    assert top2 == {"location:WP08", "location:WP09"}
    assert _q(url, "SELECT node_id FROM match_kg_nodes_text('localization failure', NULL, 1)") == [
        ("failure_type:LOCALIZATION_FAILURE",)]
    q = _q(url, "SELECT node_id FROM match_kg_nodes_text('why does HX-204 keep failing in the pipe gallery?')")
    assert {"asset:HX-204", "location:WP05"} <= {r[0] for r in q}
    assert _q(url, "SELECT * FROM match_kg_nodes_text('zzqxj')") == []
    assert _q(url, "SELECT * FROM match_kg_nodes_text(NULL)") == [] and _q(url, "SELECT * FROM match_kg_nodes_text('  ')") == []
    scores = [r[0] for r in _q(url, "SELECT score FROM match_kg_nodes_text('pump', NULL, 20)")]
    assert scores == sorted(scores, reverse=True) and all(0.3 <= s <= 1.0 for s in scores)


def test_vector_match(db):
    _needs_docs(db)
    url = db["url"]
    rows = _q(url, "SELECT m.node_id, m.similarity FROM kg_nodes n, "
                   "match_kg_nodes(n.embedding::text::vector, ARRAY['manual'], 3) m WHERE n.node_id = 'manual:p53-1'")
    assert rows[0][0] == "manual:p53-1" and rows[0][1] > 0.999 and len(rows) == 3
    none = _q(url, "SELECT count(*) FROM kg_nodes n, match_kg_nodes(n.embedding::text::vector, NULL, 5, 1.5) m "
                   "WHERE n.node_id = 'manual:p53-1'")
    assert none == [(0,)]


# ── graph_neighborhood ────────────────────────────────────────────────────────────────────────────
def _by_node(rows):
    return {r[0]: r for r in rows}


def test_neighborhood_location(db):
    rows = _nb(db, ["location:WP09"], 2)
    got = _by_node(rows)
    assert got["environment:HIGH_NOISE"][2] == 1 and len(rows) == 40 and "location:WP09" not in got
    hops = [r[2] for r in rows]
    assert hops == sorted(hops)
    for h in set(hops):                                   # weight descending within a hop
        w = [r[6] for r in rows if r[2] == h]
        assert w == sorted(w, reverse=True)
    # The kit's localization failures are at WP05 (5 of 43 actions), not WP09.
    _needs_docs(db)
    got = _by_node(_nb(db, ["location:WP05"], 2, max_nodes=500))
    assert got["environment:HIGH_NOISE"][2] == 1 and got["failure_type:LOCALIZATION_FAILURE"][2] == 1
    assert got["topic:localization"][2] == 2
    manual = [r for r in got.values() if r[1] == "manual" and "failure_type:LOCALIZATION_FAILURE" in r[5]]
    assert manual and all(r[2] == 2 for r in manual)
    got3 = _nb(db, ["location:WP05"], 3, kinds=["manual"], max_nodes=500)
    assert any("topic:localization" in r[5] for r in got3)


def test_flag_action_reaches_manual(db):
    _needs_docs(db)
    rows = _nb(db, ["action:A0061"], 2, max_nodes=200)
    manual = [r for r in rows if r[1] == "manual"]
    assert manual and manual[0][2] == 2
    path = manual[0][5]
    assert path[0] == "action:A0061" and path[-1] == manual[0][0] and len(path) == 5   # node, step, node, step, node


def test_cycles_and_hop_cap(db):
    url = db["url"]
    t = ["topic:__t%d" % i for i in range(7)]
    try:
        _q(url, "INSERT INTO kg_nodes (node_id, kind, key, label, summary) "
                "SELECT x, 'topic', x, x, 'test' FROM unnest(%s::text[]) x", t)
        edges = [(t[0], "r", t[1]), (t[1], "r", t[2]), (t[2], "r", t[0]), (t[0], "r", t[0]), (t[1], "q", t[0]),
                 (t[2], "r", t[3]), (t[3], "r", t[4]), (t[4], "r", t[5]), (t[5], "r", t[6])]
        for s, r, d in edges:
            _q(url, "INSERT INTO kg_edges (src, rel, dst, props) VALUES (%s, %s, %s, '{\"source\": \"test\"}')", s, r, d)
        rows = _nb(db, [t[0]], 10, max_nodes=100)          # 10 -> capped at 3
        got = _by_node(rows)
        assert len(rows) == len(got)                        # one row per node, despite the cycle and the self-loop
        assert t[0] not in got
        assert (got[t[1]][2], got[t[2]][2], got[t[3]][2], got[t[4]][2]) == (1, 1, 2, 3)
        assert t[5] not in got and max(r[2] for r in rows) == 3
        assert got[t[2]][5] == [t[0], "<-r-", t[2]] and got[t[2]][3:5] == ("r", t[0])
        assert _nb(db, [t[0]], 0) == []
        assert {r[0] for r in _q(url, "SELECT node_id FROM graph_neighborhood(%s::text[])", [t[0]])} == {t[1], t[2], t[3]}
        assert {r[0] for r in _nb(db, [t[0]], 3, rels=["q"])} == {t[1]}
        assert _nb(db, [t[0]], 3, kinds=["manual"]) == []
        assert len(_nb(db, [t[0]], 3, max_nodes=2)) == 2
        assert _nb(db, ["topic:__nope"], 2) == [] and _nb(db, None, 2) == []
        both = _by_node(_nb(db, [t[0], t[1]], 1))           # a seed reached from another seed is returned
        assert both[t[1]][2] == 1 and both[t[0]][2] == 1
    finally:
        _q(url, "DELETE FROM kg_nodes WHERE node_id = ANY(%s)", t)


def test_timing(db):
    url = db["url"]
    with psycopg.connect(url, autocommit=True) as c:
        for sql, budget_ms in [("SELECT * FROM graph_neighborhood(ARRAY['location:WP09'], 2)", 100),
                               ("SELECT * FROM graph_neighborhood(ARRAY['action:A0061','asset:PANEL-203','location:WP05',"
                                "'failure_type:LOCALIZATION_FAILURE','policy_rule:action_failed'], 2)", 200),
                               ("SELECT * FROM match_kg_nodes_text('HX-204')", 100)]:
            c.execute(sql).fetchall()
            t0 = time.perf_counter()
            c.execute(sql).fetchall()
            assert (time.perf_counter() - t0) * 1000 < budget_ms, sql


# ── grants ────────────────────────────────────────────────────────────────────────────────────────
def test_mcp_reader_grants(db):
    url = db["url"]
    if not _q(url, "SELECT 1 FROM pg_roles WHERE rolname = 'spot_mcp_reader'"):
        pytest.skip("spot_mcp_reader missing")
    role = "graphtest_" + uuid.uuid4().hex[:6]
    _q(url, "CREATE ROLE %s LOGIN PASSWORD 'graphtest' IN ROLE spot_mcp_reader" % role)
    try:
        ro = re.sub(r"//[^@]+@", "//%s:graphtest@" % role, url)
        with psycopg.connect(ro, autocommit=True) as c:
            assert c.execute("SELECT count(*) FROM kg_nodes").fetchone()[0] > 0
            assert c.execute("SELECT count(*) FROM kg_edges").fetchone()[0] > 0
            assert c.execute("SELECT count(*) FROM match_kg_nodes_text('HX-204')").fetchone()[0] > 0
            assert c.execute("SELECT count(*) FROM graph_neighborhood(ARRAY['location:WP09'])").fetchone()[0] > 0
            c.execute("SELECT count(*) FROM match_kg_nodes(array_fill(0.1, ARRAY[%s])::vector)" % (
                os.environ.get("EMBED_DIM", "1024"),)).fetchone()
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("DELETE FROM kg_edges WHERE false")
    finally:
        _q(url, "DROP ROLE %s" % role)


# ── optional: the embedding path (GRAPH_TEST_EMBED=1, needs EMBED_URL) ────────────────────────────
@pytest.mark.skipif(os.environ.get("GRAPH_TEST_EMBED") != "1", reason="set GRAPH_TEST_EMBED=1 (needs EMBED_URL)")
def test_embedding_is_incremental(db):
    url = db["url"]
    out1 = _run(BUILD, url)
    n = int(re.search(r"embedded (\d+) summaries", out1).group(1))
    assert n > 0 and "0 pending" in out1
    out2 = _run(BUILD, url)
    assert "embedded 0 summaries (0 pending)" in out2 and "wrote 0 changed nodes" in out2
    assert _q(url, "SELECT count(*) FROM kg_nodes WHERE embedding IS NULL")[0][0] == 0
