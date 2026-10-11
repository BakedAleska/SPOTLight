"""Chat retrieval similarity floor (vector-store audit VS-06; docs/chat-contract.md §2 step 4).

Measured with Qwen3-Embedding-0.6B and the query instruction: off-topic questions score 0.40-0.54 against the generic
manual chunks, relevant hits 0.54-0.83. Before the fix, chat kept vector-only graph nodes down to cosine 0.35 and put
no floor at all on hybrid documents, so "best recipe for chocolate cake" came back with k manual passages to cite.

- Unit (always run): both floors are 0.50; the Retriever drops vector-only graph nodes below it; the hybrid query
  passes the floor and over-fetches.
- DB (`integration`): ChatRepo.search_documents on the real match_documents_hybrid, in a throwaway database
  spot_floortest_<hex> (`db/setup.py migrate`, no seed) holding synthetic documents whose cosine to each query vector
  is set exactly, so no embedding service is needed. Postgres + pgvector at FLOOR_TEST_ADMIN_URL, else
  CHAT_TEST_ADMIN_URL, else postgres:$POSTGRES_PASSWORD@localhost:$DB_PORT (5433)/postgres. Skips when it is not
  reachable or SKIP_INTEGRATION=1.
"""
from __future__ import annotations

import math
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from app.db import chat as C
from app.db.chat import CHAT_MIN_DOC_SIMILARITY, ChatRepo
from app.db.pool import Database
from app.services.chat import retrieval as R
from app.services.chat.retrieval import MIN_VECTOR_SCORE, Retriever

from .fakes_chat import FakeChatRepo

ROOT = Path(__file__).resolve().parents[2]
SETUP = ROOT / "db" / "setup.py"
DOC_KEYS = {"doc_id", "kind", "title", "content", "asset_id", "location_id", "mission_id", "action_id", "ts", "score"}
OFF_TOPIC = "best recipe for chocolate cake with buttercream frosting"       # retrieval test set N01
RELEVANT = "robot lost localization near the stairs"


# ── unit ────────────────────────────────────────────────────────────────────────────────────────
def test_floors_sit_above_the_measured_noise():
    assert MIN_VECTOR_SCORE == 0.50 and CHAT_MIN_DOC_SIMILARITY == 0.50
    assert R._useful_node({"match": "vector", "score": 0.537}) is True      # the worst measured off-topic hit
    assert R._useful_node({"match": "vector", "score": 0.444}) is False     # N01's top hit
    assert R._useful_node({"match": "vector", "score": 0.40}) is False      # was kept at 0.35
    assert R._useful_node({"match": "text", "score": 0.31}) is True         # text matches are always kept
    assert R._useful_node({"match": "both", "score": 0.31}) is True


async def test_retriever_drops_vector_only_graph_nodes_below_the_floor():
    repo = FakeChatRepo()
    repo.nodes = [
        {"node_id": "topic:docking", "kind": "topic", "label": "docking", "summary": "Docking and charging.",
         "doc_id": None, "score": 0.51, "match": "vector"},
        {"node_id": "topic:spotcheck", "kind": "topic", "label": "SpotCheck", "summary": "Generic manual topic.",
         "doc_id": None, "score": 0.44, "match": "vector"},
        {"node_id": "failure_type:LOCALIZATION_FAILURE", "kind": "failure_type", "label": "Localization failure",
         "summary": "The robot lost localization.", "doc_id": None, "score": 0.31, "match": "text"},
    ]

    class Embed:
        async def embed_query(self, q, request_id=None, timeout_s=None):
            return [0.0, 1.0, 0.0, 0.0]

    r = await Retriever(repo, Embed()).retrieve("how do I dock and charge?")
    ids = {s["id"] for s in r.sources}
    assert r.mode == "hybrid"
    assert {"topic:docking", "failure_type:LOCALIZATION_FAILURE"} <= ids
    assert "topic:spotcheck" not in ids
    assert "topic:spotcheck" not in r.graph_seeds and "topic:docking" in r.graph_seeds


