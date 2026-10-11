#!/usr/bin/env python3
"""Neon / local Postgres setup for SpotTheDOT.

  python db/setup.py migrate [--hnsw] [--recreate-documents]   apply db/migrations/*.sql (idempotent)
  python db/setup.py seed [--no-events] [--force-reseed]       load the kit into public.* and raw.*, seed flags
                                                               (non-destructive; see seed() for --force-reseed)
  python db/setup.py reset --yes [--include-logs]              drop public.* (and raw/app with --include-logs), re-migrate
  python db/setup.py status                                    row counts
  (the knowledge graph is built by db/graph_build.py, after ingest; `db/job.sh graph`)

Env: DATABASE_URL (required), LOGS_DATABASE_URL (raw.* + app.*; defaults to DATABASE_URL), EMBED_DIM (default 1024).
Works unchanged against Neon (sslmode=require is added for *.neon.tech URLs) and a local Postgres.
"""
import argparse
import csv
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import spotdb  # noqa: E402  (also puts amd/system1 on sys.path for the triage package)
from spotdb import REPO, KIT_DIR, MEDIA_DIR, connect, database_url, logs_database_url, job_run  # noqa: E402

MIGRATIONS = spotdb.DB_DIR / "migrations"
KIT_CSVS = ["locations.csv", "assets.csv", "missions.csv", "actions.csv", "readings.csv", "anomalies.csv"]


# ── migrate ─────────────────────────────────────────────────────────────────────────────────────
def _target(sql: str) -> str:
    m = re.search(r"^--\s*target:\s*(main|logs|both)\s*$", sql, re.M)
    return m.group(1) if m else "main"


def _vec_config():
    dim = int(os.environ.get("EMBED_DIM", "1024"))
    # pgvector index limits: vector <= 2000 dims, halfvec <= 4000. Exact search (our default) has no limit.
    vec_type = "halfvec" if 2000 < dim <= 4000 else "vector"
    return dim, vec_type, vec_type + "_cosine_ops"


def _pgvector_version(conn):
    row = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
    return tuple(int(x) for x in re.findall(r"\d+", row[0])[:3]) if row else None


def _render(sql: str, conn) -> str:
    dim, vec_type, vec_ops = _vec_config()
    ver = _pgvector_version(conn)
    if ver and ver >= (0, 8):
        # CREATE EXTENSION IF NOT EXISTS does not load pgvector in a fresh session.
        # Register its user-settable GUCs before CREATE FUNCTION ... SET validates them.
        conn.execute("SELECT '[0]'::vector")
    hnsw_set = "SET hnsw.iterative_scan = strict_order" if ver and ver >= (0, 8) else ""
    return (sql.replace("{{EMBED_DIM}}", str(dim)).replace("{{VEC_TYPE}}", vec_type)
            .replace("{{VEC_OPS}}", vec_ops).replace("{{HNSW_SET}}", hnsw_set))


