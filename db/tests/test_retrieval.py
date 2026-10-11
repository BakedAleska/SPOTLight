"""Embedding client + ingest builders (unit) and the retrieval regression set (db/tests/retrieval_testset.json).

- Unit tests (always run, no services): amd/embed/client.py retries, vector checks, defaults and the health check;
  ingest.py event titles and manual chunks.
- Retrieval tests: DATABASE_URL (a database after `ingest.py` + `graph_build.py`) and EMBED_URL (the same embedding
  model). Every statement runs in its own BEGIN READ ONLY transaction (psycopg read_only; no session SET, which a
  transaction pooler would leak to other clients), so it is safe against Neon. Skipped without both, when
  the embedder's /health is not ok, or when `documents` is empty. Cases marked "stretch" are xfail (non-strict).

    DATABASE_URL=... EMBED_URL=http://localhost:18081 python -m pytest -q db/tests/test_retrieval.py
"""
from __future__ import annotations

import ast
import json
import os
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "amd" / "embed"))
sys.path.insert(0, str(ROOT / "db"))
httpx = pytest.importorskip("httpx")
import client as C  # noqa: E402

TESTSET = json.loads((Path(__file__).with_name("retrieval_testset.json")).read_text())


# ── unit: embedding client ────────────────────────────────────────────────────────────────────────
def _default_prefix(path: Path) -> str:
    """DEFAULT_QUERY_PREFIX as written in another service's config.py (parsed, not imported)."""
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "DEFAULT_QUERY_PREFIX" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("DEFAULT_QUERY_PREFIX not found in %s" % path)


@pytest.fixture
def clean_env(monkeypatch):
    for k in ("EMBED_MODEL", "EMBED_DIM", "EMBED_QUERY_PREFIX", "EMBED_DOC_PREFIX", "EMBED_CLIENT_BATCH", "EMBED_WAIT_S"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def _client(handler, **kw) -> "C.EmbedClient":
    c = C.EmbedClient(url="http://embed.test", dim=kw.pop("dim", 3), **kw)
    c.http = httpx.Client(transport=httpx.MockTransport(handler))
    c.backoff = (0, 0, 0, 0)
    return c


def _ok(request, dim=3, n=None, model="Qwen/Qwen3-Embedding-0.6B"):
    inputs = json.loads(request.content)["input"]
    n = len(inputs) if n is None else n
    return httpx.Response(200, json={"model": model, "data": [{"index": i, "embedding": [0.1] * dim} for i in range(n)],
                                     "usage": {"prompt_tokens": 7 * len(inputs)},
                                     "timing": {"ms": 5, "device": "cuda", "gpu": "W7900D"}})


def test_defaults_match_backend_and_mcp(clean_env):
    c = C.EmbedClient()
    assert c.model == "Qwen/Qwen3-Embedding-0.6B" and c.batch_size == 16 and c.doc_prefix == ""
    assert c.query_prefix == C.DEFAULT_QUERY_PREFIX and c.query_prefix.endswith("\nQuery:")
    assert C.DEFAULT_QUERY_PREFIX == _default_prefix(ROOT / "amd" / "mcp" / "spot_mcp" / "config.py")
    assert C.DEFAULT_QUERY_PREFIX == _default_prefix(ROOT / "backend" / "app" / "config.py")
    clean_env.setenv("EMBED_QUERY_PREFIX", "")            # set but empty -> no prefix
    clean_env.setenv("EMBED_CLIENT_BATCH", "4")
    clean_env.setenv("EMBED_MODEL", "")
    c = C.EmbedClient()
    assert c.query_prefix == "" and c.batch_size == 4 and c.model == ""
    clean_env.setenv("EMBED_QUERY_PREFIX", "query: \\n")
    assert C.EmbedClient().query_prefix == "query: \n"


def test_check_health(clean_env):
    c = C.EmbedClient(dim=1024)
    assert c.check_health({"status": "ok", "model": "Qwen/Qwen3-Embedding-0.6B", "dim": 1024})
    with pytest.raises(SystemExit, match="runs Snowflake"):
        c.check_health({"model": "Snowflake/snowflake-arctic-embed-l-v2.0", "dim": 1024})
    with pytest.raises(SystemExit, match="EMBED_DIM=1024"):
        c.check_health({"model": "Qwen/Qwen3-Embedding-0.6B", "dim": 768})
    c = C.EmbedClient(model="", dim=1024)                   # EMBED_MODEL set but empty: adopt the server's
    c.check_health({"model": "Qwen/Qwen3-Embedding-0.6B", "dim": 1024})
    assert c.model == "Qwen/Qwen3-Embedding-0.6B"
    with pytest.raises(SystemExit, match="reports no model"):
        C.EmbedClient(model="", dim=1024).check_health({"status": "ok"})


def test_embed_retries_transient_failures_and_logs_the_path(clean_env):
    seen, calls = [], []

    def handler(request):
        seen.append(request.url.path)
        if len(seen) == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=request)
        if len(seen) in (2, 3):
            return httpx.Response(502 if len(seen) == 2 else 429, text="bridge")
        return _ok(request)

    c = _client(handler, batch_size=2, on_call=calls.append)
    vecs = c.embed(["a", "b", "c"], kind="doc")
    assert len(vecs) == 3 and len(seen) == 5               # batch 1: 3 failures + success; batch 2: success
    assert [x["endpoint"] for x in calls] == ["/v1/embeddings", "/v1/embeddings"] and calls[0]["n"] == 2
    assert calls[0]["gpu"] == "W7900D" and calls[0]["input_tokens"] == 14


