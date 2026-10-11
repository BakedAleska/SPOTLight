"""db/setup.py and scripts/db_login_roles.py regressions.

Unit tests need no database. The DB tests run on a throwaway database (spot_setuptest_<hex>) created on the server
of SETUP_TEST_ADMIN_URL, else MIGRATION_TEST_URL, else the compose Postgres (localhost:DB_PORT, default 5433), and
skip when none is reachable. It is migrated (twice) and seeded with db/setup.py, then dropped. The login-role test
creates uniquely named roles (roles are cluster-wide) and drops them again.
"""
import ast
import csv
import importlib.util
import json
import os
import socket
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
DB_DIR = Path(__file__).resolve().parents[1]
ROOT = DB_DIR.parent
sys.path.insert(0, str(DB_DIR))
spec = importlib.util.spec_from_file_location("db_setup", DB_DIR / "setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)
spec = importlib.util.spec_from_file_location("db_login_roles", ROOT / "scripts" / "db_login_roles.py")
login_roles = importlib.util.module_from_spec(spec)
spec.loader.exec_module(login_roles)

ADMIN_URL = (os.environ.get("SETUP_TEST_ADMIN_URL") or os.environ.get("MIGRATION_TEST_URL")
             or "postgresql://postgres:%s@localhost:%s/postgres" % (os.environ.get("POSTGRES_PASSWORD", "postgres"),
                                                                     os.environ.get("DB_PORT", "5433")))
KIT_METRIC_VALUES = {"baseline_mission_completion_pct": 94.4, "baseline_action_success_pct": 77.2,
                     "improved_mission_completion_pct": 100.0, "improved_action_success_pct": 91.1,
                     "wp05_navigation_failure_pct": 28.0}


def test_search_function_guc_registered_in_fresh_connection():
    url = os.environ.get("MIGRATION_TEST_URL")
    if not url:
        pytest.skip("Set MIGRATION_TEST_URL to an isolated database with pgvector >= 0.8")
    with psycopg.connect(url, prepare_threshold=None) as conn:
        version = setup._pgvector_version(conn)
        if not version or version < (0, 8):
            pytest.skip("pgvector >= 0.8 required")
        sql = setup._render(
            "CREATE FUNCTION pg_temp.migration_guc_probe() RETURNS integer "
            "LANGUAGE sql {{HNSW_SET}} AS 'SELECT 1'", conn)
        conn.execute(sql)
        assert conn.execute("SELECT pg_temp.migration_guc_probe()").fetchone() == (1,)
        conn.rollback()


# ── unit (no database) ───────────────────────────────────────────────────────────────────────────
def _kit(name: str, key: str) -> dict:
    with open(setup.KIT_DIR / name, newline="", encoding="utf-8") as f:
        return {r[key]: r for r in csv.DictReader(f)}


def _vision_samples() -> dict:
    tree = ast.parse((ROOT / "amd" / "vision" / "scripts" / "run_kit.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "SAMPLES" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("SAMPLES not found in amd/vision/scripts/run_kit.py")


def test_media_hints_match_the_vision_sample_map():
    samples = _vision_samples()
    assert {Path(n).stem for n in samples} == set(setup.MEDIA_HINTS)
    for name, action_type in samples.items():
        hints = setup.MEDIA_HINTS[Path(name).stem]
        assert hints["action_type"] == action_type, name
        assert hints["inspection_hint"] in ("acoustic", "gauge", "thermal", "visual"), name
        assert hints["subject_hint"] and "_" not in hints["subject_hint"], name
    assert setup.MEDIA_HINTS["pipe_gallery_01"]["location_hint"] == "WP05"
    assert _kit("locations.csv", "location_id")["WP05"]["location_name"] == "Pipe Gallery"
    if setup.MEDIA_DIR.exists():
        stems = {p.stem for p in list(setup.MEDIA_DIR.glob("*.jpg")) + list(setup.MEDIA_DIR.glob("*.png"))}
        assert stems <= set(setup.MEDIA_HINTS), "kit image without a MEDIA_HINTS entry: %s" % (
            stems - set(setup.MEDIA_HINTS))


def test_seed_events_carry_the_ingest_enriched_block(monkeypatch):
    captured = {}
    monkeypatch.setattr(setup, "_upsert", lambda conn, table, key, rows: captured.setdefault(table, rows) and 0)
    setup.seed_events(None, {})                # no connection needed: every write goes through _upsert
    events = {r["event_id"]: (r, json.loads(r["payload"])) for r in captured["events"]}
    assert len(events) == 500
    for eid, (row, payload) in events.items():
        if row["action_id"]:
            e = payload["enriched"]
            assert list(e) == ["action", "location", "asset"], eid
            assert tuple(e["action"]) == setup.ENRICHED_ACTION_KEYS, eid
            assert tuple(e["location"]) == setup.ENRICHED_LOCATION_KEYS, eid
            assert tuple(e["asset"]) == setup.ENRICHED_ASSET_KEYS, eid
            assert e["action"]["location_id"] == row["location_id"] and e["action"]["asset_id"] == row["asset_id"]
        else:
            assert "enriched" not in payload, eid            # mission events: Reference.normalize adds none
    # The exact block backend/app/services/reference.py Reference.normalize writes for A0001 (kit rows, typed).
    a, loc, asset = (_kit("actions.csv", "action_id")["A0001"], _kit("locations.csv", "location_id")["WP05"],
                     _kit("assets.csv", "asset_id")["HX-201"])
    want = {"action": {"sequence": 1, "action_type": "NAVIGATION", "location_id": "WP05", "asset_id": "HX-201",
                       "battery_pct": 75, "status": "FAILED", "failure_type": "OBSTACLE_DETECTION",
                       "duration_sec": int(a["duration_sec"])},
            "location": {k: loc[k] for k in ("location_name", "zone_type", "environment")},
            "asset": {k: asset[k] for k in ("asset_name", "asset_type", "criticality")}}
    assert events["E00002"][1]["enriched"] == want                 # ACTION_STARTED
    assert events["E00003"][1]["enriched"] == want                 # ACTION_FAILED
    assert events["E00003"][1]["failure_type"] == "OBSTACLE_DETECTION"          # details are kept as sent
    assert events["E00005"][1]["enriched"]["action"]["failure_type"] is None     # "" in the replay -> None


def test_login_roles_host_check(monkeypatch):
    monkeypatch.delenv("PGHOST", raising=False)
    monkeypatch.delenv("PGHOSTADDR", raising=False)
    for url in ("postgresql://u:p@localhost:5432/spot", "postgresql://u:p@db:5432/spot",
                "postgresql://u:p@127.0.0.1/spot", "host=/tmp dbname=spot", "dbname=spot",
                "postgresql://u:p@[::1]:5432/spot", "host=localhost hostaddr=127.0.0.1 dbname=spot"):
        assert login_roles.is_local(url), url
    for url in ("postgresql://u:p@ep-x-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require",
                "host=db.example.com dbname=spot", "postgresql://u:p@host.docker.internal:5432/spot",
                "postgresql://u:p@localhost,ep-x.neon.tech/spot",
                "host=localhost hostaddr=52.1.2.3 dbname=spot",                  # hostaddr is where libpq connects
                "postgresql://u:p@localhost/spot?hostaddr=10.0.0.5"):
        assert not login_roles.is_local(url), url
    monkeypatch.setenv("PGHOST", "ep-x.neon.tech")
    assert not login_roles.is_local("dbname=spot")                       # no host in the URL: libpq uses PGHOST
    monkeypatch.delenv("PGHOST")
    monkeypatch.setenv("PGHOSTADDR", "52.1.2.3")
    assert not login_roles.is_local("dbname=spot")


# ── integration: throwaway database ──────────────────────────────────────────────────────────────
def _env(url, **extra):
    env = {k: v for k, v in os.environ.items() if k not in ("LOGS_DATABASE_URL", "SPOT_ROTATE_PASSWORDS")}
    env["DATABASE_URL"] = url
    env.update(extra)
    return env


def _run(script, url, *args, check=True, **extra):
    r = subprocess.run([sys.executable, str(script), *args], env=_env(url, **extra), capture_output=True, text=True,
                       timeout=900)
    if check:
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    return r


@pytest.fixture(scope="module")
def admin():
    try:
        conn = psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3)
    except psycopg.Error as exc:
        pytest.skip("no Postgres at %s (make up, or set SETUP_TEST_ADMIN_URL): %s" % (ADMIN_URL.split("@")[-1], exc))
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def seeded(admin):
    name = "spot_setuptest_" + uuid.uuid4().hex[:8]
    try:
        admin.execute("CREATE DATABASE %s" % name)
    except psycopg.Error as exc:
        pytest.skip("cannot create a throwaway database: %s" % exc)
    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + name
    try:
        _run(setup.__file__, url, "migrate")
        _run(setup.__file__, url, "migrate")                       # every migration is idempotent
        out = _run(setup.__file__, url, "seed").stdout
        counts = json.loads(out[out.index("{"):])
        yield url, counts
    finally:
        admin.execute("DROP DATABASE IF EXISTS %s WITH (FORCE)" % name)


def _q(url, sql, *args):
    with psycopg.connect(url, autocommit=True) as c:
        cur = c.execute(sql, args)
        return cur.fetchall() if cur.description else []


def test_migrations_schema_hygiene(seeded):
    url, _ = seeded
    assert _q(url, "SELECT to_regclass('public.anomaly_briefs')")[0][0] is None
    cols = {r[0] for r in _q(url, "SELECT column_name FROM information_schema.columns "
                                  "WHERE table_schema = 'public' AND table_name = 'assessments'")}
    assert {"fallback_reason", "source"} <= cols
    names = ["flags_status_check", "flags_severity_check", "flags_priority_check", "assessments_state_check",
             "acknowledgements_action_check", "system1_predictions_event_kind_check", "actions_failure_type_iff_failed"]
    checks = dict(_q(url, "SELECT conname, convalidated FROM pg_constraint WHERE conname = ANY(%s)", names))
    assert len(checks) == 7 and all(checks.values()), checks
    idx = ["flags_event_idx", "flags_mission_idx", "live_captures_flag_idx"]
    assert {r[0] for r in _q(url, "SELECT indexname FROM pg_indexes WHERE indexname = ANY(%s)", idx)} == set(idx)
    assert _q(url, "SELECT count(*) FROM pg_description d JOIN pg_class c ON c.oid = d.objoid WHERE c.relname IN "
                   "('locations','assets','missions','actions','readings','anomalies') AND d.objsubid > 0")[0][0] == 56
    reset = (DB_DIR / "reset.sql").read_text()
    assert "anomaly_briefs" not in reset and "replay_runs" in reset


def test_seed_analytics_fills_and_is_idempotent(seeded):
    url, counts = seeded
    assert (counts["mission_kpis"], counts["risk_scores"], counts["insights"]) == (27, 189, 5)
    assert _q(url, "SELECT count(*), sum(total_actions), sum(failed_actions) FROM mission_kpis")[0] == (27, 270, 49)
    assert _q(url, "SELECT count(*) FROM mission_kpis k JOIN (SELECT mission_id, count(*) n, count(*) FILTER "
                   "(WHERE status = 'SUCCESS') ok FROM actions GROUP BY 1) a USING (mission_id) "
                   "WHERE k.true_health <> round(100.0 * a.ok / a.n, 1)::real")[0][0] == 0
    assert _q(url, "SELECT count(*), sum(n_observed) FROM risk_scores")[0] == (189, 270)
    rows = {k: (v, d, s) for k, v, d, s in _q(url, "SELECT key, value, detail, source FROM insights")}
    assert set(rows) == set(KIT_METRIC_VALUES)
    for key, want in KIT_METRIC_VALUES.items():
        value, detail, source = rows[key]
        assert value == pytest.approx(want) and detail["sql_value"] == pytest.approx(want), key
        assert detail["unit"] == "%" and detail["validation_status"] == "PASS" and detail["generated_at"]
        assert source == "kit:validation_results.json"
    # A second run changes 0 rows.
    with psycopg.connect(url, autocommit=True) as c:
        again = {}
        setup.seed_analytics(c, again)
    assert again == {"mission_kpis": 0, "risk_scores": 0, "risk_scores_removed": 0, "insights": 0}
    # Drifted rows are put back and stale risk groups removed.
    _q(url, "UPDATE mission_kpis SET true_health = 1 WHERE mission_id = 'M001'")
    _q(url, "DELETE FROM insights WHERE key = 'wp05_navigation_failure_pct'")
    _q(url, "INSERT INTO risk_scores VALUES ('WP99', 'NAVIGATION', '<=25', 'V1', 1, 1)")
    with psycopg.connect(url, autocommit=True) as c:
        fixed = {}
        setup.seed_analytics(c, fixed)
    assert fixed == {"mission_kpis": 1, "risk_scores": 0, "risk_scores_removed": 1, "insights": 1}


def test_seed_raw_media_metadata(seeded):
    url, _ = seeded
    rows = _q(url, "SELECT media_id, action_id, mission_id, captured_at, metadata FROM raw.inspection_media")
    assert len(rows) == len(setup.MEDIA_HINTS)
    for media_id, action_id, mission_id, captured_at, meta in rows:
        assert (action_id, mission_id, captured_at) == (None, None, None), media_id   # synthetic, reused images
        hints = setup.MEDIA_HINTS[media_id[len("sample:"):]]
        assert {k: meta.get(k) for k in hints} == hints, media_id
        assert meta["synthetic"] is True


def test_status_lists_analytics_and_run_tables(seeded):
    url, _ = seeded
    out = _run(setup.__file__, url, "status").stdout
    got = {ln.split()[0]: int(ln.split()[1]) for ln in out.splitlines() if len(ln.split()) >= 2
           and ln.split()[1].isdigit()}
    assert {k: got.get(k) for k in ("mission_kpis", "risk_scores", "insights")} == {
        "mission_kpis": 27, "risk_scores": 189, "insights": 5}, out
    assert {"replay_runs", "live_runs", "live_captures"} <= set(got), out


def test_reset_drops_replay_runs_and_legacy_anomaly_briefs(admin):
    """reset.sql drops replay_runs (010); a legacy anomaly_briefs is gone after the re-migrate (014 drops it)."""
    name = "spot_setuptest_" + uuid.uuid4().hex[:8]
    try:
        admin.execute("CREATE DATABASE %s" % name)
    except psycopg.Error as exc:
        pytest.skip("cannot create a throwaway database: %s" % exc)
    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + name
    try:
        _run(setup.__file__, url, "migrate")
        _q(url, "INSERT INTO replay_runs (state, event_ids) VALUES ('finished', ARRAY['E00001'])")
        _q(url, "CREATE TABLE anomaly_briefs (anomaly_id text PRIMARY KEY, brief text)")   # the old 001 table
        _run(setup.__file__, url, "reset", "--yes")
        assert _q(url, "SELECT count(*) FROM replay_runs")[0][0] == 0
        assert _q(url, "SELECT to_regclass('public.anomaly_briefs')")[0][0] is None
    finally:
        admin.execute("DROP DATABASE IF EXISTS %s WITH (FORCE)" % name)


def test_seed_steps_run_inside_the_callers_transaction(seeded):
    """A baseline restore runs seed_events + seed_flags + seed_analytics in ONE transaction: none may commit."""
    url, _ = seeded
    snapshot = ("SELECT (SELECT count(*) FROM flags), (SELECT count(*) FROM events), "
                "(SELECT max(synced_at) FROM mission_kpis)")
    before = _q(url, snapshot)[0]
    with psycopg.connect(url, prepare_threshold=None) as conn:      # autocommit off
        with conn.transaction() as tx:
            conn.execute("DELETE FROM flags")
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM mission_kpis")
            counts = {}
            setup.seed_events(conn, counts)
            setup.seed_flags(conn, counts)
            setup.seed_analytics(conn, counts)
            assert (counts["events"], counts["flags_written"], counts["mission_kpis"]) == (500, 58, 27)
            assert conn.execute("SELECT count(*) FROM events WHERE payload ? 'enriched'").fetchone()[0] == 455
            assert conn.execute("SELECT count(*) FROM flags WHERE event_id IS NULL").fetchone()[0] == 7
            assert conn.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS
            raise psycopg.Rollback(tx)
    assert _q(url, snapshot)[0] == before


def test_hybrid_search_ranks_exact_ids_in_a_question(seeded):
    url, _ = seeded

    def vec(hot):
        return "[" + ",".join("1" if i == hot else "0" for i in range(1024)) + "]"

    near, far = vec(0), vec(1)
    with psycopg.connect(url, prepare_threshold=None) as conn:
        with conn.transaction() as tx:
            for doc_id, title, content, emb in [
                    ("t:212", "Anomaly on PANEL-212", "Hot spot on PANEL-212 in Turbine Hall South: anomalies found",
                     near),
                    ("t:211", "Anomaly on PANEL-211", "Loose terminal on PANEL-211", far),
                    ("t:a61", "M007 step 1", "navigation PANEL-203 at WP05 FAILED, action A0061", near)]:
                conn.execute("INSERT INTO documents (doc_id, kind, title, content, embedding) "
                             "VALUES (%s, 'incident', %s, %s, %s::vector)", (doc_id, title, content, emb))
            q = ("SELECT doc_id, text_match FROM match_documents_hybrid(%s::vector, %s, ARRAY['incident'], "
                 "match_count => 3)")
            got = conn.execute(q, (near, "What anomalies were found on PANEL-211 in Turbine Hall South?")).fetchall()
            assert got[0] == ("t:211", True) and ("t:212", False) in got     # the id beats two closer vectors
            got = conn.execute(q, (far, "Why did action A0061 fail at WP05?")).fetchall()
            assert got[0] == ("t:a61", True)
            got = conn.execute(q, (near, "general question with no ids")).fetchall()
            assert got[0][0] == "t:212"                              # no id: plain RRF, unchanged behaviour
            raise psycopg.Rollback(tx)


def test_login_roles_create_keep_rotate_and_refuse_dev_defaults(seeded, admin):
    url, _ = seeded
    script = ROOT / "scripts" / "db_login_roles.py"
    suffix = uuid.uuid4().hex[:8]
    app_user, mcp_user, new_user = "spot_app_t" + suffix, "spot_mcp_t" + suffix, "spot_app_r" + suffix
    users = dict(SPOT_APP_USER=app_user, SPOT_MCP_USER=mcp_user)

    def can_login(user, pw):
        try:
            with psycopg.connect(url, user=user, password=pw, connect_timeout=3):
                return True
        except psycopg.OperationalError:
            return False

    try:
        r = _run(script, url, SPOT_APP_PASSWORD="first-" + suffix, SPOT_MCP_PASSWORD="mcp-" + suffix, **users)
        assert "created %s in spot_app" % app_user in r.stdout
        assert can_login(app_user, "first-" + suffix)
        # A re-run with another password keeps the existing one (db-init runs this on every `up`).
        r = _run(script, url, SPOT_APP_PASSWORD="second-" + suffix, SPOT_MCP_PASSWORD="mcp-" + suffix, **users)
        assert "kept the existing password of %s" % app_user in r.stdout
        assert can_login(app_user, "first-" + suffix) and not can_login(app_user, "second-" + suffix)
        # SPOT_ROTATE_PASSWORDS=1 is the explicit way to change it.
        _run(script, url, SPOT_APP_PASSWORD="second-" + suffix, SPOT_MCP_PASSWORD="mcp-" + suffix,
             SPOT_ROTATE_PASSWORDS="1", **users)
        assert can_login(app_user, "second-" + suffix)
        # An existing NOLOGIN user gets LOGIN back, and still keeps its password.
        admin.execute("ALTER ROLE %s NOLOGIN" % app_user)
        r = _run(script, url, SPOT_APP_PASSWORD="third-" + suffix, SPOT_MCP_PASSWORD="mcp-" + suffix, **users)
        assert "restored LOGIN" in r.stdout
        assert can_login(app_user, "second-" + suffix) and not can_login(app_user, "third-" + suffix)
        # The public dev defaults are refused on a non-local host (the same server, under a non-local host name).
        addr = socket.gethostbyname(psycopg.conninfo.conninfo_to_dict(url).get("host") or "localhost")
        remote = psycopg.conninfo.make_conninfo(url, host="db.example.com", hostaddr=addr)
        r = _run(script, remote, check=False, SPOT_APP_USER=new_user, SPOT_APP_PASSWORD="spot_app_dev",
                 SPOT_MCP_USER=mcp_user, SPOT_MCP_PASSWORD="spot_mcp_dev", SPOT_ROTATE_PASSWORDS="1")
        assert r.returncode == 1, r.stdout + r.stderr
        assert "refusing to create %s" % new_user in r.stderr
        assert "refusing to change the password of %s" % mcp_user in r.stderr
        assert not admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (new_user,)).fetchone()
        assert can_login(mcp_user, "mcp-" + suffix)
        for secret in ("db.example.com", "spot_app_dev", "spot_mcp_dev"):
            assert secret not in r.stdout + r.stderr
        # A user name that is a group role is refused: the NOLOGIN group must never become a login.
        r = _run(script, url, check=False, SPOT_APP_USER="spot_app", SPOT_APP_PASSWORD="x-" + suffix,
                 SPOT_MCP_USER=mcp_user, SPOT_MCP_PASSWORD="mcp-" + suffix)
        assert r.returncode == 1 and "group role" in r.stderr, r.stdout + r.stderr
        assert admin.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = 'spot_app'").fetchone() == (False,)
    finally:
        for u in (app_user, mcp_user, new_user):
            admin.execute("DROP ROLE IF EXISTS %s" % u)