def _embedding_type(conn, table: str):
    row = conn.execute("SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                       "WHERE attrelid = to_regclass(%s) AND attname = 'embedding'", (table,)).fetchone()
    return row[0] if row else None


def _drop_graph(conn, why: str) -> None:
    # The graph (009_graph.sql) is derived data: `db/job.sh graph` rebuilds it in seconds.
    print("dropping kg_edges + kg_nodes (%s); rebuild with `db/job.sh graph`" % why)
    conn.execute("DROP TABLE IF EXISTS kg_edges, kg_nodes CASCADE")


def _check_documents_dim(conn, recreate: bool) -> None:
    dim, vec_type, _ = _vec_config()
    want = "%s(%d)" % (vec_type, dim)
    have = _embedding_type(conn, "public.documents")
    if have is not None and have != want:
        if not recreate:
            sys.exit("documents.embedding is %s but EMBED_DIM=%d wants %s. Re-run with `migrate --recreate-documents` "
                     "(drops the RAG store; re-run ingest after) or fix EMBED_DIM." % (have, dim, want))
        _drop_graph(conn, "documents is recreated")      # else the CASCADE would silently drop kg_nodes' FK
        print("dropping documents (%s -> %s)" % (have, want))
        conn.execute("DROP TABLE documents CASCADE")
    have = _embedding_type(conn, "public.kg_nodes")
    if have is not None and have != want:
        _drop_graph(conn, "kg_nodes.embedding is %s, EMBED_DIM wants %s" % (have, want))


def _apply(conn, path: Path) -> None:
    sql = _render(path.read_text(), conn)
    with conn.transaction():
        conn.execute(sql)


def migrate(args) -> None:
    main_url, logs_url = database_url(), logs_database_url()
    same = main_url == logs_url
    main = connect(main_url, autocommit=True)
    main.add_notice_handler(lambda d: print("  notice: %s" % d.message_primary))
    logs = main if same else connect(logs_url, autocommit=True)
    if not same:
        logs.add_notice_handler(lambda d: print("  notice (logs): %s" % d.message_primary))
    print("main DB: %s\nlogs DB: %s" % (spotdb.redact(main_url), "same" if same else spotdb.redact(logs_url)))
    _check_documents_dim(main, args.recreate_documents)
    files = sorted(MIGRATIONS.glob("*.sql"))
    if args.hnsw:
        files.append(MIGRATIONS / "optional" / "hnsw.sql")
    for f in files:
        target = _target(f.read_text())
        conns = {"main": [main], "logs": [logs], "both": [main] if same else [main, logs]}[target]
        for c in conns:
            _apply(c, f)
        print("applied %-32s -> %s" % (f.name, target))
    dim, vec_type, _ = _vec_config()
    print("pgvector %s, documents.embedding %s(%d), search: %s" % (
        ".".join(map(str, _pgvector_version(main) or ())), vec_type, dim,
        "HNSW (approximate)" if args.hnsw or spotdb.has_table(main, "public.documents_embedding_hnsw") else "exact"))
    main.close()
    if not same:
        logs.close()


# ── seed ────────────────────────────────────────────────────────────────────────────────────────
def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rel(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(REPO))
    except ValueError:
        return str(p)


def _rows(name: str):
    with open(KIT_DIR / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _n(v, cast):
    return None if v in ("", None) else cast(float(v)) if cast is int else cast(v)


def _bool(v):
    return None if v in ("", None) else v.strip().lower() in ("true", "1", "t", "yes")


# Audit M5: by default every seed write is insert-if-missing (ON CONFLICT DO NOTHING), so a re-run never changes
# a row that exists. --force-reseed sets FORCE and restores the old destructive upserts (DO UPDATE).
FORCE = False


def _upsert(conn, table: str, key: str, rows: list) -> int:
    """Insert rows; on a key conflict update them only with --force-reseed. Returns the rows inserted or updated."""
    if not rows:
        return 0
    cols = list(rows[0].keys())
    if FORCE:
        upd = ", ".join("%s = EXCLUDED.%s" % (c, c) for c in cols if c != key)
        conflict = "DO UPDATE SET %s" % upd
    else:
        conflict = "DO NOTHING"
    sql = "INSERT INTO %s (%s) VALUES (%s) ON CONFLICT (%s) %s" % (
        table, ", ".join(cols), ", ".join(["%s"] * len(cols)), key, conflict)
    with conn.cursor() as cur:
        cur.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
        return max(cur.rowcount, 0)    # psycopg >= 3.1: the total over all rows


def seed_core(conn, counts: dict) -> None:
    counts["locations"] = _upsert(conn, "locations", "location_id", [{
        "location_id": r["location_id"], "location_name": r["location_name"], "zone_type": r["zone_type"],
        "floor": _n(r["floor"], int), "x_coord": _n(r["x_coord"], float), "y_coord": _n(r["y_coord"], float),
        "environment": r["environment"]} for r in _rows("locations.csv")])
    counts["assets"] = _upsert(conn, "assets", "asset_id", [{
        "asset_id": r["asset_id"], "asset_name": r["asset_name"], "asset_type": r["asset_type"],
        "location_id": r["location_id"], "criticality": r["criticality"]} for r in _rows("assets.csv")])
    counts["missions"] = _upsert(conn, "missions", "mission_id", [{
        "mission_id": r["mission_id"], "mission_name": r["mission_name"], "scenario": r["scenario"],
        "configuration_version": r["configuration_version"], "start_time": r["start_time"] or None,
        "end_time": r["end_time"] or None, "duration_min": _n(r["duration_min"], int),
        "start_battery_pct": _n(r["start_battery_pct"], int), "end_battery_pct": _n(r["end_battery_pct"], int),
        "mission_status": r["mission_status"], "operator_mode": r["operator_mode"]} for r in _rows("missions.csv")])
    counts["actions"] = _upsert(conn, "actions", "action_id", [{
        "action_id": r["action_id"], "mission_id": r["mission_id"], "sequence": _n(r["sequence"], int),
        "timestamp": r["timestamp"] or None, "location_id": r["location_id"], "asset_id": r["asset_id"],
        "action_type": r["action_type"], "battery_pct": _n(r["battery_pct"], int), "status": r["status"],
        "failure_type": r["failure_type"] or None, "duration_sec": _n(r["duration_sec"], int),
        "scenario": r["scenario"], "configuration_version": r["configuration_version"]} for r in _rows("actions.csv")])
    counts["readings"] = _upsert(conn, "readings", "reading_id", [{
        "reading_id": r["reading_id"], "action_id": r["action_id"], "mission_id": r["mission_id"],
        "asset_id": r["asset_id"], "location_id": r["location_id"], "timestamp": r["timestamp"] or None,
        "measurement_type": r["measurement_type"], "value": _n(r["value"], float), "unit": r["unit"],
        "quality": r["quality"]} for r in _rows("readings.csv")])
    counts["anomalies"] = _upsert(conn, "anomalies", "anomaly_id", [{
        "anomaly_id": r["anomaly_id"], "action_id": r["action_id"], "mission_id": r["mission_id"],
        "location_id": r["location_id"], "asset_id": r["asset_id"], "timestamp": r["timestamp"] or None,
        "anomaly_type": r["anomaly_type"], "severity": r["severity"],
        "operator_review_required": _bool(r["operator_review_required"]), "description": r["description"]}
        for r in _rows("anomalies.csv")])


def _replay_lines():
    with open(KIT_DIR / "replay.ndjson", "rb") as f:
        return [ln.rstrip(b"\r\n") for ln in f if ln.strip()]


def seed_events(conn, counts: dict) -> None:
    actions = {r["action_id"]: r for r in _rows("actions.csv")}
    readings = {r["action_id"]: r for r in _rows("readings.csv")}
    anomalies = {r["action_id"]: r for r in _rows("anomalies.csv")}
    rows = []
    for ln in _replay_lines():
        e = json.loads(ln)
        d = dict(e.get("details") or {})
        aid = d.get("action_id")
        a = actions.get(aid) if aid else None
        payload = dict(d)
        if e["event_type"] in ("ACTION_COMPLETED", "ACTION_FAILED") and aid:
            if aid in readings:
                r = readings[aid]
                payload["reading"] = {"reading_id": r["reading_id"], "measurement_type": r["measurement_type"],
                                      "value": float(r["value"]), "unit": r["unit"], "quality": r["quality"]}
            if aid in anomalies:
                an = anomalies[aid]
                payload["anomaly"] = {"anomaly_id": an["anomaly_id"], "anomaly_type": an["anomaly_type"],
                                      "severity": an["severity"], "description": an["description"]}
        batt = d.get("battery_pct", d.get("end_battery_pct"))
        if batt is None and a:
            batt = _n(a["battery_pct"], int)
        rows.append({
            "event_id": e["event_id"], "mission_id": e["mission_id"], "ts": e["timestamp"],
            "event_type": e["event_type"], "action_id": aid,
            "location_id": d.get("location_id") or (a["location_id"] if a else None),
            "asset_id": d.get("asset_id") or (a["asset_id"] if a else None),
            "battery_pct": batt, "payload": json.dumps(payload)})
    counts["events"] = _upsert(conn, "events", "event_id", rows)


def seed_flags(conn, counts: dict) -> None:
    """One flag per kit action/mission event the deterministic policy escalates (P1/P2)."""
    import policy_rules as PR
    from triage.events import mission_events
    from triage.kit import load_kit
    from triage.policy import label_action
    kit = load_kit(kit_dir=KIT_DIR)
    replay = {}
    for event_id, event_type, action_id, mission_id in conn.execute(
            "SELECT event_id, event_type, action_id, mission_id FROM events "
            "WHERE event_type IN ('ACTION_COMPLETED','ACTION_FAILED','MISSION_COMPLETED')"):
        replay[("act-" + action_id) if action_id else ("mis-" + mission_id)] = event_id
    rows, by_prio, triaged = [], {}, 0
    for mid, mission in kit.missions.items():
        for ev in mission_events(mission, kit.actions_of(mid), kit.locations, kit.assets, kit.readings,
                                 kit.anomalies, source="kit"):
            if ev["kind"] not in ("action", "mission"):
                continue
            triaged += 1
            res = PR.evaluate(ev)
            PR.check_against_policy(ev, res)
            by_prio[res["priority"]] = by_prio.get(res["priority"], 0) + 1
            if res["priority"] not in ("P1", "P2"):
                continue
            is_action = ev["kind"] == "action"
            category = label_action(ev)["category"] if is_action else "mission"
            top = res["reasons"][0]["detail"]
            if is_action:
                subj = "%s %s at %s" % (ev["action_type"].replace("_", " ").lower(), ev["asset"]["asset_id"],
                                        ev["location"]["location_id"])
            else:
                subj = "mission %s" % mid
            title = "%s %s · %s: %s" % (res["priority"], category, subj, top)
            rows.append({
                "triage_event_id": ev["event_id"], "event_id": replay.get(ev["event_id"]), "mission_id": mid,
                "action_id": ev.get("action_id"),
                "location_id": ev["location"]["location_id"] if is_action else None,
                "asset_id": ev["asset"]["asset_id"] if is_action else None,
                "score": res["score"], "severity": res["severity"], "title": title[:200],
                "reasons": json.dumps(res["reasons"]), "escalated": True, "priority": res["priority"],
                "category": category})
    # Default: insert-if-missing. A flag that exists (live ingest, operator acks, or an earlier seed) is never
    # touched. --force-reseed overwrites seeded columns (status and acks are kept).
    counts["flags_written"] = _upsert(conn, "flags", "triage_event_id", rows)
    counts["flags_escalated"] = len(rows)
    counts["triaged_events"] = triaged
    counts["priority_counts"] = dict(sorted(by_prio.items()))


def seed_raw(logs, counts: dict) -> None:
    """raw.*: kit files and rows verbatim, replay lines verbatim, sample inspection media. Append-only, idempotent."""
    files = rows = 0
    for name in KIT_CSVS:
        path = KIT_DIR / name
        data = path.read_bytes()
        with open(path, newline="", encoding="utf-8") as f:
            recs = list(csv.DictReader(f))
        fname, sha = _rel(path), _sha256(data)
        with logs.transaction():
            r = logs.execute("INSERT INTO raw.kit_files (file_name, sha256, row_count) VALUES (%s, %s, %s) "
                             "ON CONFLICT (file_name, sha256) DO NOTHING RETURNING file_id",
                             (fname, sha, len(recs))).fetchone()
            if r is None:
                continue                      # this exact file was already imported
            with logs.cursor() as cur:
                cur.executemany("INSERT INTO raw.kit_rows (file_id, row_num, data) VALUES (%s, %s, %s) "
                                "ON CONFLICT DO NOTHING", [(r[0], i, json.dumps(rec)) for i, rec in enumerate(recs, 1)])
        files += 1
        rows += len(recs)
    counts["raw_kit_files_new"], counts["raw_kit_rows_new"] = files, rows

    before = logs.execute("SELECT count(*) FROM raw.spot_event_log WHERE source = 'replay.ndjson'").fetchone()[0]
    recs = []
    for ln in _replay_lines():
        e = json.loads(ln)
        recs.append(("replay.ndjson", e.get("event_id"), e.get("mission_id"), e.get("event_type"),
                     e.get("timestamp"), ln.decode("utf-8"), _sha256(ln)))
    with logs.transaction(), logs.cursor() as cur:
        cur.executemany("INSERT INTO raw.spot_event_log (source, event_id, mission_id, event_type, event_ts, payload, "
                        "sha256) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s) ON CONFLICT (source, sha256) DO NOTHING",
                        recs)
    after = logs.execute("SELECT count(*) FROM raw.spot_event_log WHERE source = 'replay.ndjson'").fetchone()[0]
    counts["raw_spot_events_new"] = after - before

    media = []
    for p in sorted(MEDIA_DIR.glob("*.jpg")) + sorted(MEDIA_DIR.glob("*.png")):
        data = p.read_bytes()
        inspection, _, subject = p.stem.partition("_")
        media.append(("sample:" + p.stem, None, None, "image/jpeg" if p.suffix == ".jpg" else "image/png",
                      _rel(p), _sha256(data), len(data), None, json.dumps({
                          "synthetic": True, "source": "NextEra kit 04_ASSETS/sample_inspection_images",
                          "inspection_hint": inspection, "subject_hint": subject.replace("_", " "),
                          "note": "synthetic example imagery; not linked to a kit action"})))
    with logs.transaction(), logs.cursor() as cur:
        cur.executemany("INSERT INTO raw.inspection_media (media_id, action_id, mission_id, media_type, uri, sha256, "
                        "bytes, captured_at, metadata) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (media_id) DO NOTHING", media)
    counts["raw_media"] = len(media)


def demo_baseline_seeded() -> bool:
    """True once a seed run has loaded the demo baseline (events + flags) into this database: a finished `seed`
    job in app.job_runs that counted flags. After that, plain `seed` leaves events and flags alone, so a
    `docker compose up` (db-init) never refills a demo that was reset or is running live (audit M5)."""
    try:
        logs = connect(logs_database_url(), autocommit=True)
    except Exception:  # noqa: BLE001 - no logs DB yet: treat as a fresh database
        return False
    try:
        if not spotdb.has_table(logs, "app.job_runs"):
            return False
        return logs.execute("SELECT EXISTS (SELECT 1 FROM app.job_runs WHERE job = 'seed' AND status = 'ok' "
                            "AND counts ? 'flags_escalated')").fetchone()[0]
    finally:
        logs.close()


def seed(args) -> None:
    """Non-destructive by default (audit M5; db-init runs this on every `docker compose up`):
      - kit tables and raw.*: insert-if-missing, existing rows are never updated;
      - the demo baseline (events from replay.ndjson + the seeded flags) is loaded only the first time this
        database is seeded, and then also insert-if-missing: live flags, acknowledged flags and a reset demo
        are left as they are.
    --force-reseed is the explicit destructive path (old behaviour): upsert the kit, events and flags over existing
    rows (flag status/acks kept, reasons/score/escalated overwritten) even if the baseline was loaded before.
    db-init never passes it."""
    global FORCE
    FORCE = bool(args.force_reseed)
    baseline = FORCE or not demo_baseline_seeded()
    meta = {"kit_dir": _rel(KIT_DIR), "events": not args.no_events, "force_reseed": FORCE}
    with job_run("seed", meta) as run:
        main = connect(autocommit=True)
        with main.transaction():
            seed_core(main, run.counts)
            if baseline:
                if not args.no_events:
                    seed_events(main, run.counts)
                seed_flags(main, run.counts)
            else:
                run.counts["demo_baseline"] = ("skipped: already seeded once; events and flags left as they are "
                                               "(use --force-reseed to overwrite)")
                print(run.counts["demo_baseline"])
        main.close()
        logs = connect(logs_database_url(), autocommit=True)
        seed_raw(logs, run.counts)
        logs.close()
        print(json.dumps(run.counts, indent=1))


# ── reset / status ──────────────────────────────────────────────────────────────────────────────
def reset(args) -> None:
    if not args.yes:
        sys.exit("reset drops every table in public (the RAG store too). Re-run with --yes to confirm.")
    main = connect(autocommit=True)
    with main.transaction():
        main.execute(spotdb.DB_DIR.joinpath("reset.sql").read_text())
    print("dropped public tables on %s" % spotdb.redact(database_url()))
    main.close()
    if args.include_logs:
        logs = connect(logs_database_url(), autocommit=True)
        with logs.transaction():
            logs.execute(spotdb.DB_DIR.joinpath("reset_logs.sql").read_text())
        print("dropped raw/app schemas on %s" % spotdb.redact(logs_database_url()))
        logs.close()
    migrate(argparse.Namespace(hnsw=False, recreate_documents=False))


def status(args) -> None:
    main = connect(autocommit=True)
    for t in ["locations", "assets", "missions", "actions", "readings", "anomalies", "events", "flags",
              "assessments", "acknowledgements", "system1_predictions", "documents"]:
        if spotdb.has_table(main, "public." + t):
            print("%-22s %6d" % (t, main.execute("SELECT count(*) FROM %s" % t).fetchone()[0]))
    if spotdb.has_table(main, "public.documents"):
        for kind, n, embedded in main.execute("SELECT kind, count(*), count(embedding) FROM documents GROUP BY 1 ORDER BY 1"):
            print("  documents.%-14s %5d (%d embedded)" % (kind, n, embedded))
    if spotdb.has_table(main, "public.kg_nodes"):
        n, embedded = main.execute("SELECT count(*), count(embedding) FROM kg_nodes").fetchone()
        print("%-22s %6d (%d embedded)" % ("kg_nodes", n, embedded))
        print("  " + ", ".join("%s %d" % r for r in main.execute(
            "SELECT kind, count(*) FROM kg_nodes GROUP BY 1 ORDER BY 1")))
        print("%-22s %6d" % ("kg_edges", main.execute("SELECT count(*) FROM kg_edges").fetchone()[0]))
        print("  " + ", ".join("%s %d" % r for r in main.execute(
            "SELECT rel, count(*) FROM kg_edges GROUP BY 1 ORDER BY 1")))
    main.close()
    logs = connect(logs_database_url(), autocommit=True)
    for t in ["raw.spot_event_log", "raw.kit_files", "raw.kit_rows", "raw.inspection_media", "app.model_calls",
              "app.mcp_tool_calls", "app.request_log", "app.job_runs"]:
        if spotdb.has_table(logs, t):
            print("%-22s %6d" % (t, logs.execute("SELECT count(*) FROM %s" % t).fetchone()[0]))
    logs.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("migrate", help="apply db/migrations (idempotent)")
    m.add_argument("--hnsw", action="store_true", help="also create the optional HNSW index (approximate search)")
    m.add_argument("--recreate-documents", action="store_true", help="drop documents if EMBED_DIM changed")
    s = sub.add_parser("seed", help="load the kit into public.* and raw.*, seed escalated flags")
    s.add_argument("--no-events", action="store_true", help="don't pre-load public.events from replay.ndjson")
    s.add_argument("--force-reseed", action="store_true",
                   help="DESTRUCTIVE: overwrite existing kit rows, events and seeded flags (reasons, score, "
                        "escalated) and reload the demo baseline even if it was loaded before. Never used by db-init.")
    r = sub.add_parser("reset", help="drop and recreate (needs --yes)")
    r.add_argument("--yes", action="store_true")
    r.add_argument("--include-logs", action="store_true", help="also drop raw.* and app.*")
    sub.add_parser("status", help="row counts")
    args = ap.parse_args()
    {"migrate": migrate, "seed": seed, "reset": reset, "status": status}[args.cmd](args)


if __name__ == "__main__":
    main()