def test_embed_gives_up_after_five_attempts(clean_env):
    n = []

    def unavailable(request):
        n.append(1)
        return httpx.Response(503, text="down")

    with pytest.raises(RuntimeError, match="503"):
        _client(unavailable).embed(["a"])
    assert len(n) == 5

    def drops(request):
        n.append(1)
        raise httpx.ConnectError("refused", request=request)

    n.clear()
    with pytest.raises(httpx.ConnectError):
        _client(drops).embed(["a"])
    assert len(n) == 5

    def bad_request(request):                              # not transient: no retry
        n.append(1)
        return httpx.Response(400, text="too long")

    n.clear()
    with pytest.raises(RuntimeError, match="400"):
        _client(bad_request).embed(["a"])
    assert len(n) == 1


def test_embed_checks_count_and_every_width(clean_env):
    with pytest.raises(ValueError, match="2 vectors for 3 inputs"):
        _client(lambda r: _ok(r, n=2)).embed(["a", "b", "c"])

    def ragged(request):
        body = _ok(request).json()
        body["data"][1]["embedding"] = [0.1] * 4
        return httpx.Response(200, json=body)

    with pytest.raises(ValueError, match="4 dims but EMBED_DIM=3"):
        _client(ragged).embed(["a", "b", "c"])


def test_query_prefix_is_prepended(clean_env):
    got = []

    def handler(request):
        got.extend(json.loads(request.content)["input"])
        return _ok(request)

    _client(handler).embed(["pump too hot"], kind="query")
    _client(handler, doc_prefix="").embed(["doc text"], kind="doc")
    assert got == [C.DEFAULT_QUERY_PREFIX + "pump too hot", "doc text"]


def test_wait_ready_fails_fast_on_unknown_host(clean_env):
    def handler(request):
        try:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        except socket.gaierror as e:
            raise httpx.ConnectError(str(e), request=request) from e

    c = _client(handler)
    with pytest.raises(RuntimeError, match="does not resolve"):
        c.wait_ready(timeout_s=60)
    clean_env.setenv("EMBED_WAIT_S", "0")                   # refused (not DNS): polls until EMBED_WAIT_S

    def refused(request):
        raise httpx.ConnectError("Connection refused", request=request)

    with pytest.raises(TimeoutError, match="EMBED_WAIT_S"):
        _client(refused).wait_ready()


