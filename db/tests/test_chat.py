"""AI Assistance chat (docs/chat-contract.md §1, §9): 011_chat.sql, 012_chat_logs.sql, 013_chat_mission.sql and
backend/app/db/chat.py.

Integration tests on a throwaway database spot_chattest_<hex> in the compose Postgres (DB_PORT, default 5433; or
CHAT_TEST_ADMIN_URL): `setup.py migrate` twice, `seed`, the stack's RAG `documents` (with vectors) copied in from
CHAT_TEST_SOURCE_URL (default: the `spot` database on the same server) plus two synthetic documents, then
`graph_build.py --no-embed`. ChatRepo runs as a throwaway LOGIN member of spot_app; a member of spot_mcp_reader must
not be able to read chats. The stack's own database is only read. Skips without Postgres. No embedding service is
needed: the vector paths use synthetic vectors.

    make test-chat-db      # = DB_PORT=<.env> backend/.venv/bin/python -m pytest db/tests/test_chat.py -q
"""
from __future__ import annotations

import asyncio
import datetime as dt
import decimal
import math
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
psycopg = pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")

from app.db.chat import ChatRepo, ConversationNotFound  # noqa: E402
from app.db.pool import Database, DatabaseUnavailable  # noqa: E402
from app.db.queries import BAND_ORDER  # noqa: E402

SETUP, BUILD = ROOT / "db" / "setup.py", ROOT / "db" / "graph_build.py"
MIGRATIONS = ROOT / "db" / "migrations"
ADMIN_URL = os.environ.get("CHAT_TEST_ADMIN_URL") or "postgresql://postgres:%s@localhost:%s/postgres" % (
    os.environ.get("POSTGRES_PASSWORD", "postgres"), os.environ.get("DB_PORT", "5433"))
SOURCE_URL = os.environ.get("CHAT_TEST_SOURCE_URL") or ADMIN_URL.rsplit("/", 1)[0] + "/spot"

CONV_KEYS = {"conversation_id", "title", "title_source", "model", "mode", "pinned", "created_at", "updated_at",
             "message_count", "mission_id", "mission"}                                   # chat.3: mission
MISSION_SUMMARY_KEYS = {"mission_id", "mission_name", "scenario", "configuration_version", "start_time",
                        "duration_min", "start_battery_pct", "end_battery_pct", "mission_status", "actions", "failed",
                        "anomalies", "serious_anomalies", "open_flags", "hidden_failure", "chats"}
SUMMARY_KEYS = CONV_KEYS | {"last_message_preview"}
MSG_KEYS = {"message_id", "conversation_id", "seq", "role", "content", "thinking", "thinking_ms", "model", "mode",
            "status", "sources", "citations", "grounding", "usage", "latency_ms", "error", "error_code", "request_id",
            "created_at"}
DOC_KEYS = {"doc_id", "kind", "title", "content", "asset_id", "location_id", "mission_id", "action_id", "ts", "score"}
NODE_KEYS = {"node_id", "kind", "label", "summary", "doc_id", "score", "match"}
NB_KEYS = {"node_id", "kind", "label", "summary", "doc_id", "hop", "via_rel", "via_node", "path", "weight", "evidence"}
SNAPSHOT_KEYS = {"missions", "reported_completed", "completed_with_failures", "reported_failed", "actions",
                 "failed_actions", "anomalies", "serious_anomalies", "open_flags", "open_by_severity", "battery_bands",
                 "by_config", "worst_locations", "repeat_offenders", "failure_types"}
MISSION_KEYS = {"mission_id", "mission_name", "scenario", "configuration_version", "start_time", "end_time",
                "duration_min", "start_battery_pct", "end_battery_pct", "mission_status", "actions", "failed",
                "failures", "anomalies", "flags"}
ASSET_KEYS = {"asset_id", "asset_name", "asset_type", "criticality", "location_id", "location_name", "inspections",
              "failed", "anomalies", "open_flags"}
LOCATION_KEYS = {"location_id", "location_name", "zone_type", "environment", "actions", "failed", "success_rate_pct",
                 "failure_types", "assets"}
FLAG_KEYS = {"flag_id", "title", "severity", "status", "escalated", "mission_id", "action_id", "asset_id",
             "location_id", "reasons", "created_at", "assessment", "acks"}


def _vec(f, dim):
    return "[" + ",".join("%.6f" % f(i) for i in range(dim)) + "]"


# ── fixture ───────────────────────────────────────────────────────────────────────────────────────
def _env(url):
    env = {k: v for k, v in os.environ.items() if k != "LOGS_DATABASE_URL"}
    env["DATABASE_URL"] = url
    return env


def _run(script, url, *args):
    r = subprocess.run([sys.executable, str(script), *args], env=_env(url), capture_output=True, text=True,
                       timeout=900)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    return r.stdout


def _q(url, sql, *args):
    with psycopg.connect(url, autocommit=True) as c:
        cur = c.execute(sql, args or None)            # no args: no placeholder parsing (011 has literal %)
        return cur.fetchall() if cur.description else []


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


def _login_url(url, user, password):
    u = urlparse(url)
    return u._replace(netloc="%s:%s@%s:%s" % (user, password, u.hostname, u.port or 5432)).geturl()