class _RecordingDb:
    def __init__(self):
        self.calls = []

    async def fetch(self, sql, params=()):
        self.calls.append((sql, params))
        return []


async def test_hybrid_query_passes_the_floor_and_overfetches():
    db = _RecordingDb()
    rows, mode = await ChatRepo(db).search_documents("HX-204", "[1,0,0]", k=6, kinds=["event"], mission_id="m014")
    assert (rows, mode) == ([], "hybrid")
    (sql, params), = db.calls
    assert "match_documents_hybrid" in sql and "h.text_match OR h.similarity >=" in sql
    assert "similarity" not in sql.split(" FROM ")[0] and "text_match" not in sql.split(" FROM ")[0]   # shape kept
    # a mission filter already scopes the rows: the lower mission floor (RE-06)
    assert tuple(params) == ("[1,0,0]", "HX-204", ["event"], "M014", 6 * C.DOC_OVERFETCH,
                             C.CHAT_MISSION_MIN_DOC_SIMILARITY, 6)
    assert C.CHAT_MISSION_MIN_DOC_SIMILARITY == 0.40


async def test_hybrid_query_without_a_mission_uses_the_full_floor():
    db = _RecordingDb()
    await ChatRepo(db).search_documents("HX-204", "[1,0,0]", k=6)
    (sql, params), = db.calls
    assert tuple(params) == ("[1,0,0]", "HX-204", None, None, 6 * C.DOC_OVERFETCH, CHAT_MIN_DOC_SIMILARITY, 6)


async def test_fulltext_path_has_no_floor():
    db = _RecordingDb()
    await ChatRepo(db).search_documents(OFF_TOPIC, None)
    assert all("similarity" not in sql for sql, _ in db.calls)


# ── DB: the real match_documents_hybrid ─────────────────────────────────────────────────────────
ADMIN_URL = (os.environ.get("FLOOR_TEST_ADMIN_URL") or os.environ.get("CHAT_TEST_ADMIN_URL")
             or "postgresql://postgres:%s@localhost:%s/postgres" % (os.environ.get("POSTGRES_PASSWORD", "postgres"),
                                                                   os.environ.get("DB_PORT", "5433")))
# Query axes: each query vector is a unit axis; a document's cosine to a query is its component on that axis.
Q_OFF, Q_ID, Q_REL = 0, 1, 2
# doc_id, kind, asset_id, mission_id, title, content, {query axis: cosine} (None = no embedding)
DOCS = [
    ("manual:p61-1", "manual", None, None, "SpotCheck",
     "Best practice: run SpotCheck before the first mission of the day to calibrate the cameras and legs.",
     {Q_OFF: 0.444, Q_REL: 0.45}),
    ("manual:p6-1", "manual", None, None, "Safety", "Keep a clear area around Spot while it powers on.",
     {Q_OFF: 0.402, Q_REL: 0.30}),
    ("event:act-floor1", "event", "HX-204", "M014", "M014 step 9: thermal scan HX-204 at WP05, FAILED",
     "Mission M014 step 9: thermal scan of HX-204 at WP05 failed after the climb.", {Q_ID: 0.30}),
    ("incident:floor2", "incident", "HX-204", "M007", "Gauge out of range on HX-204",
     "During mission M007 the pressure gauge on HX-204 read above its safe band.", {Q_ID: 0.20}),
    ("event:act-floor3", "event", "HX-201", "M007", "M007 step 2: thermal scan HX-201 at WP05",
     "Mission M007 step 2: thermal scan of HX-201 at WP05 succeeded.", {Q_ID: 0.45}),
    ("operator_note:floor-noemb", "operator_note", "HX-204", None, "Operator note",
     "HX-204 localization drift seen near the stairs.", None),
    ("event:act-floor-loc1", "event", None, None, "Navigation to WP05 failed",
     "Spot could not find its fiducial on the landing and aborted the navigation to WP05.", {Q_REL: 0.62}),
    ("event:act-floor-loc2", "event", None, None, "Navigation to WP09 failed",
     "Spot could not re-anchor at WP09 after a fiducial was blocked.", {Q_REL: 0.55}),
    # Lexical noise for RELEVANT: most (not all) of its terms, so a high RRF rank but no text_match and a low cosine.
    ("incident:floor-noise1", "incident", None, None, "Robot lost near stairs",
     "The robot lost its way near the stairs, twice; the robot was lost near the stairs again later.", {Q_REL: 0.10}),
    ("incident:floor-noise2", "incident", None, None, "Robot near stairs",
     "The robot stopped near the stairs and was lost for a minute.", {Q_REL: 0.08}),
]