# ── unit: ingest builders ─────────────────────────────────────────────────────────────────────────
def test_event_titles_carry_the_action_id():
    pytest.importorskip("psycopg")
    import ingest
    docs = {d["doc_id"]: d for d in ingest.build_events()}
    assert docs["event:act-A0061"]["title"].startswith("M") and "(A0061)" in docs["event:act-A0061"]["title"]
    actions = [d for d in docs.values() if d["metadata"]["event_kind"] == "action"]
    assert actions and all("(%s)" % d["action_id"] in d["title"] for d in actions)


def test_manual_chunks_strip_the_footer_and_cite_printed_pages():
    pytest.importorskip("psycopg")
    pytest.importorskip("pypdf")
    import ingest
    if not list(ingest.spotdb.MANUAL_DIR.glob("*.pdf")):
        pytest.skip("Spot User Guide PDFs missing")
    docs = {d["doc_id"]: d for d in ingest.build_manual(ingest.spotdb.MANUAL_DIR)}
    assert len(docs) == 85                                 # same chunks and doc_ids as before the footer fix
    assert not [k for k, d in docs.items() if ingest.FOOTER_RX.match(d["content"])]
    p52 = docs["manual:p52-1"]
    assert p52["content"].startswith("TROUBLESHOOTING AUTOWALK")
    assert p52["title"] == "Spot User Guide p.44 (PDF p.52): TROUBLESHOOTING AUTOWALK"
    assert p52["metadata"]["page"] == 52 and p52["metadata"]["printed_page"] == 44
    assert docs["manual:p52-2"]["title"].endswith("(part 2)")
    assert docs["manual:p3-1"]["metadata"]["printed_page"] is None


# ── retrieval regression set (needs DATABASE_URL + EMBED_URL) ─────────────────────────────────────
@pytest.fixture(scope="module")
def live():
    url, embed_url = os.environ.get("DATABASE_URL"), os.environ.get("EMBED_URL")
    if not (url and embed_url):
        pytest.skip("set DATABASE_URL and EMBED_URL to run the retrieval set")
    psycopg = pytest.importorskip("psycopg")
    import spotdb
    client = C.EmbedClient(url=embed_url)
    try:
        health = client.health()
    except httpx.HTTPError as e:
        pytest.skip("embedding service not reachable: %s" % type(e).__name__)
    if health.get("status") != "ok":
        pytest.skip("embedding service not ready: %s" % health.get("status"))
    client.check_health(health)
    # Never writes, even when pointed at Neon: every statement runs in its own BEGIN READ ONLY transaction (see
    # _fetch). No session-level SET: through a transaction pooler (Neon's -pooler host) a SET stays on the pooled
    # server connection and would make other clients' transactions read-only.
    conn = spotdb.connect(url)
    conn.read_only = True
    try:
        assert _fetch(conn, "SHOW transaction_read_only")[0][0] == "on"
        if not _fetch(conn, "SELECT count(embedding) FROM documents")[0][0]:
            pytest.skip("documents has no vectors: run amd/embed/ingest.py first")
        cases = TESTSET["documents"]
        vecs = client.embed([t["query"] for t in cases], kind="query")
        graph_q = [g for g in TESTSET["graph"] if "sql_vec" in g]
        gvecs = client.embed([g["query"] for g in graph_q], kind="query") if graph_q else []
        yield {"conn": conn, "vec": {t["id"]: C.to_pgvector(v) for t, v in zip(cases, vecs)},
               "gvec": {g["id"]: C.to_pgvector(v) for g, v in zip(graph_q, gvecs)}, "psycopg": psycopg}
    finally:
        conn.close()


def _fetch(conn, sql: str, args=None, cols: bool = False):
    """One read in its own read-only transaction (conn.read_only: BEGIN READ ONLY), rolled back at once."""
    try:
        cur = conn.execute(sql, args or None)
        rows = cur.fetchall()
        return ([c.name for c in cur.description], rows) if cols else rows
    finally:
        conn.rollback()