@pytest.fixture(scope="module")
def db():
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3)
    except psycopg.Error as exc:
        pytest.skip("no Postgres at %s (start the stack: make up): %s" % (ADMIN_URL.split("@")[-1], exc))
    tag = uuid.uuid4().hex[:8]
    name, app_role, mcp_role = "spot_chattest_" + tag, "chattest_app_" + tag, "chattest_mcp_" + tag
    admin.execute("CREATE DATABASE %s" % name)
    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + name
    try:
        out1 = _run(SETUP, url, "migrate")
        out2 = _run(SETUP, url, "migrate")                       # idempotent
        _run(SETUP, url, "seed")
        n_docs = _copy_documents(SOURCE_URL, url)
        dim = _q(url, "SELECT atttypmod FROM pg_attribute WHERE attrelid = 'documents'::regclass "
                      "AND attname = 'embedding'")[0][0]
        vec_doc = _vec(lambda i: math.sin(i * 0.37 + 1.0), dim)
        vec_node = _vec(lambda i: math.cos(i * 0.11 + 2.0), dim)
        _q(url, "INSERT INTO documents (doc_id, kind, title, content, asset_id, embedding, embed_model) VALUES "
                "('note:chattest-vec', 'operator_note', 'Chat test note', 'Quasar flux capacitor drift seen on HX-204 "
                "during the chattest run.', 'HX-204', %s::vector, 'synthetic'), "
                "('note:chattest-sub', 'operator_note', 'Chat test substring', 'Bespoke widget with xylophonic "
                "resonance.', NULL, NULL, NULL)", vec_doc)
        _run(BUILD, url, "--no-embed")
        _q(url, "UPDATE kg_nodes SET embedding = %s::vector WHERE node_id = 'location:WP09'", vec_node)
        for role, group in ((app_role, "spot_app"), (mcp_role, "spot_mcp_reader")):
            admin.execute("CREATE ROLE %s LOGIN PASSWORD 'chattest' IN ROLE %s" % (role, group))
        yield {"url": url, "app_url": _login_url(url, app_role, "chattest"),
               "mcp_url": _login_url(url, mcp_role, "chattest"), "migrate_out": (out1, out2), "docs": n_docs,
               "vec_doc": vec_doc, "vec_node": vec_node, "dim": dim}
    finally:
        admin.execute("DROP DATABASE IF EXISTS %s WITH (FORCE)" % name)
        for role in (app_role, mcp_role):
            admin.execute("DROP ROLE IF EXISTS %s" % role)
        admin.close()


def run(url, fn):
    """Run `await fn(ChatRepo)` on a fresh pool (each test gets its own event loop)."""
    async def main():
        d = Database(url, name="chattest", pool_max=4, connect_timeout_s=5)
        await d.open()
        try:
            return await fn(ChatRepo(d))
        finally:
            await d.close()
    return asyncio.run(main())


def _no_decimals(v):
    if isinstance(v, decimal.Decimal):
        raise AssertionError("Decimal in result: %r" % v)
    if isinstance(v, dict):
        for x in v.values():
            _no_decimals(x)
    elif isinstance(v, (list, tuple)):
        for x in v:
            _no_decimals(x)


def _is_uuid(s):
    return isinstance(s, str) and str(uuid.UUID(s)) == s


# ── migrations ────────────────────────────────────────────────────────────────────────────────────
def test_migrations_apply_twice(db):
    for out in db["migrate_out"]:
        assert "applied 011_chat.sql" in out and "applied 012_chat_logs.sql" in out
        assert "applied 013_chat_mission.sql" in out
    url = db["url"]
    idx = {r[0] for r in _q(url, "SELECT indexname FROM pg_indexes WHERE tablename IN "
                                 "('chat_conversations', 'chat_messages')")}
    assert {"chat_conversations_sidebar", "chat_conversations_title_trgm", "chat_messages_content_trgm",
            "chat_messages_conversation_id_seq_key", "chat_conversations_mission"} <= idx
    # 013: one FK to missions, ON DELETE SET NULL ('n')
    assert _q(url, "SELECT conname, confrelid::regclass::text, confdeltype FROM pg_constraint "
                   "WHERE conrelid = 'chat_conversations'::regclass AND contype = 'f'") == [
        ("chat_conversations_mission_id_fkey", "missions", "n")]
    # 011: exactly one CHECK on app.model_calls.system, allowing 'chat'
    checks = _q(url, "SELECT c.conname FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid "
                     "AND a.attnum = ANY (c.conkey) WHERE c.conrelid = 'app.model_calls'::regclass "
                     "AND c.contype = 'c' AND a.attname = 'system'")
    assert checks == [("model_calls_system_check",)]
    _q(url, "INSERT INTO app.model_calls (system, model, endpoint, ok, metadata) VALUES ('chat', 'gemini-3.8-flash', "
            "'gemini:streamGenerateContent', true, '{\"provider\": \"gemini\"}')")
    with pytest.raises(psycopg.errors.CheckViolation):
        _q(url, "INSERT INTO app.model_calls (system, ok) VALUES ('bogus', true)")


def test_011_adds_error_code_to_an_older_table(db):
    """chat.2: a database created before error_code existed gets the column from a re-run of 010 (data kept)."""
    url = db["url"]
    _q(url, "INSERT INTO chat_conversations (conversation_id, title, model) VALUES "
            "('00000000-0000-4000-8000-0000000000e1', 'pre chat.2', 'm')")
    _q(url, "INSERT INTO chat_messages (conversation_id, seq, role, content) VALUES "
            "('00000000-0000-4000-8000-0000000000e1', 1, 'user', 'old row')")
    _q(url, "ALTER TABLE chat_messages DROP COLUMN error_code")
    sql = (MIGRATIONS / "011_chat.sql").read_text()
    _q(url, sql)
    _q(url, sql)
    cols = {r[0]: r[1] for r in _q(url, "SELECT column_name, is_nullable FROM information_schema.columns "
                                        "WHERE table_name = 'chat_messages'")}
    assert cols.get("error_code") == "YES"
    assert _q(url, "SELECT content, error_code FROM chat_messages WHERE conversation_id = "
                   "'00000000-0000-4000-8000-0000000000e1'") == [("old row", None)]
    _q(url, "DELETE FROM chat_conversations WHERE conversation_id = '00000000-0000-4000-8000-0000000000e1'")