def _vec(dim, comps, slot):
    """Unit vector with the given components on the query axes, the rest on its own axis (orthogonal to the rest)."""
    v = [0.0] * dim
    for axis, c in comps.items():
        v[axis] = c
    v[10 + slot] = math.sqrt(max(0.0, 1.0 - sum(c * c for c in comps.values())))
    return "[" + ",".join("%.6f" % x for x in v) + "]"


def _axis(dim, axis):
    return "[" + ",".join("1" if i == axis else "0" for i in range(dim)) + "]"


@pytest.fixture(scope="module")
def floor_db():
    if os.environ.get("SKIP_INTEGRATION") == "1":
        pytest.skip("SKIP_INTEGRATION=1")
    psycopg = pytest.importorskip("psycopg")
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3)
    except psycopg.Error as exc:
        pytest.skip("no Postgres at %s: %s" % (ADMIN_URL.split("@")[-1], exc))
    name = "spot_floortest_" + uuid.uuid4().hex[:8]
    admin.execute("CREATE DATABASE %s" % name)
    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + name
    try:
        env = {k: v for k, v in os.environ.items() if k != "LOGS_DATABASE_URL"}
        env["DATABASE_URL"] = url
        r = subprocess.run([sys.executable, str(SETUP), "migrate"], env=env, capture_output=True, text=True,
                           timeout=300)
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
        with psycopg.connect(url, autocommit=True) as c:
            dim = c.execute("SELECT atttypmod FROM pg_attribute WHERE attrelid = 'documents'::regclass "
                            "AND attname = 'embedding'").fetchone()[0]
            assert dim >= 10 + len(DOCS)
            for slot, (doc_id, kind, asset, mission, title, content, comps) in enumerate(DOCS, start=1):
                c.execute("INSERT INTO documents (doc_id, kind, asset_id, mission_id, title, content, embedding, "
                          "embed_model) VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s)",
                          (doc_id, kind, asset, mission, title, content,
                           None if comps is None else _vec(dim, comps, slot), None if comps is None else "synthetic"))
        yield {"url": url, "q": {a: _axis(dim, a) for a in (Q_OFF, Q_ID, Q_REL)}}
    finally:
        admin.execute("DROP DATABASE IF EXISTS %s WITH (FORCE)" % name)
        admin.close()


@pytest.fixture
async def repo(floor_db):
    db = Database(floor_db["url"], name="floortest", pool_max=2, connect_timeout_s=5)
    await db.open()
    try:
        yield ChatRepo(db)
    finally:
        await db.close()


def _ids(rows):
    return [r["doc_id"] for r in rows]


