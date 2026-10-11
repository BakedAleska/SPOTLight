"""Live simulation storage (db/migrations/013_live.sql; docs/backend-services-contract.md "Live simulation").

`LiveRepo` owns `live_runs` / `live_captures` and the live knowledge-graph rows (`kg_nodes` / `kg_edges` with
props.source = 'live', which db/graph_build.py never deletes). The usual pipeline rows (raw log, events,
predictions, flags, assessments, documents) are still written through `Repo` by the ingest pipeline and
`LiveService`, so a live stop looks like any other event to the rest of the system.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

from psycopg.types.json import Jsonb

from .pool import Database

RUN_COLS = ("run_id, route_id, mission_id, status, current_stop, total_stops, runner, requested_by, error, counts, "
            "created_at, claimed_at, finished_at, updated_at")
CAPTURE_JSON = ("reading", "anomaly", "laya", "rules", "decision", "photo", "vision", "qwen", "kg", "timings")
CAPTURE_FIELDS = ("action_id", "source_action_id", "location_id", "asset_id", "action_type", "battery_pct", "status",
                  "failure_type", "reading", "anomaly", "phase", "event_id", "flag_id", "verdict", "priority",
                  "severity", "category", "laya", "rules", "decision", "photo", "vision", "qwen", "qwen_source",
                  "doc_id", "kg", "timings")
STALE_MINUTES = 10      # a pending/running run with no progress for this long no longer blocks a new run
LIVE_SOURCE = "live"    # props.source on every live kg row (services/live.py LIVE_SOURCE)


def _val(col: str, v: Any) -> Any:
    return Jsonb(v) if col in CAPTURE_JSON and v is not None else v


class LiveRepo:
    def __init__(self, db: Database):
        self.db = db

    # ── runs ──────────────────────────────────────────────────────────────────────────────────────
    async def create_run(self, route_id: str, total_stops: int, requested_by: Optional[str]) -> Optional[Dict[str, Any]]:
        """Queue a run (status pending). None if another run is pending/running and not stale."""
        async with self.db.transaction() as cur:
            await cur.execute(
                "UPDATE live_runs SET status = 'error', error = %s, finished_at = now(), updated_at = now() "
                "WHERE status IN ('pending', 'running') AND updated_at < now() - make_interval(mins => %s)",
                ("stale: no progress for %d minutes" % STALE_MINUTES, STALE_MINUTES))
            await cur.execute(
                "INSERT INTO live_runs (route_id, total_stops, requested_by) VALUES (%s, %s, %s) "
                "ON CONFLICT DO NOTHING RETURNING run_id", (route_id, total_stops, requested_by))
            row = await cur.fetchone()
            if not row:
                return None
            await cur.execute("UPDATE live_runs SET mission_id = 'LIVE-' || run_id WHERE run_id = %s RETURNING "
                              + RUN_COLS, (row["run_id"],))
            return dict(await cur.fetchone())

    async def active_run(self) -> Optional[Dict[str, Any]]:
        return await self.db.fetchone("SELECT %s FROM live_runs WHERE status IN ('pending', 'running') "
                                      "ORDER BY run_id DESC LIMIT 1" % RUN_COLS)

    async def claim(self, runner: Optional[str]) -> Optional[Dict[str, Any]]:
        """The oldest pending run -> running (claimed by `runner`). None if nothing is queued."""
        return await self.db.fetchone(
            "UPDATE live_runs SET status = 'running', runner = %%s, claimed_at = now(), updated_at = now() "
            "WHERE run_id = (SELECT run_id FROM live_runs WHERE status = 'pending' ORDER BY run_id "
            "LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING %s" % RUN_COLS, (runner,))

    async def get_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        return await self.db.fetchone("SELECT %s FROM live_runs WHERE run_id = %%s" % RUN_COLS, (run_id,))

    async def list_runs(self, limit: int) -> List[Dict[str, Any]]:
        return await self.db.fetch(
            "SELECT %s, (SELECT count(*)::int FROM live_captures c WHERE c.run_id = r.run_id AND c.verdict = 'red') "
            "AS red, (SELECT count(*)::int FROM live_captures c WHERE c.run_id = r.run_id AND c.verdict = 'green') "
            "AS green FROM live_runs r ORDER BY run_id DESC LIMIT %%s" % RUN_COLS.replace("run_id,", "r.run_id,", 1),
            (limit,))

    async def update_run(self, run_id: int, **fields: Any) -> Optional[Dict[str, Any]]:
        sets, vals = ["updated_at = now()"], []
        for k, v in fields.items():
            if k not in ("status", "current_stop", "error", "counts", "finished"):
                raise ValueError("unknown live_runs field %s" % k)
            if k == "finished":
                if v:
                    sets.append("finished_at = now()")
                continue
            sets.append("%s = %%s" % k)
            vals.append(Jsonb(v) if k == "counts" else v)
        return await self.db.fetchone("UPDATE live_runs SET %s WHERE run_id = %%s RETURNING %s"
                                      % (", ".join(sets), RUN_COLS), vals + [run_id])

    async def touch_run(self, run_id: int, current_stop: Optional[int] = None) -> None:
        if current_stop is None:
            await self.db.execute("UPDATE live_runs SET updated_at = now() WHERE run_id = %s", (run_id,))
        else:
            await self.db.execute("UPDATE live_runs SET current_stop = greatest(current_stop, %s), updated_at = now() "
                                  "WHERE run_id = %s", (current_stop, run_id))

    # ── captures ──────────────────────────────────────────────────────────────────────────────────
    async def upsert_capture(self, run_id: int, stop_index: int, stop: str, fields: Dict[str, Any],
                             stamp: Optional[str] = None) -> Dict[str, Any]:
        """Insert or update one stop's capture. `stamp` sets triaged_at / analysed_at to now()."""
        cols = [c for c in CAPTURE_FIELDS if c in fields]
        stamped = stamp in ("triaged_at", "analysed_at")
        # UPDATE first: a partial row (no action_id) must never be proposed to INSERT (NOT NULL is checked first).
        sets = ["%s = %%s" % c for c in cols] + ["updated_at = now()"] + (["%s = now()" % stamp] if stamped else [])
        row = await self.db.fetchone(
            "UPDATE live_captures SET %s WHERE run_id = %%s AND stop_index = %%s RETURNING *" % ", ".join(sets),
            [_val(c, fields[c]) for c in cols] + [run_id, stop_index])
        if row:
            return dict(row)
        names = ["run_id", "stop_index", "stop"] + cols + ([stamp] if stamped else [])
        vals = [run_id, stop_index, stop] + [_val(c, fields[c]) for c in cols]
        placeholders = ["%s"] * len(vals) + (["now()"] if stamped else [])
        row = await self.db.fetchone(
            "INSERT INTO live_captures (%s) VALUES (%s) RETURNING *" % (", ".join(names), ", ".join(placeholders)), vals)
        return dict(row)

    async def merge_capture_json(self, run_id: int, stop_index: int, col: str, patch: Dict[str, Any]) -> None:
        if col not in ("timings", "kg"):
            raise ValueError("merge only on timings/kg")
        await self.db.execute("UPDATE live_captures SET %s = %s || %%s::jsonb, updated_at = now() "
                              "WHERE run_id = %%s AND stop_index = %%s" % (col, col),
                              (json.dumps(patch, default=str), run_id, stop_index))

    async def set_draft(self, run_id: int, stop_index: int, qwen: Dict[str, Any]) -> bool:
        """One conditional UPDATE: store a Qwen draft only while the run is running and the stop's analysis is
        pending (once /analysis has written the checked result, qwen_source is no longer 'pending' and nothing
        changes). True if updated."""
        n = await self.db.execute(
            "UPDATE live_captures c SET qwen = %s, phase = 'analysing', updated_at = now() FROM live_runs r "
            "WHERE r.run_id = c.run_id AND r.status = 'running' AND c.run_id = %s AND c.stop_index = %s "
            "AND c.qwen_source = 'pending'", (Jsonb(qwen), run_id, stop_index))
        return bool(n)

    async def expire_pending(self, run_id: int) -> int:
        """When a run ends: captures still waiting for /analysis (the runner's post failed or it stopped) become
        qwen_source 'error', so /live stops showing an analysing card or an unverified draft as if still live.
        A leftover draft stays in `qwen` (the console labels it discarded). Returns the rows changed."""
        return await self.db.execute(
            "UPDATE live_captures SET qwen_source = 'error', phase = 'error', updated_at = now() "
            "WHERE run_id = %s AND qwen_source = 'pending'", (run_id,)) or 0

    async def get_capture(self, run_id: int, stop_index: int) -> Optional[Dict[str, Any]]:
        return await self.db.fetchone("SELECT * FROM live_captures WHERE run_id = %s AND stop_index = %s",
                                      (run_id, stop_index))

    async def captures(self, run_id: int, after: Optional[Any] = None) -> List[Dict[str, Any]]:
        if after is None:
            return await self.db.fetch("SELECT * FROM live_captures WHERE run_id = %s ORDER BY stop_index", (run_id,))
        return await self.db.fetch("SELECT * FROM live_captures WHERE run_id = %s AND updated_at > %s "
                                   "ORDER BY stop_index", (run_id, after))

    # ── knowledge graph (props.source = 'live') ───────────────────────────────────────────────────
    async def kg_upsert(self, nodes: Iterable[Dict[str, Any]], edges: Iterable[Dict[str, Any]]) -> Dict[str, int]:
        """Upsert live nodes, then edges whose two ends exist (an edge to a missing kit node is skipped).
        Every row gets props.source = 'live': db/graph_build.py and amd/embed/ingest.py keep live rows on that."""
        nodes = [{**n, "props": {**(n.get("props") or {}), "source": LIVE_SOURCE}} for n in nodes]
        edges = [{**e, "props": {**(e.get("props") or {}), "source": LIVE_SOURCE}} for e in edges]
        n_edges = 0
        async with self.db.transaction() as cur:
            for n in nodes:
                await cur.execute(
                    "INSERT INTO kg_nodes (node_id, kind, key, label, summary, props, doc_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, (SELECT doc_id FROM documents WHERE doc_id = %s)) "
                    "ON CONFLICT (node_id) DO UPDATE SET label = EXCLUDED.label, summary = EXCLUDED.summary, "
                    "props = kg_nodes.props || EXCLUDED.props, doc_id = coalesce(EXCLUDED.doc_id, kg_nodes.doc_id), "
                    "updated_at = now()",
                    (n["node_id"], n["kind"], n["key"], n["label"], n["summary"], Jsonb(n.get("props") or {}),
                     n.get("doc_id")))
            for e in edges:
                await cur.execute(
                    "INSERT INTO kg_edges (src, rel, dst, weight, props, evidence) "
                    "SELECT %s, %s, %s, %s, %s, %s WHERE EXISTS (SELECT 1 FROM kg_nodes WHERE node_id = %s) "
                    "AND EXISTS (SELECT 1 FROM kg_nodes WHERE node_id = %s) "
                    "ON CONFLICT (src, rel, dst) DO UPDATE SET props = EXCLUDED.props, evidence = EXCLUDED.evidence",
                    (e["src"], e["rel"], e["dst"], float(e.get("weight", 1.0)), Jsonb(e.get("props") or {}),
                     e.get("evidence"), e["src"], e["dst"]))
                n_edges += max(cur.rowcount, 0)
        return {"nodes": len(nodes), "edges": n_edges}

    async def embed_document(self, doc_id: str, text: str, vec: str, model: str) -> bool:
        """Set a document's embedding if its content is exactly `text`; document-backed nodes reuse it."""
        n = await self.db.execute("UPDATE documents SET embedding = %s::vector, embed_model = %s "
                                  "WHERE doc_id = %s AND content = %s", (vec, model, doc_id, text))
        if n:
            await self.db.execute("UPDATE kg_nodes n SET embedding = d.embedding, embed_model = d.embed_model, "
                                  "updated_at = now() FROM documents d WHERE d.doc_id = n.doc_id AND n.doc_id = %s",
                                  (doc_id,))
        return bool(n)

    async def embed_node(self, node_id: str, text: str, vec: str, model: str) -> bool:
        """Set a live node's embedding if its summary is exactly `text`."""
        n = await self.db.execute("UPDATE kg_nodes SET embedding = %s::vector, embed_model = %s, updated_at = now() "
                                  "WHERE node_id = %s AND summary = %s AND props->>'source' = 'live'",
                                  (vec, model, node_id, text))
        return bool(n)

    async def kg_for_run(self, run_id: int) -> Dict[str, Any]:
        rid = str(run_id)
        nodes = await self.db.fetch(
            "SELECT node_id, kind, key, label, summary, doc_id, (embedding IS NOT NULL) AS embedded, embed_model, "
            "props->>'stop' AS stop, updated_at FROM kg_nodes WHERE props->>'source' = 'live' "
            "AND props->>'run_id' = %s ORDER BY updated_at, node_id", (rid,))
        edges = await self.db.fetch(
            "SELECT src, rel, dst, evidence, props->>'stop' AS stop FROM kg_edges WHERE props->>'source' = 'live' "
            "AND props->>'run_id' = %s ORDER BY props->>'stop', src, rel, dst", (rid,))
        totals = await self.db.fetchone(
            "SELECT (SELECT count(*)::int FROM kg_nodes) AS nodes, (SELECT count(*)::int FROM kg_edges) AS edges, "
            "(SELECT count(*)::int FROM kg_nodes WHERE props->>'source' = 'live') AS live_nodes, "
            "(SELECT count(*)::int FROM kg_edges WHERE props->>'source' = 'live') AS live_edges")
        return {"nodes": nodes, "edges": edges, "totals": totals}