def test_013_on_an_older_table_keeps_data_and_detaches_orphans(db):
    """chat.3: 013 re-run on a table from before it (no column, no FK, no index) adds all three; a column that
    already exists without the FK gets the FK, and a dangling mission id is set NULL rather than failing."""
    url = db["url"]
    cid = "00000000-0000-4000-8000-0000000000e3"
    _q(url, "ALTER TABLE chat_conversations DROP COLUMN mission_id")          # drops the FK and the index too
    _q(url, "INSERT INTO chat_conversations (conversation_id, title, model) VALUES ('%s', 'pre chat.3', 'm')" % cid)
    sql = (MIGRATIONS / "013_chat_mission.sql").read_text()
    _q(url, sql)
    _q(url, sql)
    assert _q(url, "SELECT title, mission_id FROM chat_conversations WHERE conversation_id = %s::uuid", cid) == [
        ("pre chat.3", None)]
    _q(url, "ALTER TABLE chat_conversations DROP CONSTRAINT chat_conversations_mission_id_fkey")
    _q(url, "UPDATE chat_conversations SET mission_id = 'M_GONE' WHERE conversation_id = %s::uuid", cid)
    _q(url, sql)
    assert _q(url, "SELECT mission_id FROM chat_conversations WHERE conversation_id = %s::uuid", cid) == [(None,)]
    assert _q(url, "SELECT count(*) FROM pg_constraint WHERE conname = 'chat_conversations_mission_id_fkey'")[0][0] == 1
    assert _q(url, "SELECT count(*) FROM pg_indexes WHERE indexname = 'chat_conversations_mission'")[0][0] == 1
    _q(url, "DELETE FROM chat_conversations WHERE conversation_id = %s::uuid", cid)