@pytest.mark.integration
async def test_off_topic_question_returns_no_documents(floor_db, repo):
    vec = floor_db["q"][Q_OFF]
    raw = await repo.db.fetch("SELECT doc_id, similarity::float8 AS sim, text_match FROM "
                              "match_documents_hybrid(%s::vector, %s, NULL, NULL, NULL, NULL, 8)", (vec, OFF_TOPIC))
    # Without the floor the function hands back the generic manual chunks (and 'best' matches one lexically) ...
    assert {"manual:p61-1", "manual:p6-1"} <= set(_ids(raw))
    assert max(r["sim"] for r in raw if r["sim"] is not None) < CHAT_MIN_DOC_SIMILARITY
    assert not any(r["text_match"] for r in raw)
    # ... with it, chat gets nothing to cite.
    assert await repo.search_documents(OFF_TOPIC, vec) == ([], "hybrid")
    assert await repo.search_documents(OFF_TOPIC, vec, k=50) == ([], "hybrid")


@pytest.mark.integration
async def test_exact_id_query_keeps_id_matching_documents_through_text_match(floor_db, repo):
    rows, mode = await repo.search_documents("HX-204", floor_db["q"][Q_ID])
    assert mode == "hybrid" and all(set(r) == DOC_KEYS and isinstance(r["score"], float) for r in rows)
    # Every HX-204 document is kept although none reaches the floor by cosine (one has no embedding at all) ...
    assert set(_ids(rows)) == {"event:act-floor1", "incident:floor2", "operator_note:floor-noemb"}
    assert all("HX-204" in r["content"] for r in rows)
    # ... while HX-201 (closer by cosine, 0.45, and a lexical hit on 'hx') is not.
    assert "event:act-floor3" not in _ids(rows)
    assert [r["score"] for r in rows] == sorted((r["score"] for r in rows), reverse=True)
    rows, _ = await repo.search_documents("HX-204", floor_db["q"][Q_ID], mission_id="M014")       # chat.3 filter
    assert _ids(rows) == ["event:act-floor1"]


@pytest.mark.integration
async def test_id_inside_a_question_keeps_its_documents(floor_db, repo):
    # A chat question names the id among other words, which no document contains ("went", "wrong", "week"), so
    # "every query term" is false. text_match still holds through the id tokens alone (004's tsq_ids, SC-3); without
    # them the floor would drop these sub-0.50 documents.
    q = "What went wrong with HX-204 last week?"
    raw = await repo.db.fetch("SELECT doc_id, to_tsvector('english', content) @@ plainto_tsquery('english', %s) AS every "
                              "FROM documents WHERE asset_id = 'HX-204'", (q,))
    assert raw and not any(r["every"] for r in raw)
    rows, _ = await repo.search_documents(q, floor_db["q"][Q_ID])
    assert set(_ids(rows)) == {"event:act-floor1", "incident:floor2", "operator_note:floor-noemb"}


@pytest.mark.integration
async def test_relevant_question_keeps_documents_above_the_floor_only(floor_db, repo):
    vec = floor_db["q"][Q_REL]
    rows, mode = await repo.search_documents(RELEVANT, vec)
    assert mode == "hybrid" and _ids(rows) == ["event:act-floor-loc1", "event:act-floor-loc2"]
    # Pure semantic (no text, so no text_match) keeps the same two.
    assert _ids((await repo.search_documents("", vec))[0]) == ["event:act-floor-loc1", "event:act-floor-loc2"]


@pytest.mark.integration
async def test_floor_overfetch_still_fills_k(floor_db, repo):
    vec = floor_db["q"][Q_REL]
    top = await repo.db.fetch("SELECT doc_id FROM match_documents_hybrid(%s::vector, %s, NULL, NULL, NULL, NULL, 2)",
                              (vec, RELEVANT))
    # The two best RRF rows are lexical noise below the floor; asking for exactly k would leave nothing.
    assert set(_ids(top)) == {"incident:floor-noise1", "incident:floor-noise2"}
    rows, _ = await repo.search_documents(RELEVANT, vec, k=2)
    assert _ids(rows) == ["event:act-floor-loc1", "event:act-floor-loc2"]
    # The over-fetch is bounded (k * DOC_OVERFETCH rows): with k=1 both fetched rows are noise, so nothing is kept.
    assert await repo.search_documents(RELEVANT, vec, k=1) == ([], "hybrid")
