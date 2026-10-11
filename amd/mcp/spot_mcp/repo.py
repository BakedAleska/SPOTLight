"""Every SQL statement the server runs. Parameterized only; the model never supplies SQL.

The service layer depends on the `Repo` protocol, so unit tests swap in an in-memory fake.
Most lookups return whole rows via row_to_json, so extra columns the DB side adds don't break anything.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, Sequence

from .db import Database


class Repo(Protocol):
    async def flag(self, flag_id: int) -> Optional[Dict[str, Any]]: ...
    async def action_bundle(self, action_id: str) -> Optional[Dict[str, Any]]: ...
    async def mission_bundle(self, mission_id: str) -> Optional[Dict[str, Any]]: ...
    async def s1_predictions(self, action_id: Optional[str], mission_id: Optional[str]) -> List[Dict[str, Any]]: ...
    async def reliability_counts(self, filters: Dict[str, Any], group_by: Optional[str]) -> List[Dict[str, Any]]: ...
    async def asset_profile(self, asset_id: str, exclude_flag_id: Optional[int]) -> Optional[Dict[str, Any]]: ...
    async def documents_by_id(self, doc_ids: Sequence[str]) -> List[Dict[str, Any]]: ...
    async def match(self, vec: List[float], kinds: Optional[List[str]], asset: Optional[str],
                    location: Optional[str], mission: Optional[str], k: int,
                    min_sim: float) -> List[Dict[str, Any]]: ...
    async def match_hybrid(self, vec: List[float], text: str, kinds: Optional[List[str]], asset: Optional[str],
                           location: Optional[str], mission: Optional[str], k: int) -> List[Dict[str, Any]]: ...
    async def raw_event_log(self, mission_id: str, around_ts: Optional[str], limit: int) -> List[Dict[str, Any]]: ...
    # Graph RAG (contract §7). Each raises GraphUnavailable on a database without migration 009.
    async def match_kg_nodes(self, vec: List[float], kinds: Optional[List[str]], k: int,
                             min_sim: float) -> List[Dict[str, Any]]: ...
    async def match_kg_nodes_text(self, text: str, kinds: Optional[List[str]], k: int) -> List[Dict[str, Any]]: ...
    async def graph_neighborhood(self, seeds: Sequence[str], hops: int, rels: Optional[List[str]],
                                 kinds: Optional[List[str]], max_nodes: int) -> List[Dict[str, Any]]: ...
    async def kg_details(self, node_ids: Sequence[str]) -> Dict[str, List[Dict[str, Any]]]: ...


class GraphUnavailable(RuntimeError):
    """The knowledge graph's tables/functions are missing (a DB before migration 009) or not granted to this role."""


# SQLSTATEs that mean "this database has no graph (yet)": undefined function/table/schema, no privilege.
_GRAPH_MISSING = {"42883", "42P01", "3F000", "42501"}
# Result caps (defence in depth; the service validates first).
MAX_ENTRY_NODES = 20
MAX_GRAPH_NODES = 60
MAX_SEEDS = 20


def vec_literal(vec: List[float]) -> str:
    return "[" + ",".join("%.7g" % float(x) for x in vec) + "]"


BATTERY_BAND_SQL = ("CASE WHEN a.battery_pct <= 25 THEN '<=25' WHEN a.battery_pct <= 40 THEN '26-40' "
                    "WHEN a.battery_pct <= 60 THEN '41-60' WHEN a.battery_pct <= 80 THEN '61-80' ELSE '81+' END")
# Whitelist: model-facing names -> SQL expressions. Nothing else is ever interpolated.
DIMENSIONS = {
    "action_type": "a.action_type",
    "location_id": "a.location_id",
    "configuration_version": "a.configuration_version",
    "battery_band": BATTERY_BAND_SQL,
}