def test_012_replaces_a_differently_named_check(db):
    url = db["url"]
    _q(url, "ALTER TABLE app.model_calls DROP CONSTRAINT model_calls_system_check")
    _q(url, "ALTER TABLE app.model_calls ADD CONSTRAINT legacy_system_chk "      # NOT VALID: a 'chat' row exists
            "CHECK (system IN ('system1','system2','embed')) NOT VALID")
    sql = (MIGRATIONS / "012_chat_logs.sql").read_text()
    _q(url, sql)
    _q(url, sql)
    checks = _q(url, "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                     "WHERE conrelid = 'app.model_calls'::regclass AND contype = 'c'")
    assert len(checks) == 1 and checks[0][0] == "model_calls_system_check" and "'chat'" in checks[0][1]


def test_rerun_keeps_data_and_mcp_revoke(db):
    """A full `migrate` re-runs 007 (which grants spot_mcp_reader SELECT on every public table) and then 010."""
    url = db["url"]
    _q(url, "INSERT INTO chat_conversations (title, model) VALUES ('survives migrate', 'm')")
    _run(SETUP, url, "migrate")
    assert _q(url, "SELECT count(*) FROM chat_conversations WHERE title = 'survives migrate'")[0][0] == 1
    with psycopg.connect(db["mcp_url"], autocommit=True) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SELECT count(*) FROM chat_conversations")
    _q(url, "DELETE FROM chat_conversations WHERE title = 'survives migrate'")


# ── grants ────────────────────────────────────────────────────────────────────────────────────────
def test_grants(db):
    async def crud(r):
        c = await r.create_conversation(title="grants", model="m", mode="fast")
        await r.add_message(c["conversation_id"], {"role": "user", "content": "hello"})
        await r.update_conversation(c["conversation_id"], pinned=True)
        assert (await r.list_conversations(q="grants"))[1] == 1
        assert await r.delete_messages_from(c["conversation_id"], 1) == 1
        assert await r.delete_conversation(c["conversation_id"]) is True
        # spot_app also reads everything retrieval needs
        await r.search_documents("battery", None)
        await r.match_graph_nodes("HX-204", db["vec_node"])
        await r.fleet_snapshot()
    run(db["app_url"], crud)

    with psycopg.connect(db["mcp_url"], autocommit=True) as c:
        for sql in ("SELECT count(*) FROM chat_conversations", "SELECT count(*) FROM chat_messages",
                    "INSERT INTO chat_conversations (model) VALUES ('x')", "DELETE FROM chat_messages"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(sql)
        assert c.execute("SELECT count(*) FROM documents").fetchone()[0] >= 2     # its other grants still work
        assert c.execute("SELECT count(*) FROM kg_nodes").fetchone()[0] > 0


def test_missing_tables_or_db_raise():
    with pytest.raises(DatabaseUnavailable):
        run(None, lambda r: r.list_conversations())
    try:
        psycopg.connect(ADMIN_URL, connect_timeout=3).close()
    except psycopg.Error:
        pytest.skip("no Postgres")
    with pytest.raises(psycopg.Error):                        # the `postgres` DB has no chat tables
        run(ADMIN_URL, lambda r: r.list_conversations())


# ── conversations ─────────────────────────────────────────────────────────────────────────────────
def test_conversation_crud(db):
    async def t(r):
        c = await r.create_conversation(title="Battery failures on M007", model="gemini-3.8-flash", mode="fast")
        assert set(c) == CONV_KEYS and _is_uuid(c["conversation_id"])
        assert (c["title_source"], c["pinned"], c["message_count"], c["mode"]) == ("default", False, 0, "fast")
        assert isinstance(c["created_at"], dt.datetime) and c["created_at"].tzinfo is not None
        cid = c["conversation_id"]
        assert await r.get_conversation(cid) == c
        assert await r.get_conversation(cid.upper()) == c                       # any uuid spelling
        for bad in ("not-a-uuid", "", None, "123", str(uuid.uuid4())):
            assert await r.get_conversation(bad) is None
            assert await r.update_conversation(bad, title="x") is None
            assert await r.delete_conversation(bad) is False

        u = await r.update_conversation(cid, title="Renamed", title_source="user")
        assert set(u) == CONV_KEYS and (u["title"], u["title_source"]) == ("Renamed", "user")
        assert (u["model"], u["mode"], u["pinned"]) == ("gemini-3.8-flash", "fast", False)   # untouched
        assert u["updated_at"] > c["updated_at"] and u["created_at"] == c["created_at"]
        p = await r.update_conversation(cid, pinned=True, mode="thinking", model="gemma-4-31b-it")
        assert (p["title"], p["pinned"], p["mode"], p["model"]) == ("Renamed", True, "thinking", "gemma-4-31b-it")
        assert p["updated_at"] > u["updated_at"]
        bump = await r.update_conversation(cid)                                 # nothing given: only updated_at
        assert bump["updated_at"] > p["updated_at"] and bump["title"] == "Renamed"
        # keep_user_title: the model-title write is refused in SQL once the operator has renamed the chat
        assert await r.update_conversation(cid, title="Model title", title_source="model", keep_user_title=True) is None
        assert (await r.get_conversation(cid))["title"] == "Renamed"
        auto = await r.create_conversation(title="x", model="m", mode="fast")
        await r.update_conversation(auto["conversation_id"], title="Auto", title_source="auto")
        m = await r.update_conversation(auto["conversation_id"], title="Model title", title_source="model",
                                        keep_user_title=True)
        assert (m["title"], m["title_source"]) == ("Model title", "model")

        assert (await r.create_conversation(title="  \n ", model="m", mode="fast"))["title"] == "New chat"
        long = await r.create_conversation(title="x" * 300, model="m", mode="fast")
        assert len(long["title"]) == 200

        assert await r.delete_conversation(cid) is True
        assert await r.get_conversation(cid) is None
        assert await r.delete_conversation(cid) is False
    run(db["app_url"], t)


# ── messages ──────────────────────────────────────────────────────────────────────────────────────
def test_messages_seq_jsonb_and_cascade(db):
    sources = [{"id": "incident:AN012", "n": 1, "origin": "rag", "kind": "incident", "title": "Gauge",
                "snippet": "out of range", "score": 0.83, "refs": {"asset_id": "HX-217", "flag_id": 14}}]
    grounding = {"grounded": True, "cited_ids": ["incident:AN012"], "unknown_ids": [], "unverified_numbers": [],
                 "source_count": 1, "retrieval_mode": "hybrid", "graph_seeds": ["asset:HX-217"]}
    usage = {"prompt_tokens": 3100, "output_tokens": 240, "thoughts_tokens": 410}

    async def t(r):
        c = await r.create_conversation(title="msgs", model="m", mode="fast")
        cid = c["conversation_id"]
        m1 = await r.add_message(cid, {"role": "user", "content": "Why HX-217?"})
        assert set(m1) == MSG_KEYS and m1["seq"] == 1 and _is_uuid(m1["message_id"])
        assert m1["conversation_id"] == cid and isinstance(m1["created_at"], dt.datetime)
        assert (m1["sources"], m1["citations"], m1["grounding"], m1["usage"], m1["thinking"], m1["status"]) == \
            ([], [], None, None, None, "complete")
        conv = await r.get_conversation(cid)
        assert conv["updated_at"] > c["updated_at"] and conv["message_count"] == 1

        m2 = await r.add_message(cid, {
            "role": "assistant", "content": "Gauge out of range [incident:AN012].", "thinking": "hmm",
            "thinking_ms": 5200, "model": "gemini-3.8-flash", "mode": "thinking", "status": "complete",
            "sources": sources, "citations": ["incident:AN012"], "grounding": grounding, "usage": usage,
            "latency_ms": 6400, "error": None, "error_code": None, "request_id": "req-1",
            # assigned by the database / unknown: ignored
            "message_id": str(uuid.uuid4()), "seq": 99, "created_at": "2020-01-01", "conversation_id": "x",
            "n": 3})
        assert m2["seq"] == 2 and (m2["sources"], m2["citations"], m2["grounding"], m2["usage"]) == \
            (sources, ["incident:AN012"], grounding, usage)
        assert (m2["thinking"], m2["thinking_ms"], m2["latency_ms"], m2["request_id"]) == ("hmm", 5200, 6400, "req-1")
        m3 = await r.add_message(cid, {"role": "assistant", "content": None, "status": "error", "error": "429",
                                       "error_code": "rate_limited", "sources": None, "citations": None})
        assert (m3["seq"], m3["content"], m3["status"], m3["sources"], m3["citations"]) == (3, "", "error", [], [])
        assert m3["error_code"] == "rate_limited" and m2["error_code"] is None and m1["error_code"] is None

        with pytest.raises(ValueError):
            await r.add_message(cid, {"content": "no role"})
        for bad in ("nope", str(uuid.uuid4())):
            with pytest.raises(ConversationNotFound):
                await r.add_message(bad, {"role": "user", "content": "x"})

        # race-free seq: 12 concurrent writers on one conversation
        got = await asyncio.gather(*[r.add_message(cid, {"role": "user", "content": "c%d" % i}) for i in range(12)])
        assert sorted(m["seq"] for m in got) == list(range(4, 16))
        msgs = await r.list_messages(cid)
        assert [m["seq"] for m in msgs] == list(range(1, 16)) and all(set(m) == MSG_KEYS for m in msgs)
        assert msgs[1] == m2
        assert await r.list_messages("garbage") == [] and await r.list_messages(str(uuid.uuid4())) == []

        # edit / regenerate: drop the tail, next message continues from the new max
        assert await r.delete_messages_from(cid, 5) == 11
        assert [m["seq"] for m in await r.list_messages(cid)] == [1, 2, 3, 4]
        assert await r.delete_messages_from(cid, 99) == 0
        assert await r.delete_messages_from("garbage", 1) == 0
        assert await r.delete_messages_from(str(uuid.uuid4()), 1) == 0
        assert (await r.add_message(cid, {"role": "user", "content": "edited"}))["seq"] == 5
        assert (await r.get_conversation(cid))["message_count"] == 5

        assert await r.delete_conversation(cid) is True
        return cid
    cid = run(db["app_url"], t)
    assert _q(db["url"], "SELECT count(*) FROM chat_messages WHERE conversation_id = %s::uuid", cid)[0][0] == 0


def test_list_conversations_order_and_search(db):
    _q(db["url"], "DELETE FROM chat_conversations")

    async def t(r):
        a = await r.create_conversation(title="Alpha battery question", model="m", mode="fast")
        b = await r.create_conversation(title="Bravo", model="m", mode="fast")
        c = await r.create_conversation(title="Charlie", model="m", mode="fast")
        empty = await r.create_conversation(title="Empty draft", model="m", mode="fast")
        await r.add_message(a["conversation_id"], {"role": "user", "content": "How long do the batteries last?"})
        await r.add_message(a["conversation_id"], {"role": "assistant", "content": "", "status": "error",
                                                   "error": "429", "error_code": "rate_limited"})
        await r.add_message(c["conversation_id"], {"role": "user", "content": "Charlie question"})
        await r.update_conversation(a["conversation_id"], pinned=True)
        body = "Why does the zebra-stripe\n\n  pattern   show up? " + "word " * 80
        await r.add_message(b["conversation_id"], {"role": "user", "content": body})
        await r.add_message(b["conversation_id"], {"role": "assistant", "content": "", "status": "error"})
        await r.update_conversation(empty["conversation_id"], pinned=True)   # pinned but empty: still hidden

        items, total = await r.list_conversations()
        assert total == 3 and [i["title"] for i in items] == ["Alpha battery question", "Bravo", "Charlie"]
        assert all(set(i) == SUMMARY_KEYS for i in items)
        bi = items[1]
        assert bi["message_count"] == 2 and bi["last_message_preview"].startswith("Why does the zebra-stripe pattern")
        assert len(bi["last_message_preview"]) <= 120 and "\n" not in bi["last_message_preview"]
        assert items[0]["last_message_preview"] == "How long do the batteries last?"   # the empty error is skipped
        assert items[0]["message_count"] == 2

        await r.update_conversation(c["conversation_id"], pinned=True)      # two pinned: most recent first
        assert [i["title"] for i in (await r.list_conversations())[0]] == ["Charlie", "Alpha battery question", "Bravo"]

        assert [i["title"] for i in (await r.list_conversations(q="ALPHA"))[0]] == ["Alpha battery question"]
        assert [i["title"] for i in (await r.list_conversations(q="zebra-STRIPE"))[0]] == ["Bravo"]   # content
        assert await r.list_conversations(q="no such thing anywhere") == ([], 0)
        assert await r.list_conversations(q="empty draft") == ([], 0)      # chat.2: no ghost rows, even by title
        assert (await r.list_conversations(q="   "))[1] == 3                    # blank q = no filter
        u = await r.create_conversation(title="100% uptime_check", model="m", mode="fast")
        assert [i["title"] for i in (await r.list_conversations(q="%"))[0]] == []          # no messages yet
        await r.add_message(u["conversation_id"], {"role": "user", "content": "uptime?"})
        assert [i["title"] for i in (await r.list_conversations(q="%"))[0]] == ["100% uptime_check"]   # literal
        assert [i["title"] for i in (await r.list_conversations(q="_"))[0]] == ["100% uptime_check"]

        page, total = await r.list_conversations(limit=1, offset=1)
        assert total == 4 and [i["title"] for i in page] == ["Alpha battery question"]
        assert await r.list_conversations(limit=10, offset=50) == ([], 4)   # the fallback count skips empty ones too
        assert await r.get_conversation(empty["conversation_id"]) is not None   # hidden from the list only
    run(db["app_url"], t)


# ── retrieval ─────────────────────────────────────────────────────────────────────────────────────
def test_search_documents(db):
    async def t(r):
        rows, mode = await r.search_documents("quasar flux capacitor", None)
        assert mode == "fulltext" and rows[0]["doc_id"] == "note:chattest-vec"
        assert all(set(x) == DOC_KEYS and isinstance(x["score"], float) for x in rows)
        assert [x["score"] for x in rows] == sorted((x["score"] for x in rows), reverse=True)

        rows, _ = await r.search_documents("HX-204 drift", None, k=50, kinds=["operator_note"])
        assert rows and {x["kind"] for x in rows} == {"operator_note"}
        rows, mode = await r.search_documents("ylophoni", None)          # no lexeme matches: literal ILIKE pass
        assert mode == "fulltext" and [(x["doc_id"], x["score"]) for x in rows] == [("note:chattest-sub", 0.0)]
        assert await r.search_documents("", None) == ([], "fulltext")
        assert await r.search_documents("zzzqqq vvvxxj", None) == ([], "fulltext")

        rows, mode = await r.search_documents("quasar", db["vec_doc"])
        assert mode == "hybrid" and rows[0]["doc_id"] == "note:chattest-vec" and len(rows) <= 8
        assert all(set(x) == DOC_KEYS and isinstance(x["score"], float) for x in rows)
        rows, mode = await r.search_documents("", db["vec_doc"], k=3)    # pure semantic
        assert mode == "hybrid" and rows[0]["doc_id"] == "note:chattest-vec" and len(rows) <= 3
        rows, _ = await r.search_documents("quasar", db["vec_doc"], kinds=["policy"])
        assert all(x["kind"] == "policy" for x in rows)
        if db["docs"]:                                                   # real kit documents were copied
            rows, mode = await r.search_documents("robot lost localization", None, k=5)
            assert mode == "fulltext" and 1 <= len(rows) <= 5
            rows, _ = await r.search_documents("HX-204", None, k=5)
            assert rows and all("hx" in (x["content"] + (x["title"] or "")).lower() for x in rows)
    run(db["app_url"], t)


def test_match_graph_nodes(db):
    async def t(r):
        rows = await r.match_graph_nodes("HX-204", None)
        assert rows[0]["node_id"] == "asset:HX-204" and rows[0]["score"] == 1.0 and rows[0]["match"] == "text"
        assert len(rows) <= 6 and all(set(x) == NODE_KEYS and 0.0 <= x["score"] <= 1.0 for x in rows)
        rows = await r.match_graph_nodes("why does HX-204 keep failing", None, k=3)
        assert rows[0]["node_id"] == "asset:HX-204" and len(rows) <= 3

        rows = await r.match_graph_nodes("zzqv", db["vec_node"])        # vector only
        assert rows[0]["node_id"] == "location:WP09" and rows[0]["match"] == "vector" and rows[0]["score"] > 0.999
        assert all(x["match"] == "vector" and 0.0 <= x["score"] <= 1.0 for x in rows)
        rows = await r.match_graph_nodes("Turbine Hall South", db["vec_node"], k=10)
        assert rows[0]["node_id"] == "location:WP09" and rows[0]["match"] == "both" and rows[0]["score"] == 1.0
        assert len({x["node_id"] for x in rows}) == len(rows) <= 10
        assert [x["score"] for x in rows] == sorted((x["score"] for x in rows), reverse=True)
        assert await r.match_graph_nodes("", None) == []
    run(db["app_url"], t)


def test_graph_neighborhood(db):
    async def t(r):
        rows = await r.graph_neighborhood(["asset:HX-204"])
        assert 0 < len(rows) <= 25 and all(set(x) == NB_KEYS for x in rows)
        assert all(isinstance(x["path"], list) and x["path"][0] == "asset:HX-204" and x["hop"] >= 1 for x in rows)
        assert all(isinstance(x["weight"], float) for x in rows)
        rows = await r.graph_neighborhood(["asset:HX-204", "location:WP05"], max_hops=2, max_nodes=5,
                                          kind_filter=["failure_type", "anomaly_type", "topic"])
        assert 0 < len(rows) <= 5 and {x["kind"] for x in rows} <= {"failure_type", "anomaly_type", "topic"}
        assert await r.graph_neighborhood([]) == [] and await r.graph_neighborhood(["asset:NOPE-000"]) == []
    run(db["app_url"], t)


def test_fleet_snapshot(db):
    s = run(db["app_url"], lambda r: r.fleet_snapshot())
    url = db["url"]
    assert set(s) == SNAPSHOT_KEYS
    _no_decimals(s)
    one = lambda sql: _q(url, sql)[0][0]  # noqa: E731
    assert s["missions"] == one("SELECT count(*) FROM missions") > 0
    assert s["reported_completed"] + s["reported_failed"] == s["missions"]
    assert 0 < s["completed_with_failures"] <= s["reported_completed"]
    assert s["actions"] == one("SELECT count(*) FROM actions")
    assert s["failed_actions"] == one("SELECT count(*) FROM actions WHERE status = 'FAILED'")
    assert s["anomalies"] == one("SELECT count(*) FROM anomalies")
    assert s["serious_anomalies"] == one("SELECT count(*) FROM anomalies WHERE severity IN ('HIGH','CRITICAL')")
    assert s["open_flags"] == one("SELECT count(*) FROM flags WHERE escalated AND status = 'OPEN'")
    assert sum(s["open_by_severity"].values()) == s["open_flags"]
    assert list(s["open_by_severity"])[:2] == ["CRITICAL", "HIGH"]
    assert [b["band"] for b in s["battery_bands"]] == BAND_ORDER
    assert sum(b["n"] for b in s["battery_bands"]) == one("SELECT count(*) FROM actions "
                                                         "WHERE status IN ('SUCCESS','FAILED')")
    for b in s["battery_bands"] + s["by_config"] + s["worst_locations"]:
        assert b["success_rate_pct"] is None if b["n"] == 0 else isinstance(b["success_rate_pct"], float)
    assert {c["config"] for c in s["by_config"]} == {"V1", "V2"}
    assert all(set(c) == {"config", "n", "success_rate_pct"} for c in s["by_config"])
    wl = s["worst_locations"]
    assert len(wl) == 5 and all(set(x) == {"location_id", "location_name", "environment", "n", "success_rate_pct"}
                                for x in wl)
    assert [x["success_rate_pct"] for x in wl] == sorted(x["success_rate_pct"] for x in wl)
    ro = s["repeat_offenders"]
    assert len(ro) == 5 and all(set(x) == {"asset_id", "asset_name", "criticality", "anomalies", "serious"} for x in ro)
    assert [x["anomalies"] for x in ro] == sorted((x["anomalies"] for x in ro), reverse=True)
    ft = s["failure_types"]
    assert 0 < len(ft) <= 8 and all(set(x) == {"failure_type", "n"} for x in ft)
    assert [x["n"] for x in ft] == sorted((x["n"] for x in ft), reverse=True)
    assert sum(x["n"] for x in ft) <= s["failed_actions"]


def test_entity_digests(db):
    url = db["url"]
    mission = _q(url, "SELECT mission_id FROM actions WHERE status = 'FAILED' GROUP BY 1 ORDER BY count(*) DESC, 1 "
                      "LIMIT 1")[0][0]
    asset = _q(url, "SELECT asset_id FROM anomalies GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 1")[0][0]
    location = _q(url, "SELECT location_id FROM actions WHERE status = 'FAILED' GROUP BY 1 ORDER BY count(*) DESC, 1 "
                       "LIMIT 1")[0][0]
    flag = _q(url, "SELECT flag_id FROM flags ORDER BY flag_id LIMIT 1")[0][0]
    _q(url, "INSERT INTO acknowledgements (flag_id, action, note, operator) VALUES (%s, 'NOTE', 'checked it', 'ana'), "
            "(%s, 'ACKNOWLEDGE', NULL, 'ben')", flag, flag)
    _q(url, "INSERT INTO assessments (flag_id, state, answer, recommended_action, model) VALUES "
            "(%s, 'DONE', 'Escalate.', 'Inspect the gauge.', 'test-model') ON CONFLICT (flag_id) DO UPDATE SET "
            "state = 'DONE', answer = EXCLUDED.answer, recommended_action = EXCLUDED.recommended_action, "
            "model = EXCLUDED.model", flag)

    d = run(db["app_url"], lambda r: r.entity_digests(
        mission_ids=["M999", mission.lower()], asset_ids=["XX-000", "asset:" + asset], location_ids=[location, "WP99"],
        flag_ids=[999999999, "#%d" % flag, "abc", -1]))
    _no_decimals(d)
    assert set(d) == {"missions", "assets", "locations", "flags"}
    (m,), (a,), (lo,), (f,) = d["missions"], d["assets"], d["locations"], d["flags"]

    assert set(m) == MISSION_KEYS and m["mission_id"] == mission and isinstance(m["start_time"], dt.datetime)
    assert 0 < m["failed"] <= m["actions"] and len(m["failures"]) == min(m["failed"], 12)
    assert all(set(x) == {"action_id", "sequence", "action_type", "asset_id", "location_id", "battery_pct",
                          "failure_type"} for x in m["failures"])
    assert [x["sequence"] for x in m["failures"]] == sorted(x["sequence"] for x in m["failures"])
    assert len(m["anomalies"]) <= 10 and all(set(x) == {"anomaly_id", "asset_id", "anomaly_type", "severity",
                                                        "description"} for x in m["anomalies"])
    assert len(m["flags"]) <= 10 and all(set(x) == {"flag_id", "severity", "status", "title"} for x in m["flags"])

    assert set(a) == ASSET_KEYS and a["asset_id"] == asset and a["location_name"]
    assert a["inspections"] >= a["failed"] >= 0 and 0 < len(a["anomalies"]) <= 10
    assert all(set(x) == {"anomaly_id", "mission_id", "anomaly_type", "severity", "timestamp"} and
               isinstance(x["timestamp"], dt.datetime) for x in a["anomalies"])
    assert isinstance(a["open_flags"], int)

    assert set(lo) == LOCATION_KEYS and lo["location_id"] == location and lo["failed"] > 0
    assert isinstance(lo["success_rate_pct"], float) and lo["failure_types"] and lo["assets"]
    assert sum(x["n"] for x in lo["failure_types"]) == lo["failed"]
    assert all(set(x) == {"asset_id", "asset_name"} for x in lo["assets"])

    assert set(f) == FLAG_KEYS and f["flag_id"] == flag and isinstance(f["reasons"], list)
    assert isinstance(f["created_at"], dt.datetime)
    assert f["assessment"] == {"answer": "Escalate.", "recommended_action": "Inspect the gauge.", "model": "test-model"}
    assert [(k["action"], k["note"], k["operator"]) for k in f["acks"]][-2:] == [("NOTE", "checked it", "ana"),
                                                                                ("ACKNOWLEDGE", None, "ben")]
    assert all(set(k) == {"action", "note", "operator", "created_at"} and isinstance(k["created_at"], dt.datetime)
               for k in f["acks"])

    # <= 5 per kind, in the order given; nothing asked -> empty lists
    many = [r[0] for r in _q(url, "SELECT mission_id FROM missions ORDER BY mission_id DESC LIMIT 7")]
    got = run(db["app_url"], lambda r: r.entity_digests(mission_ids=many))
    assert [x["mission_id"] for x in got["missions"]] == many[:5] and got["flags"] == []
    assert run(db["app_url"], lambda r: r.entity_digests()) == {"missions": [], "assets": [], "locations": [],
                                                                "flags": []}


def test_suggestion_facts(db):
    async def t(r):
        return await r.suggestion_facts(), await r.fleet_snapshot()
    f, s = run(db["app_url"], t)
    _no_decimals(f)
    assert set(f) == {"hidden_failures", "top_asset", "worst_location", "latest_serious_flag", "low_battery"}
    assert f["hidden_failures"] == {"count": s["completed_with_failures"], "of": s["reported_completed"]}
    top = s["repeat_offenders"][0]
    assert f["top_asset"] == {"asset_id": top["asset_id"], "asset_name": top["asset_name"],
                              "anomalies": top["anomalies"]}
    w = s["worst_locations"][0]
    assert f["worst_location"] == {k: w[k] for k in ("location_id", "location_name", "success_rate_pct")}
    first = next(b for b in s["battery_bands"] if b["n"])
    assert f["low_battery"] == {"band": first["band"], "success_rate_pct": first["success_rate_pct"]}
    lf = f["latest_serious_flag"]
    assert set(lf) == {"flag_id", "title", "mission_id"}
    assert _q(db["url"], "SELECT severity FROM flags WHERE flag_id = %s", lf["flag_id"])[0][0] in ("HIGH", "CRITICAL")


def test_retrieval_is_fast(db):
    """Budget for the whole retrieval step is 8 s; each call should be well under 100 ms locally."""
    q = "Why does HX-204 keep failing at the Pipe Gallery on low battery?"

    async def t(r):
        calls = {
            "search_documents/fulltext": lambda: r.search_documents(q, None),
            "search_documents/hybrid": lambda: r.search_documents(q, db["vec_doc"]),
            "match_graph_nodes": lambda: r.match_graph_nodes(q, db["vec_node"]),
            "graph_neighborhood": lambda: r.graph_neighborhood(["asset:HX-204", "location:WP05", "mission:M007",
                                                                "topic:battery"]),
            "fleet_snapshot": r.fleet_snapshot,
            "entity_digests": lambda: r.entity_digests(mission_ids=["M007"], asset_ids=["HX-204"],
                                                       location_ids=["WP05"], flag_ids=[1]),
            "suggestion_facts": r.suggestion_facts,
        }
        times = {}
        for name, call in calls.items():
            await call()                                                  # warm the pool / plan cache
            t0 = time.perf_counter()
            await call()
            times[name] = (time.perf_counter() - t0) * 1000
        return times
    times = run(db["app_url"], t)
    print("\nretrieval timings (ms): " + ", ".join("%s %.1f" % kv for kv in times.items()))
    assert all(ms < 500 for ms in times.values()), times


# ── mission chats (chat.3, §9) ──────────────────────────────────────────────────────────────────────
def test_mission_conversations(db):
    url = db["url"]
    mid = _q(url, "SELECT mission_id FROM missions ORDER BY mission_id LIMIT 1")[0][0]
    name, status = _q(url, "SELECT mission_name, mission_status FROM missions WHERE mission_id = %s", mid)[0]
    _q(url, "INSERT INTO missions (mission_id, mission_name, mission_status) VALUES ('MZ01', 'Throwaway', 'FAILED')")

    async def t(r):
        c = await r.create_conversation(title="about a mission", model="m", mode="fast", mission_id=mid.lower())
        assert set(c) == CONV_KEYS and c["mission_id"] == mid                    # stored canonical
        assert c["mission"] == {"mission_id": mid, "mission_name": name, "mission_status": status}
        cid = c["conversation_id"]
        assert (await r.get_conversation(cid))["mission"] == c["mission"]
        u = await r.update_conversation(cid, title="renamed", pinned=True)       # no way to change the mission
        assert u["mission_id"] == mid and u["mission"] == c["mission"]
        plain = await r.create_conversation(title="plain", model="m", mode="fast")
        assert plain["mission_id"] is None and plain["mission"] is None
        gone = await r.create_conversation(title="gone", model="m", mode="fast", mission_id="MZ01")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            await r.create_conversation(title="x", model="m", mode="fast", mission_id="M999")

        for x in (c, plain, gone):
            await r.add_message(x["conversation_id"], {"role": "user", "content": "mission question"})
        items, total = await r.list_conversations(q="mission question")
        assert total == 3 and all(set(i) == SUMMARY_KEYS for i in items)
        for given in (mid, mid.lower(), "mission:" + mid):
            items, total = await r.list_conversations(mission_id=given)
            assert total == 1 and [i["conversation_id"] for i in items] == [cid]
            assert items[0]["mission"]["mission_name"] == name
        assert await r.list_conversations(mission_id="M999") == ([], 0)
        assert (await r.list_conversations(mission_id=mid, limit=1, offset=5)) == ([], 1)   # fallback count filtered
        return cid, plain["conversation_id"], gone["conversation_id"]
    cid, plain, gone = run(db["app_url"], t)

    _q(url, "DELETE FROM missions WHERE mission_id = 'MZ01'")                    # ON DELETE SET NULL
    async def after(r):
        g = await r.get_conversation(gone)
        assert g is not None and g["mission_id"] is None and g["mission"] is None
        for x in (cid, plain, gone):
            assert await r.delete_conversation(x)
    run(db["app_url"], after)


def test_search_documents_mission_filter(db):
    url = db["url"]
    mid = _q(url, "SELECT mission_id FROM missions ORDER BY mission_id DESC LIMIT 1")[0][0]
    vec = _vec(lambda i: math.sin(i * 0.53 + 0.2), db["dim"])
    _q(url, "INSERT INTO documents (doc_id, kind, title, content, mission_id, embedding, embed_model) VALUES "
            "('note:chattest-mission', 'operator_note', 'Mission note', 'Quasar flux capacitor drift on this "
            "mission too.', %s, %s::vector, 'synthetic')", mid, vec)
    try:
        async def t(r):
            rows, mode = await r.search_documents("quasar flux capacitor", None, mission_id=mid.lower())
            assert mode == "fulltext" and [x["doc_id"] for x in rows] == ["note:chattest-mission"]
            rows, _ = await r.search_documents("quasar flux capacitor", None)
            assert {"note:chattest-mission", "note:chattest-vec"} <= {x["doc_id"] for x in rows}  # unfiltered
            rows, _ = await r.search_documents("zzqx-not-a-lexeme-ylophoni", None, mission_id=mid)  # ILIKE pass
            assert rows == []
            rows, mode = await r.search_documents("quasar", vec, k=6, mission_id=mid)
            assert mode == "hybrid" and rows and rows[0]["doc_id"] == "note:chattest-mission"
            assert all(x["mission_id"] == mid for x in rows) and len(rows) <= 6
            assert all(set(x) == DOC_KEYS for x in rows)
            if db["docs"]:                                                    # real kit events carry mission ids
                rows, _ = await r.search_documents("failed action battery", None, k=6, mission_id="M007")
                assert all(x["mission_id"] == "M007" for x in rows)
        run(db["app_url"], t)
    finally:
        _q(url, "DELETE FROM documents WHERE doc_id = 'note:chattest-mission'")


def test_list_missions(db):
    url = db["url"]

    async def t(r):
        rows = await r.list_missions()
        snap = await r.fleet_snapshot()
        c = await r.create_conversation(title="count me", model="m", mode="fast", mission_id=rows[0]["mission_id"])
        empty = await r.create_conversation(title="not me", model="m", mode="fast", mission_id=rows[0]["mission_id"])
        await r.add_message(c["conversation_id"], {"role": "user", "content": "hi"})
        after = await r.list_missions(q=rows[0]["mission_id"].lower())
        dig = await r.entity_digests(mission_ids=[rows[0]["mission_id"]])
        filtered = await r.list_missions(q=(rows[0]["mission_name"] or "")[:5].upper())
        for x in (c, empty):
            await r.delete_conversation(x["conversation_id"])
        return rows, snap, after, dig, filtered, await r.list_missions(limit=2), await r.list_missions(q="%_zz")
    rows, snap, after, dig, filtered, two, none = run(db["app_url"], t)
    _no_decimals(rows)
    assert len(rows) == snap["missions"] and all(set(x) == MISSION_SUMMARY_KEYS for x in rows)
    assert [x["mission_id"] for x in rows] == sorted(x["mission_id"] for x in rows)
    assert sum(x["hidden_failure"] for x in rows) == snap["completed_with_failures"]
    assert sum(x["failed"] for x in rows) == snap["failed_actions"] and sum(x["actions"] for x in rows) == snap["actions"]
    assert sum(x["anomalies"] for x in rows) == snap["anomalies"]
    assert sum(x["serious_anomalies"] for x in rows) == snap["serious_anomalies"]
    assert sum(x["open_flags"] for x in rows) <= snap["open_flags"]     # flags without a mission aren't counted
    assert all(x["chats"] == 0 for x in rows)
    assert [x["chats"] for x in after if x["mission_id"] == rows[0]["mission_id"]] == [1]   # empty chat not counted
    d = dig["missions"][0]
    assert {k: after[0][k] for k in ("mission_id", "actions", "failed", "mission_status")} == \
        {k: d[k] for k in ("mission_id", "actions", "failed", "mission_status")}
    assert after[0]["hidden_failure"] == (d["mission_status"] == "COMPLETED" and d["failed"] > 0)
    assert rows[0]["mission_id"] in [x["mission_id"] for x in filtered]
    assert len(two) == 2 and none == []
    with psycopg.connect(db["mcp_url"], autocommit=True) as c:                # 013 grants nothing new
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SELECT mission_id FROM chat_conversations")
