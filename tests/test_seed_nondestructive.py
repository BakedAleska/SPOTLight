"""Audit M5: `db/setup.py seed` (run by db-init on every `docker compose up`) never overwrites live flags or refills a
demo; only `seed --force-reseed` does.

Runs against a throwaway database (spot_seedtest_<hex>) created on the compose Postgres, so the stack's own `spot`
database is never touched. Needs the `db` service up (host port DB_PORT, default 5433) or SEED_TEST_ADMIN_URL;
skips otherwise. `make test-db`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "db" / "setup.py"
ADMIN_URL = os.environ.get("SEED_TEST_ADMIN_URL") or "postgresql://postgres:%s@localhost:%s/postgres" % (
    os.environ.get("POSTGRES_PASSWORD", "postgres"), os.environ.get("DB_PORT", "5433"))


def _setup(url, *args):
    env = {k: v for k, v in os.environ.items() if k not in ("LOGS_DATABASE_URL",)}
    env["DATABASE_URL"] = url
    r = subprocess.run([sys.executable, str(SETUP), *args], env=env, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    return r.stdout


@pytest.fixture(scope="module")
def db():
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3)
    except psycopg.Error as exc:
        pytest.skip("no Postgres at %s (start the stack: make up): %s" % (ADMIN_URL.split("@")[-1], exc))
    name = "spot_seedtest_" + uuid.uuid4().hex[:8]
    admin.execute("CREATE DATABASE %s" % name)
    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + name
    try:
        _setup(url, "migrate")
        yield url
    finally:
        admin.execute("DROP DATABASE IF EXISTS %s WITH (FORCE)" % name)
        admin.close()


def _q(url, sql, *args):
    with psycopg.connect(url, autocommit=True) as c:
        cur = c.execute(sql, args)
        return cur.fetchall() if cur.description else []


ANALYTICS = ("SELECT (SELECT count(*) FROM mission_kpis), (SELECT count(*) FROM risk_scores), "
             "(SELECT count(*) FROM insights)")


def _counts(out: str) -> dict:
    return json.loads(out[out.index("{"):])


def test_seed_is_non_destructive_and_never_refills(db):
    # 1. First seed of a fresh database loads the demo baseline.
    _setup(db, "seed")
    assert _q(db, "SELECT count(*) FROM flags")[0][0] == 58
    assert _q(db, "SELECT count(*) FROM events")[0][0] == 500
    assert _q(db, ANALYTICS)[0] == (27, 189, 5)
    (live_id, live_reasons), = _q(db, "SELECT flag_id, reasons FROM flags WHERE triage_event_id = 'act-A0029'")
    acked_id, = _q(db, "SELECT flag_id FROM flags WHERE triage_event_id <> 'act-A0029' ORDER BY flag_id LIMIT 1")[0]

    # 2. Live activity: live ingest rewrote one flag (verdict reasons, un-escalated), an operator acked another,
    #    and a live-only flag exists.
    live = [{"rule": "laya_verdict", "verdict": "log"}, {"rule": "rules_verdict", "agreement": "rules_only_escalate"}]
    _q(db, "UPDATE flags SET reasons = %s::jsonb, escalated = false, score = 47, priority = 'P3', severity = 'MEDIUM' "
           "WHERE flag_id = %s", json.dumps(live), live_id)
    _q(db, "INSERT INTO acknowledgements (flag_id, action, note) VALUES (%s, 'ACKNOWLEDGE', 'seen')", acked_id)
    _q(db, "UPDATE flags SET status = 'ACKNOWLEDGED', title = 'edited by operator' WHERE flag_id = %s", acked_id)
    _q(db, "INSERT INTO flags (score, severity, title, reasons, escalated, triage_event_id) "
           "VALUES (80, 'HIGH', 'live only', '[]', true, 'act-LIVE1')")
    before = _q(db, "SELECT flag_id, reasons, escalated, score, status, title FROM flags ORDER BY flag_id")

    out = _setup(db, "seed")                            # what db-init runs on every `up`
    assert "skipped" in out
    assert _q(db, "SELECT flag_id, reasons, escalated, score, status, title FROM flags ORDER BY flag_id") == before
    assert _q(db, "SELECT reasons FROM flags WHERE flag_id = %s", live_id)[0][0] == live
    assert _q(db, "SELECT count(*) FROM acknowledgements WHERE flag_id = %s", acked_id)[0][0] == 1

    # 3. The demo is reset (what /ops/reset-demo deletes): a re-run must not refill the queue or the feed.
    for t in ("acknowledgements", "assessments", "flags", "events"):
        _q(db, "DELETE FROM %s" % t)
    _setup(db, "seed")
    _setup(db, "seed", "--no-events")
    assert _q(db, "SELECT count(*) FROM flags")[0][0] == 0
    assert _q(db, "SELECT count(*) FROM events")[0][0] == 0

    # 4. --force-reseed is the explicit destructive path: the baseline comes back, seeded columns overwritten.
    _setup(db, "seed", "--force-reseed")
    assert _q(db, "SELECT count(*) FROM flags")[0][0] == 58
    assert _q(db, "SELECT count(*) FROM events")[0][0] == 500
    assert _q(db, "SELECT reasons FROM flags WHERE triage_event_id = 'act-A0029'")[0][0] == live_reasons


def test_plain_seed_after_a_baseline_still_fills_analytics(db):
    """A database whose baseline was loaded before seed_analytics existed (Neon): plain `seed` (what db-init runs)
    fills mission_kpis / risk_scores / insights from the kit and leaves flags and events exactly as they are."""
    _setup(db, "seed")                                   # loads the baseline only if this database never had it
    _q(db, "UPDATE flags SET status = 'ACKNOWLEDGED' WHERE flag_id = (SELECT min(flag_id) FROM flags)")
    for t in ("mission_kpis", "risk_scores", "insights"):
        _q(db, "DELETE FROM %s" % t)
    flags = _q(db, "SELECT * FROM flags ORDER BY flag_id")
    events = _q(db, "SELECT * FROM events ORDER BY event_id")
    assert flags and events

    out = _setup(db, "seed")
    assert "skipped" in out
    counts = _counts(out)
    assert (counts["mission_kpis"], counts["risk_scores"], counts["insights"]) == (27, 189, 5)
    assert _q(db, ANALYTICS)[0] == (27, 189, 5)
    assert _q(db, "SELECT * FROM flags ORDER BY flag_id") == flags
    assert _q(db, "SELECT * FROM events ORDER BY event_id") == events

    counts = _counts(_setup(db, "seed"))                 # idempotent: a re-run changes no analytics row
    assert (counts["mission_kpis"], counts["risk_scores"], counts["insights"]) == (0, 0, 0)


def test_db_init_never_force_reseeds():
    """The compose db-init command and job.sh `all` must use the non-destructive seed."""
    compose = (ROOT / "docker-compose.yml").read_text()
    cmd = compose[compose.index("\n  db-init:\n"):compose.index("\n  embed:\n")]
    command = cmd[cmd.index("command:"):]
    assert "db/job.sh seed" in command and "--force-reseed" not in command
    assert "--force-reseed" not in (ROOT / "db" / "job.sh").read_text()