def reliability_sql(filters: Dict[str, Any], group_by: Optional[str]):
    """Build the reliability query from whitelisted dimensions. Returns (sql, params)."""
    where, params = ["a.status IN ('SUCCESS','FAILED')"], []
    for name, value in filters.items():
        if value is None:
            continue
        if name not in DIMENSIONS:
            raise ValueError("unknown filter %r" % name)
        where.append("(%s) = %%s" % DIMENSIONS[name])
        params.append(value)
    if group_by is not None and group_by not in DIMENSIONS:
        raise ValueError("unknown group_by %r" % group_by)
    grp = DIMENSIONS[group_by] if group_by else "'all'"
    sql = ("SELECT (%s) AS grp, count(*)::int AS n, count(*) FILTER (WHERE a.status = 'SUCCESS')::int AS n_success "
           "FROM actions a WHERE %s GROUP BY 1 ORDER BY 1 LIMIT 50") % (grp, " AND ".join(where))
    return sql, params


class PgRepo:
    def __init__(self, db: Database):
        self.db = db

    async def _one(self, sql: str, params: Sequence[Any]) -> Optional[Dict[str, Any]]:
        rows = await self.db.fetch(sql, params)
        return rows[0] if rows else None

    async def flag(self, flag_id: int) -> Optional[Dict[str, Any]]:
        row = await self._one(
            """SELECT row_to_json(f) AS flag,
                      (SELECT row_to_json(e) FROM events e WHERE e.event_id = f.event_id) AS event,
                      (SELECT coalesce(json_agg(json_build_object('ack_id', k.ack_id, 'action', k.action, 'note', k.note,
                                'operator', k.operator, 'created_at', k.created_at) ORDER BY k.created_at), '[]'::json)
                         FROM acknowledgements k WHERE k.flag_id = f.flag_id) AS acks,
                      (SELECT row_to_json(s) FROM assessments s WHERE s.flag_id = f.flag_id) AS assessment
                 FROM flags f WHERE f.flag_id = %s""", (flag_id,))
        if not row or not row.get("flag"):
            return None
        return row

    async def action_bundle(self, action_id: str) -> Optional[Dict[str, Any]]:
        row = await self._one(
            """SELECT row_to_json(a) AS action, row_to_json(s) AS asset, row_to_json(l) AS location,
                      (SELECT json_build_object('mission_id', m.mission_id, 'mission_status', m.mission_status,
                              'configuration_version', m.configuration_version, 'start_time', m.start_time)
                         FROM missions m WHERE m.mission_id = a.mission_id) AS mission,
                      (SELECT coalesce(json_agg(row_to_json(r) ORDER BY r.timestamp), '[]'::json)
                         FROM readings r WHERE r.action_id = a.action_id) AS readings,
                      (SELECT coalesce(json_agg(row_to_json(n) ORDER BY n.timestamp), '[]'::json)
                         FROM anomalies n WHERE n.action_id = a.action_id) AS anomalies
                 FROM actions a
                 LEFT JOIN assets s ON s.asset_id = a.asset_id
                 LEFT JOIN locations l ON l.location_id = a.location_id
                WHERE a.action_id = %s""", (action_id,))
        return row if row and row.get("action") else None

    async def mission_bundle(self, mission_id: str) -> Optional[Dict[str, Any]]:
        row = await self._one(
            """SELECT row_to_json(m) AS mission,
                      (SELECT row_to_json(k) FROM mission_kpis k WHERE k.mission_id = m.mission_id) AS kpis,
                      (SELECT count(*)::int FROM actions a WHERE a.mission_id = m.mission_id) AS actions_total,
                      (SELECT count(*)::int FROM actions a WHERE a.mission_id = m.mission_id AND a.status = 'FAILED')
                          AS actions_failed,
                      (SELECT coalesce(json_object_agg(ft, n), '{}'::json) FROM (
                          SELECT a.failure_type AS ft, count(*) AS n FROM actions a
                           WHERE a.mission_id = m.mission_id AND coalesce(a.failure_type, '') <> ''
                           GROUP BY 1) x) AS failure_types,
                      (SELECT coalesce(json_object_agg(sev, n), '{}'::json) FROM (
                          SELECT n.severity AS sev, count(*) AS n FROM anomalies n
                           WHERE n.mission_id = m.mission_id GROUP BY 1) y) AS anomalies_by_severity,
                      (SELECT json_build_object('anomaly_id', n.anomaly_id, 'anomaly_type', n.anomaly_type,
                              'severity', n.severity, 'asset_id', n.asset_id, 'action_id', n.action_id,
                              'asset_name', wa.asset_name, 'asset_criticality', wa.criticality)
                         FROM anomalies n LEFT JOIN assets wa ON wa.asset_id = n.asset_id
                        WHERE n.mission_id = m.mission_id
                        ORDER BY CASE n.severity WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3 WHEN 'MEDIUM' THEN 2
                                 ELSE 1 END DESC, n.timestamp LIMIT 1) AS worst_anomaly
                 FROM missions m WHERE m.mission_id = %s""", (mission_id,))
        return row if row and row.get("mission") else None

    async def s1_predictions(self, action_id: Optional[str], mission_id: Optional[str]) -> List[Dict[str, Any]]:
        if action_id:
            sql, p = ("SELECT prediction_id, event_kind, action_id, mission_id, question, answer, probs, confidence, "
                      "model, created_at FROM system1_predictions WHERE action_id = %s "
                      "ORDER BY created_at DESC, prediction_id DESC LIMIT 40", (action_id,))
        else:
            sql, p = ("SELECT prediction_id, event_kind, action_id, mission_id, question, answer, probs, confidence, "
                      "model, created_at FROM system1_predictions WHERE mission_id = %s AND event_kind = 'mission' "
                      "ORDER BY created_at DESC, prediction_id DESC LIMIT 40", (mission_id,))
        return await self.db.fetch(sql, p)

    async def reliability_counts(self, filters: Dict[str, Any], group_by: Optional[str]) -> List[Dict[str, Any]]:
        sql, params = reliability_sql(filters, group_by)
        return await self.db.fetch(sql, params)

    async def asset_profile(self, asset_id: str, exclude_flag_id: Optional[int]) -> Optional[Dict[str, Any]]:
        """The asset's history as it stood when this flag's event happened (robot time: the event's ts, else the
        action's timestamp, else the mission's end_time): anomalies, the last 5 readings and the 3 latest prior
        flags strictly before that time, never the flag's own action or anomaly, and never a later flag (replays
        create flags out of robot-time order, so created_at is not used). Without a flag (or a flag with no robot
        time) nothing is time-bounded. Blank operator notes are skipped."""
        row = await self._one(
            """WITH me AS (   -- one row even when the flag is unknown (an aggregate without GROUP BY)
                   SELECT max(f.action_id) AS action_id, max(coalesce(e.ts, a.timestamp, m.end_time)) AS ts
                     FROM flags f
                     LEFT JOIN events e ON e.event_id = f.event_id
                     LEFT JOIN actions a ON a.action_id = f.action_id
                     LEFT JOIN missions m ON m.mission_id = f.mission_id
                    WHERE f.flag_id = %s)
               SELECT row_to_json(s) AS asset, me.ts AS as_of,
                      (SELECT count(*)::int FROM anomalies n
                        WHERE n.asset_id = s.asset_id AND (me.ts IS NULL OR n.timestamp < me.ts)
                          AND (me.action_id IS NULL OR n.action_id IS DISTINCT FROM me.action_id)) AS anomaly_count,
                      (SELECT coalesce(json_object_agg(t, c), '{}'::json) FROM (
                          SELECT n.anomaly_type AS t, count(*) AS c FROM anomalies n
                           WHERE n.asset_id = s.asset_id AND (me.ts IS NULL OR n.timestamp < me.ts)
                             AND (me.action_id IS NULL OR n.action_id IS DISTINCT FROM me.action_id)
                           GROUP BY 1) x) AS anomalies_by_type,
                      (SELECT coalesce(json_agg(r ORDER BY r.timestamp), '[]'::json) FROM (
                          SELECT rd.reading_id, rd.action_id, rd.timestamp, rd.measurement_type, rd.value, rd.unit,
                                 rd.quality
                            FROM readings rd
                           WHERE rd.asset_id = s.asset_id AND (me.ts IS NULL OR rd.timestamp < me.ts)
                             AND (me.action_id IS NULL OR rd.action_id IS DISTINCT FROM me.action_id)
                           ORDER BY rd.timestamp DESC LIMIT 5) r) AS recent_readings,
                      (SELECT coalesce(json_agg(p ORDER BY p.ts DESC NULLS LAST, p.flag_id DESC), '[]'::json) FROM (
                          SELECT pf.flag_id, pf.title, pf.severity, pf.status, pf.created_at, pf.ts,
                                 (SELECT string_agg(btrim(k.note), ' | ' ORDER BY k.created_at) FROM acknowledgements k
                                   WHERE k.flag_id = pf.flag_id AND nullif(btrim(k.note), '') IS NOT NULL) AS notes
                            FROM (SELECT f.flag_id, f.title, f.severity, f.status, f.created_at, f.action_id,
                                         coalesce(e.ts, a.timestamp, mm.end_time) AS ts
                                    FROM flags f
                                    LEFT JOIN events e ON e.event_id = f.event_id
                                    LEFT JOIN actions a ON a.action_id = f.action_id
                                    LEFT JOIN missions mm ON mm.mission_id = f.mission_id
                                   WHERE f.asset_id = s.asset_id AND f.flag_id <> coalesce(%s, -1)) pf
                           WHERE (me.ts IS NULL OR pf.ts < me.ts)
                             AND (me.action_id IS NULL OR pf.action_id IS DISTINCT FROM me.action_id)
                           ORDER BY pf.ts DESC NULLS LAST, pf.flag_id DESC LIMIT 3) p) AS prior_flags
                 FROM assets s CROSS JOIN me WHERE s.asset_id = %s""", (exclude_flag_id, exclude_flag_id, asset_id))
        return row if row and row.get("asset") else None

    async def documents_by_id(self, doc_ids: Sequence[str]) -> List[Dict[str, Any]]:
        if not doc_ids:
            return []
        return await self.db.fetch(
            "SELECT doc_id, kind, title, content, metadata, asset_id, location_id, mission_id, action_id, ts "
            "FROM documents WHERE doc_id = ANY(%s) LIMIT 20", (list(doc_ids),))

    async def match(self, vec, kinds, asset, location, mission, k, min_sim):
        return await self.db.fetch(
            "SELECT * FROM match_documents(%s::vector, %s::text[], %s, %s, %s, %s, %s::real)",
            (vec_literal(vec), kinds, asset, location, mission, k, min_sim))

    async def match_hybrid(self, vec, text, kinds, asset, location, mission, k):
        return await self.db.fetch(
            "SELECT * FROM match_documents_hybrid(%s::vector, %s, %s::text[], %s, %s, %s, %s)",
            (vec_literal(vec), text, kinds, asset, location, mission, k))

    async def raw_event_log(self, mission_id: str, around_ts: Optional[str], limit: int) -> List[Dict[str, Any]]:
        """One row per event (contract §5): raw.* is append-only, and a streamed replay re-logs the seed's
        replay.ndjson events under its own source (same payload), so copies are collapsed here. The replay.ndjson
        row wins, else the first one logged: a later replay never changes the log:<id> an event is cited by.
        Rows without an event_id are kept one by one. The limit applies after de-duplication."""
        dedup = ("SELECT DISTINCT ON (coalesce(event_id, 'log:' || log_id)) "
                 "log_id, source, event_id, event_type, event_ts, payload FROM raw.spot_event_log "
                 "WHERE mission_id = %s "
                 "ORDER BY coalesce(event_id, 'log:' || log_id), (source = 'replay.ndjson') DESC, log_id")
        if around_ts is None:
            sql, p = ("SELECT * FROM (%s) d ORDER BY event_ts, log_id LIMIT %%s" % dedup, (mission_id, limit))
        else:
            sql, p = ("SELECT * FROM (SELECT * FROM (%s) d "
                      "ORDER BY abs(extract(epoch FROM (event_ts - %%s::timestamp))), log_id LIMIT %%s) x "
                      "ORDER BY event_ts, log_id" % dedup, (mission_id, around_ts, limit))
        return await self.db.fetch(sql, p)

    # ── Graph RAG (contract §7): the three graph functions plus one by-id lookup ───────────────────
    async def _graph(self, sql: str, params: Sequence[Any]) -> List[Dict[str, Any]]:
        try:
            return await self.db.fetch(sql, params)
        except Exception as exc:
            if getattr(exc, "sqlstate", None) in _GRAPH_MISSING:
                raise GraphUnavailable("knowledge graph not available in this database (%s; migration 009)"
                                       % type(exc).__name__) from exc
            raise

    async def kg_has_nodes(self) -> bool:
        """False when kg_nodes is empty (db/graph_build.py has not run). Optional in the Repo protocol."""
        rows = await self._graph("SELECT EXISTS (SELECT 1 FROM kg_nodes) AS has_nodes", ())
        return bool(rows and rows[0].get("has_nodes"))

    async def match_kg_nodes(self, vec, kinds, k, min_sim):
        return await self._graph(
            "SELECT node_id, kind, label, summary, doc_id, similarity "
            "FROM match_kg_nodes(%s::vector, %s::text[], %s, %s::real)",
            (vec_literal(vec), kinds, max(1, min(int(k), MAX_ENTRY_NODES)), min_sim))

    async def match_kg_nodes_text(self, text, kinds, k):
        return await self._graph(
            "SELECT node_id, kind, label, summary, doc_id, score FROM match_kg_nodes_text(%s, %s::text[], %s)",
            (text, kinds, max(1, min(int(k), MAX_ENTRY_NODES))))

    async def graph_neighborhood(self, seeds, hops, rels, kinds, max_nodes):
        return await self._graph(
            "SELECT node_id, kind, label, summary, doc_id, hop, via_rel, via_node, weight, evidence "
            "FROM graph_neighborhood(%s::text[], %s, %s::text[], %s::text[], %s)",
            (list(seeds)[:MAX_SEEDS], max(1, min(int(hops), 3)), rels, kinds,
             max(1, min(int(max_nodes), MAX_GRAPH_NODES))))

    async def kg_details(self, node_ids):
        """The nodes (label, kind, doc_id) and every stored edge among them (true direction, weight, props, evidence).
        graph_neighborhood returns one row per reached node with the edge it was reached by, so edges between two
        seeds, or between two nodes at the same hop (often the on-point statistics), never appear in it; reading
        the induced edges here recovers them, plus the props (n/rate) the traversal doesn't return."""
        ids = list(dict.fromkeys(node_ids))[:MAX_GRAPH_NODES + MAX_SEEDS]
        rows = await self._graph(
            """SELECT
                 (SELECT coalesce(json_agg(json_build_object('node_id', n.node_id, 'kind', n.kind, 'label', n.label,
                          'summary', n.summary, 'doc_id', n.doc_id)), '[]'::json)
                    FROM kg_nodes n WHERE n.node_id = ANY(%s::text[])) AS nodes,
                 (SELECT coalesce(json_agg(x), '[]'::json) FROM (
                     SELECT e.src, e.rel, e.dst, e.weight, e.props, e.evidence FROM kg_edges e
                      WHERE e.src = ANY(%s::text[]) AND e.dst = ANY(%s::text[])
                      ORDER BY e.weight DESC, e.src, e.rel, e.dst LIMIT 400) x) AS edges""",
            (ids, ids, ids))
        row = rows[0] if rows else {}
        return {"nodes": row.get("nodes") or [], "edges": row.get("edges") or []}