def _match(row: dict, cond: dict) -> bool:
    for k, v in cond.items():
        val = (row["metadata"] or {}).get(k[5:]) if k.startswith("meta.") else row.get(k)
        if val != v:
            return False
    return True


def _pred(row: dict, p: dict) -> bool:
    return any(_match(row, c) for c in p["any"]) if "any" in p else all(_match(row, c) for c in p["all"])


def _search(conn, t: dict, vec: str):
    if t["mode"] == "hybrid":
        fn, args = "match_documents_hybrid(%s::vector, %s, %s::text[], match_count => %s)", (
            vec, t["query"], t["kinds"], t["k"])
    else:
        fn, args = "match_documents(%s::vector, %s::text[], match_count => %s)", (vec, t["kinds"], t["k"])
    # anomaly_type is not an OUT column of the search functions: join it, keeping the function's rank order
    cols, rows = _fetch(conn, "SELECT m.doc_id, m.kind, m.asset_id, m.location_id, d.anomaly_type, m.metadata, "
                              "m.similarity FROM %s WITH ORDINALITY AS m LEFT JOIN documents d ON d.doc_id = m.doc_id "
                              "ORDER BY m.ordinality" % fn, args, cols=True)
    return [dict(zip(cols, r)) for r in rows]


def _check(t: dict, rows: list) -> list:
    """Failed expectations (empty = pass)."""
    bad, ids = [], [r["doc_id"] for r in rows]
    if "expect_any" in t:
        need = 1 if "expect_pred" in t else t.get("min_hits", 1)
        hits = [i for i in ids if i in t["expect_any"]]
        if len(hits) < need:
            bad.append("expected %d of %s, got %s" % (need, t["expect_any"], hits))
    if "expect_pred" in t:
        hits = [r["doc_id"] for r in rows if _pred(r, t["expect_pred"])]
        if len(hits) < t.get("min_hits", 1):
            bad.append("expected %d rows matching %s, got %s" % (t.get("min_hits", 1), t["expect_pred"], hits))
        if t.get("first_must_match") and not (rows and _pred(rows[0], t["expect_pred"])):
            bad.append("first row %s does not match %s" % (ids[:1], t["expect_pred"]))
    if "expect_top_sim_below" in t and rows and max(r["similarity"] for r in rows) >= t["expect_top_sim_below"]:
        bad.append("top similarity %.3f >= %s" % (max(r["similarity"] for r in rows), t["expect_top_sim_below"]))
    return bad


def _param(t: dict):
    marks = [pytest.mark.xfail(reason="stretch case (reported, not gating)", strict=False)] if t.get("stretch") else []
    return pytest.param(t, id=t["id"], marks=marks)


@pytest.mark.parametrize("case", [_param(t) for t in TESTSET["documents"]])
def test_document_retrieval(live, case):
    rows = _search(live["conn"], case, live["vec"][case["id"]])
    bad = _check(case, rows)
    assert not bad, "%s %r: %s | got %s" % (case["id"], case["query"], "; ".join(bad),
                                           [(r["doc_id"], round(r["similarity"], 3)) for r in rows])


@pytest.mark.parametrize("case", [pytest.param(g, id=g["id"]) for g in TESTSET["graph"]])
def test_graph_retrieval(live, case):
    conn = live["conn"]
    if not (_fetch(conn, "SELECT to_regclass('public.kg_nodes') IS NOT NULL")[0][0]
            and _fetch(conn, "SELECT EXISTS (SELECT 1 FROM kg_nodes)")[0][0]):
        pytest.skip("kg_nodes is empty: run db/graph_build.py first")
    if "sql_vec" in case:
        rows = [r[0] for r in _fetch(conn, case["sql_vec"], (live["gvec"][case["id"]],))]
    else:
        rows = [r[0] for r in _fetch(conn, case["sql"])]
    if "expect_first" in case:
        assert rows[:1] == [case["expect_first"]], rows
    elif "expect_set" in case:
        assert set(rows) == set(case["expect_set"]), rows
    else:
        assert all(x in rows for x in case["expect_contains"]), rows
