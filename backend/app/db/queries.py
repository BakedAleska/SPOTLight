"""Every SQL statement the backend runs (parameterized only). Tables: docs/system2-rag-contract.md §2 and §6
(`db/migrations/`). Additive columns (`flags.triage_event_id/priority/category`,
`raw.spot_event_log.request_id`) are used only when the migration created them.

`Repo` is the seam the routers and services depend on; unit tests replace it with an in-memory fake.
"""
from __future__ import annotations

import base64
import datetime as dt
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from psycopg.types.json import Jsonb

from .pool import Database

SERIOUS = ("HIGH", "CRITICAL")
BATTERY_BAND_SQL = ("CASE WHEN a.battery_pct <= 25 THEN '<=25' WHEN a.battery_pct <= 40 THEN '26-40' "
                    "WHEN a.battery_pct <= 60 THEN '41-60' WHEN a.battery_pct <= 80 THEN '61-80' ELSE '81+' END")
BAND_ORDER = ["<=25", "26-40", "41-60", "61-80", "81+"]
# Whitelisted reliability dimensions: API name -> SQL expression. Nothing else is ever interpolated.
DIMENSIONS = {
    "action_type": "a.action_type",
    "location_id": "a.location_id",
    "configuration_version": "a.configuration_version",
    "battery_band": BATTERY_BAND_SQL,
    "environment": "l.environment",
    "asset_id": "a.asset_id",
    "mission_id": "a.mission_id",
}


def wilson(successes: int, n: int, z: float = 1.96) -> Tuple[Optional[float], Optional[float]]:
    if n <= 0:
        return None, None
    p = successes / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return round(max(0.0, c - h) * 100, 1), round(min(1.0, c + h) * 100, 1)


def rate(n_success: int, n: int) -> Optional[float]:
    return round(100.0 * n_success / n, 1) if n else None


def encode_cursor(ts: dt.datetime, event_id: str) -> str:
    return base64.urlsafe_b64encode(("%s|%s" % (ts.isoformat(), event_id)).encode()).decode().rstrip("=")


def decode_cursor(c: str) -> Tuple[dt.datetime, str]:
    raw = base64.urlsafe_b64decode(c + "=" * (-len(c) % 4)).decode()
    ts, eid = raw.split("|", 1)
    return dt.datetime.fromisoformat(ts), eid


class Repo:
    def __init__(self, db: Database, logs: Optional[Database] = None):
        self.db = db
        self.logs = logs or db

    # ════ ingest ════════════════════════════════════════════════════════════════════════════════
    async def append_raw(self, source: str, raw: Dict[str, Any], sha256: str, ts: Optional[dt.datetime],
                         request_id: Optional[str]) -> Optional[int]:
        """Step 1: verbatim append to raw.spot_event_log. Returns log_id, or None if already logged."""
        cols = ["source", "event_id", "mission_id", "event_type", "event_ts", "payload", "sha256"]
        vals: List[Any] = [source, raw.get("event_id"), raw.get("mission_id"), raw.get("event_type"), ts,
                           Jsonb(raw), sha256]
        if "request_id" in await self.logs.columns("raw", "spot_event_log"):
            cols.append("request_id")
            vals.append(request_id)
        row = await self.logs.fetchone(
            "INSERT INTO raw.spot_event_log (%s) VALUES (%s) ON CONFLICT (source, sha256) DO NOTHING "
            "RETURNING log_id" % (", ".join(cols), ", ".join(["%s"] * len(cols))), vals)
        return row["log_id"] if row else None

    async def upsert_event(self, n: Dict[str, Any]) -> None:
        """Step 2: the normalized event (re-ingesting an event refreshes it and moves it to the feed's head)."""
        await self.db.execute(
            """INSERT INTO events (event_id, mission_id, ts, event_type, action_id, location_id, asset_id,
                                   battery_pct, payload)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (event_id) DO UPDATE SET mission_id = EXCLUDED.mission_id, ts = EXCLUDED.ts,
                 event_type = EXCLUDED.event_type, action_id = EXCLUDED.action_id,
                 location_id = EXCLUDED.location_id, asset_id = EXCLUDED.asset_id,
                 battery_pct = EXCLUDED.battery_pct, payload = EXCLUDED.payload, ingested_at = now()""",
            (n["event_id"], n["mission_id"], n["ts"], n["event_type"], n["action_id"], n["location_id"],
             n["asset_id"], n["battery_pct"], Jsonb(n["payload"])))

    async def set_event_triage(self, event_id: str, triage: Dict[str, Any]) -> None:
        await self.db.execute("UPDATE events SET payload = payload || jsonb_build_object('triage', %s::jsonb) "
                              "WHERE event_id = %s", (Jsonb(triage), event_id))

    async def store_predictions(self, kind: str, action_id: Optional[str], mission_id: Optional[str], model: str,
                                answers: Dict[str, Any]) -> int:
        """Step 5: one row per (event, question). Replaces earlier rows for the same event+model."""
        n = 0
        async with self.db.transaction() as cur:
            for q, a in answers.items():
                if not isinstance(a, dict) or "answer" not in a:
                    continue
                await cur.execute(
                    "DELETE FROM system1_predictions WHERE event_kind = %s AND action_id IS NOT DISTINCT FROM %s "
                    "AND mission_id IS NOT DISTINCT FROM %s AND question = %s AND model = %s",
                    (kind, action_id, mission_id, q, model))
                await cur.execute(
                    "INSERT INTO system1_predictions (event_kind, action_id, mission_id, question, answer, probs, "
                    "confidence, model) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (kind, action_id, mission_id, q, str(a["answer"]), Jsonb(a.get("probs") or {}),
                     a.get("confidence"), model))
                n += 1
        return n

    async def upsert_flag(self, f: Dict[str, Any]) -> Tuple[int, bool]:
        """Step 7: write (or refresh) the flag for a triage event. Returns (flag_id, inserted)."""
        cols = await self.db.columns("public", "flags")
        data = {k: f.get(k) for k in ("event_id", "mission_id", "action_id", "location_id", "asset_id", "score",
                                      "severity", "title", "escalated")}
        data["reasons"] = Jsonb(f["reasons"])
        for k in ("triage_event_id", "priority", "category"):
            if k in cols:
                data[k] = f.get(k)
        names = list(data)
        updates = ", ".join("%s = EXCLUDED.%s" % (k, k) for k in names if k != "triage_event_id")
        if "triage_event_id" in cols:
            row = await self.db.fetchone(
                "INSERT INTO flags (%s) VALUES (%s) ON CONFLICT (triage_event_id) DO UPDATE SET %s "
                "RETURNING flag_id, (xmax = 0) AS inserted" % (", ".join(names), ", ".join(["%s"] * len(names)),
                                                               updates), [data[k] for k in names])
            return row["flag_id"], bool(row["inserted"])
        existing = await self.db.fetchone(
            "SELECT flag_id FROM flags WHERE action_id IS NOT DISTINCT FROM %s AND mission_id IS NOT DISTINCT FROM %s "
            "AND (action_id IS NOT NULL OR event_id = %s) ORDER BY flag_id DESC LIMIT 1",
            (f.get("action_id"), f.get("mission_id"), f.get("event_id")))
        if existing:
            await self.db.execute("UPDATE flags SET %s WHERE flag_id = %%s" % ", ".join("%s = %%s" % k for k in names),
                                  [data[k] for k in names] + [existing["flag_id"]])
            return existing["flag_id"], False
        row = await self.db.fetchone("INSERT INTO flags (%s) VALUES (%s) RETURNING flag_id"
                                     % (", ".join(names), ", ".join(["%s"] * len(names))), [data[k] for k in names])
        return row["flag_id"], True

    async def unescalate_flag(self, triage_event_id: str, reasons: List[Dict[str, Any]]) -> Optional[int]:
        """A re-ingested event that no longer escalates: keep the row (operator history), mark it."""
        if "triage_event_id" not in await self.db.columns("public", "flags"):
            return None
        row = await self.db.fetchone("UPDATE flags SET escalated = false, reasons = %s WHERE triage_event_id = %s "
                                     "RETURNING flag_id", (Jsonb(reasons), triage_event_id))
        return row["flag_id"] if row else None

    # ════ events feed ═══════════════════════════════════════════════════════════════════════════
    async def list_events(self, *, after: Optional[str], before: Optional[str], mission_id: Optional[str],
                          event_type: Optional[str], limit: int) -> List[Dict[str, Any]]:
        where, params = [], []
        if mission_id:
            where.append("e.mission_id = %s")
            params.append(mission_id)
        if event_type:
            where.append("e.event_type = %s")
            params.append(event_type)
        order = "DESC"
        if after:
            ts, eid = decode_cursor(after)
            where.append("(e.ingested_at, e.event_id) > (%s, %s)")
            params += [ts, eid]
            order = "ASC"
        elif before:
            ts, eid = decode_cursor(before)
            where.append("(e.ingested_at, e.event_id) < (%s, %s)")
            params += [ts, eid]
        sql = ("SELECT e.event_id, e.mission_id, e.ts, e.event_type, e.action_id, e.location_id, e.asset_id, "
               "e.battery_pct, e.payload, e.ingested_at, f.flag_id, f.escalated, f.severity, f.score, f.title, "
               "f.status AS flag_status FROM events e LEFT JOIN LATERAL (SELECT * FROM flags f WHERE "
               "f.event_id = e.event_id ORDER BY f.flag_id DESC LIMIT 1) f ON true %s "
               "ORDER BY e.ingested_at %s, e.event_id %s LIMIT %%s"
               % ("WHERE " + " AND ".join(where) if where else "", order, order))
        rows = await self.db.fetch(sql, params + [limit])
        if order == "DESC":
            rows.reverse()
        return rows

    # ════ flags ═════════════════════════════════════════════════════════════════════════════════
    async def list_flags(self, *, status: Optional[str], escalated: Optional[bool], severity: Optional[str],
                         mission_id: Optional[str], asset_id: Optional[str], location_id: Optional[str],
                         limit: int, offset: int) -> Tuple[List[Dict[str, Any]], int]:
        where, params = [], []
        for col, val in (("f.status", status), ("f.severity", severity), ("f.mission_id", mission_id),
                         ("f.asset_id", asset_id), ("f.location_id", location_id)):
            if val is not None:
                where.append("%s = %%s" % col)
                params.append(val)
        if escalated is not None:
            where.append("f.escalated = %s")
            params.append(escalated)
        w = ("WHERE " + " AND ".join(where)) if where else ""
        rows = await self.db.fetch(
            "SELECT f.*, (SELECT s.state FROM assessments s WHERE s.flag_id = f.flag_id) AS assessment_state, "
            "count(*) OVER () AS _total FROM flags f %s "
            "ORDER BY (f.status = 'OPEN') DESC, f.score DESC, f.created_at DESC LIMIT %%s OFFSET %%s" % w,
            params + [limit, offset])
        total = rows[0]["_total"] if rows else 0
        for r in rows:
            r.pop("_total", None)
        return rows, total

    async def get_flag(self, flag_id: int) -> Optional[Dict[str, Any]]:
        row = await self.db.fetchone(
            """SELECT row_to_json(f) AS flag,
                      (SELECT row_to_json(e) FROM events e WHERE e.event_id = f.event_id) AS event,
                      (SELECT coalesce(json_agg(row_to_json(k) ORDER BY k.created_at), '[]'::json)
                         FROM acknowledgements k WHERE k.flag_id = f.flag_id) AS acks,
                      (SELECT row_to_json(s) FROM assessments s WHERE s.flag_id = f.flag_id) AS assessment
                 FROM flags f WHERE f.flag_id = %s""", (flag_id,))
        return row if row and row.get("flag") else None

    async def predictions(self, action_id: Optional[str], mission_id: Optional[str],
                          kind: Optional[str] = None) -> List[Dict[str, Any]]:
        where, params = [], []
        if action_id:
            where.append("action_id = %s")
            params.append(action_id)
        else:
            where.append("mission_id = %s AND action_id IS NULL")
            params.append(mission_id)
        if kind:
            where.append("event_kind = %s")
            params.append(kind)
        return await self.db.fetch(
            "SELECT prediction_id, event_kind, action_id, mission_id, question, answer, probs, confidence, model, "
            "created_at FROM system1_predictions WHERE %s ORDER BY created_at DESC, prediction_id DESC LIMIT 60"
            % " AND ".join(where), params)

    async def predictions_for_actions(self, action_ids: Sequence[str], kind: str) -> List[Dict[str, Any]]:
        if not action_ids:
            return []
        return await self.db.fetch(
            "SELECT action_id, question, answer, probs, confidence, model, created_at FROM system1_predictions "
            "WHERE event_kind = %s AND action_id = ANY(%s) ORDER BY created_at DESC", (kind, list(action_ids)))

    async def insert_ack(self, flag_id: int, action: str, note: Optional[str], operator: str) -> Dict[str, Any]:
        status = {"ACKNOWLEDGE": "ACKNOWLEDGED", "RESOLVE": "RESOLVED", "ESCALATE": "OPEN"}.get(action)
        async with self.db.transaction() as cur:
            await cur.execute("INSERT INTO acknowledgements (flag_id, action, note, operator) VALUES (%s, %s, %s, %s) "
                              "RETURNING *", (flag_id, action, note, operator))
            ack = await cur.fetchone()
            if status:
                await cur.execute("UPDATE flags SET status = %s WHERE flag_id = %s", (status, flag_id))
        return dict(ack)

    async def upsert_assessment(self, flag_id: int, a: Dict[str, Any], state: str = "DONE") -> Dict[str, Any]:
        row = await self.db.fetchone(
            """INSERT INTO assessments (flag_id, state, question, answer, reasoning, recommended_action,
                                        severity_assessment, confidence, model, latency_ms, tokens_per_sec, error,
                                        completed_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
               ON CONFLICT (flag_id) DO UPDATE SET state = EXCLUDED.state, question = EXCLUDED.question,
                 answer = EXCLUDED.answer, reasoning = EXCLUDED.reasoning,
                 recommended_action = EXCLUDED.recommended_action,
                 severity_assessment = EXCLUDED.severity_assessment, confidence = EXCLUDED.confidence,
                 model = EXCLUDED.model, latency_ms = EXCLUDED.latency_ms, tokens_per_sec = EXCLUDED.tokens_per_sec,
                 error = EXCLUDED.error, completed_at = now()
               RETURNING *""",
            (flag_id, state, a.get("question"), a.get("answer"), a.get("reasoning"), a.get("recommended_action"),
             a.get("severity_assessment"), a.get("confidence"), a.get("model"), a.get("latency_ms"),
             a.get("tokens_per_sec"), a.get("error")))
        return dict(row)

    async def upsert_document(self, d: Dict[str, Any], embedding: Optional[str], embed_model: Optional[str]) -> None:
        cols = ("doc_id", "kind", "source_id", "mission_id", "action_id", "asset_id", "location_id", "action_type",
                "status", "anomaly_type", "severity", "ts", "title", "content", "metadata")
        vals = [Jsonb(d.get(c) or {}) if c == "metadata" else d.get(c) for c in cols]
        await self.db.execute(
            "INSERT INTO documents (%s, embedding, embed_model) VALUES (%s, %%s::vector, %%s) "
            "ON CONFLICT (doc_id) DO UPDATE SET %s, embedding = EXCLUDED.embedding, embed_model = EXCLUDED.embed_model"
            % (", ".join(cols), ", ".join(["%s"] * len(cols)),
               ", ".join("%s = EXCLUDED.%s" % (c, c) for c in cols if c != "doc_id")),
            vals + [embedding, embed_model])

    async def action_bundle(self, action_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetchone(
            """SELECT row_to_json(a) AS action, row_to_json(s) AS asset, row_to_json(l) AS location,
                      (SELECT coalesce(json_agg(row_to_json(r) ORDER BY r.timestamp), '[]'::json)
                         FROM readings r WHERE r.action_id = a.action_id) AS readings,
                      (SELECT coalesce(json_agg(row_to_json(n) ORDER BY n.timestamp), '[]'::json)
                         FROM anomalies n WHERE n.action_id = a.action_id) AS anomalies
                 FROM actions a LEFT JOIN assets s ON s.asset_id = a.asset_id
                 LEFT JOIN locations l ON l.location_id = a.location_id WHERE a.action_id = %s""", (action_id,))
        return row if row and row.get("action") else None

    async def asset_history(self, asset_id: str, exclude_flag_id: Optional[int]) -> Dict[str, Any]:
        row = await self.db.fetchone(
            """SELECT (SELECT coalesce(json_agg(x ORDER BY x.timestamp DESC), '[]'::json) FROM (
                         SELECT anomaly_id, action_id, mission_id, timestamp, anomaly_type, severity FROM anomalies
                          WHERE asset_id = %s ORDER BY timestamp DESC LIMIT 5) x) AS anomalies,
                      (SELECT coalesce(json_agg(y), '[]'::json) FROM (
                         SELECT f.flag_id, f.title, f.severity, f.status, f.created_at,
                                (SELECT string_agg(k.note, ' | ' ORDER BY k.created_at) FROM acknowledgements k
                                  WHERE k.flag_id = f.flag_id AND k.note IS NOT NULL) AS notes
                           FROM flags f WHERE f.asset_id = %s AND f.flag_id <> coalesce(%s, -1)
                          ORDER BY f.created_at DESC LIMIT 3) y) AS prior_flags""",
            (asset_id, asset_id, exclude_flag_id))
        return row or {}

    # ════ reliability / insights ════════════════════════════════════════════════════════════════
    async def reliability(self, group_by: Optional[str], filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        where, params = ["a.status IN ('SUCCESS','FAILED')"], []
        for name, value in filters.items():
            if value is None:
                continue
            if name == "min_battery":
                where.append("a.battery_pct > %s")
            elif name == "max_battery":
                where.append("a.battery_pct <= %s")
            elif name in DIMENSIONS:
                where.append("(%s) = %%s" % DIMENSIONS[name])
            else:
                raise ValueError("unknown filter %r" % name)
            params.append(value)
        if group_by is not None and group_by not in DIMENSIONS:
            raise ValueError("unknown group_by %r" % group_by)
        grp = DIMENSIONS[group_by] if group_by else "'all'"
        rows = await self.db.fetch(
            "SELECT (%s) AS grp, count(*)::int AS n, count(*) FILTER (WHERE a.status = 'SUCCESS')::int AS n_success, "
            "avg(a.battery_pct)::real AS mean_battery FROM actions a LEFT JOIN locations l "
            "ON l.location_id = a.location_id WHERE %s GROUP BY 1 ORDER BY 1 LIMIT 100"
            % (grp, " AND ".join(where)), params)
        for r in rows:
            lo, hi = wilson(r["n_success"], r["n"])
            r["success_rate_pct"] = rate(r["n_success"], r["n"])
            r["ci95_pct"] = [lo, hi]
        if group_by == "battery_band":
            rows.sort(key=lambda r: BAND_ORDER.index(r["grp"]) if r["grp"] in BAND_ORDER else 99)
        return rows

    async def failure_baseline(self) -> List[Dict[str, Any]]:
        """Empirical failure rate per (waypoint, action type) from the kit history."""
        return await self.db.fetch(
            "SELECT location_id, action_type, count(*)::int AS n, count(*) FILTER (WHERE status = 'FAILED')::int "
            "AS failed FROM actions GROUP BY 1, 2")

    async def mission_health(self) -> List[Dict[str, Any]]:
        return await self.db.fetch(
            """SELECT m.mission_id, m.mission_name, m.scenario, m.configuration_version, m.start_time, m.end_time,
                      m.duration_min, m.start_battery_pct, m.end_battery_pct, m.mission_status, m.operator_mode,
                      coalesce(a.total, 0) AS actions_total, coalesce(a.failed, 0) AS actions_failed,
                      a.failure_types, coalesce(n.total, 0) AS anomalies_total,
                      coalesce(n.serious, 0) AS serious_anomalies, n.by_severity AS anomalies_by_severity,
                      k.true_health AS kpi_true_health,
                      coalesce(fl.escalated, 0) AS flags_escalated, coalesce(fl.open, 0) AS flags_open,
                      coalesce(ev.n, 0) AS events_ingested
                 FROM missions m
                 LEFT JOIN (SELECT mission_id, count(*)::int AS total,
                                   count(*) FILTER (WHERE status = 'FAILED')::int AS failed,
                                   (SELECT json_object_agg(ft, c) FROM (
                                       SELECT coalesce(x.failure_type, 'UNKNOWN') AS ft, count(*) AS c FROM actions x
                                        WHERE x.mission_id = actions.mission_id AND x.status = 'FAILED'
                                        GROUP BY 1) y) AS failure_types
                              FROM actions GROUP BY mission_id) a ON a.mission_id = m.mission_id
                 LEFT JOIN (SELECT mission_id, count(*)::int AS total,
                                   count(*) FILTER (WHERE severity IN ('HIGH','CRITICAL'))::int AS serious,
                                   json_build_object('LOW', count(*) FILTER (WHERE severity = 'LOW'),
                                                     'MEDIUM', count(*) FILTER (WHERE severity = 'MEDIUM'),
                                                     'HIGH', count(*) FILTER (WHERE severity = 'HIGH'),
                                                     'CRITICAL', count(*) FILTER (WHERE severity = 'CRITICAL'))
                                     AS by_severity
                              FROM anomalies GROUP BY mission_id) n ON n.mission_id = m.mission_id
                 LEFT JOIN mission_kpis k ON k.mission_id = m.mission_id
                 LEFT JOIN (SELECT mission_id, count(*) FILTER (WHERE escalated)::int AS escalated,
                                   count(*) FILTER (WHERE escalated AND status = 'OPEN')::int AS open
                              FROM flags GROUP BY mission_id) fl ON fl.mission_id = m.mission_id
                 LEFT JOIN (SELECT mission_id, count(*)::int AS n FROM events GROUP BY mission_id) ev
                        ON ev.mission_id = m.mission_id
                ORDER BY m.start_time, m.mission_id""")

    async def failure_type_counts(self, mission_id: str) -> Dict[str, int]:
        rows = await self.db.fetch("SELECT failure_type, count(*)::int AS n FROM actions WHERE mission_id = %s "
                                   "AND status = 'FAILED' GROUP BY 1 ORDER BY 2 DESC", (mission_id,))
        return {r["failure_type"] or "UNKNOWN": r["n"] for r in rows}

    async def mission(self, mission_id: str) -> Optional[Dict[str, Any]]:
        return await self.db.fetchone("SELECT * FROM missions WHERE mission_id = %s", (mission_id,))

    async def mission_actions(self, mission_id: str) -> List[Dict[str, Any]]:
        return await self.db.fetch(
            """SELECT a.*, l.location_name, l.environment, l.zone_type, s.asset_name, s.asset_type, s.criticality,
                      (SELECT row_to_json(r) FROM readings r WHERE r.action_id = a.action_id LIMIT 1) AS reading,
                      (SELECT row_to_json(n) FROM anomalies n WHERE n.action_id = a.action_id LIMIT 1) AS anomaly,
                      (SELECT json_agg(json_build_object('flag_id', f.flag_id, 'severity', f.severity,
                              'status', f.status, 'title', f.title, 'escalated', f.escalated))
                         FROM flags f WHERE f.action_id = a.action_id) AS flags
                 FROM actions a LEFT JOIN locations l ON l.location_id = a.location_id
                 LEFT JOIN assets s ON s.asset_id = a.asset_id
                WHERE a.mission_id = %s ORDER BY a.sequence""", (mission_id,))

    async def mission_events(self, mission_id: str) -> List[Dict[str, Any]]:
        return await self.db.fetch(
            "SELECT e.event_id, e.ts, e.event_type, e.action_id, e.location_id, e.asset_id, e.battery_pct, "
            "e.payload -> 'triage' AS triage, e.ingested_at FROM events e WHERE e.mission_id = %s "
            "ORDER BY e.ts, e.event_id", (mission_id,))

    async def mission_flags(self, mission_id: str) -> List[Dict[str, Any]]:
        return await self.db.fetch("SELECT * FROM flags WHERE mission_id = %s ORDER BY score DESC, created_at",
                                   (mission_id,))

    async def overview(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        out["green"] = await self.db.fetchone(
            """WITH ms AS (SELECT m.mission_id, m.mission_status,
                                  (SELECT count(*) FROM actions a WHERE a.mission_id = m.mission_id
                                      AND a.status = 'FAILED') AS failed
                             FROM missions m)
               SELECT count(*)::int AS missions,
                      count(*) FILTER (WHERE mission_status = 'COMPLETED')::int AS reported_completed,
                      count(*) FILTER (WHERE mission_status = 'COMPLETED' AND failed > 0)::int AS completed_with_failures,
                      (SELECT count(*)::int FROM anomalies WHERE severity IN ('HIGH','CRITICAL')) AS serious_anomalies,
                      (SELECT count(*)::int FROM anomalies n JOIN missions m2 ON m2.mission_id = n.mission_id
                        WHERE n.severity IN ('HIGH','CRITICAL') AND m2.mission_status = 'COMPLETED')
                        AS serious_in_completed
                 FROM ms""")
        out["battery_bands"] = await self.reliability("battery_band", {})
        out["battery_bands_by_config"] = await self.db.fetch(
            "SELECT a.configuration_version AS config, (%s) AS band, count(*)::int AS n, "
            "count(*) FILTER (WHERE a.status = 'SUCCESS')::int AS n_success FROM actions a GROUP BY 1, 2 ORDER BY 1, 2"
            % BATTERY_BAND_SQL)
        out["failed_missions"] = await self.db.fetch(
            "SELECT mission_id, end_battery_pct FROM missions WHERE mission_status = 'FAILED' ORDER BY mission_id")
        out["config"] = await self.reliability("configuration_version", {})
        out["config_matched"] = await self.reliability("configuration_version", {"min_battery": 40})
        out["config_battery"] = await self.db.fetch(
            """SELECT m.configuration_version AS config, avg(m.start_battery_pct)::real AS mean_start_battery,
                      avg((m.start_battery_pct - m.end_battery_pct)::real / nullif(a.n, 0))::real AS drain_per_action,
                      min(m.end_battery_pct)::int AS min_end_battery
                 FROM missions m JOIN (SELECT mission_id, count(*) AS n FROM actions GROUP BY 1) a
                   ON a.mission_id = m.mission_id GROUP BY 1 ORDER BY 1""")
        out["config_action_type"] = await self.db.fetch(
            "SELECT a.configuration_version AS config, a.action_type, count(*)::int AS n, "
            "count(*) FILTER (WHERE a.status = 'SUCCESS')::int AS n_success FROM actions a GROUP BY 1, 2 ORDER BY 2, 1")
        out["locations"] = await self.db.fetch(
            """SELECT a.location_id, l.location_name, l.environment, count(*)::int AS n,
                      count(*) FILTER (WHERE a.status = 'SUCCESS')::int AS n_success
                 FROM actions a LEFT JOIN locations l ON l.location_id = a.location_id
                GROUP BY 1, 2, 3 ORDER BY count(*) FILTER (WHERE a.status = 'SUCCESS')::real / count(*), 1""")
        out["localization_by_env"] = await self.db.fetch(
            "SELECT l.environment, count(*)::int AS n FROM actions a JOIN locations l ON l.location_id = a.location_id "
            "WHERE a.failure_type = 'LOCALIZATION_FAILURE' GROUP BY 1 ORDER BY 2 DESC")
        out["action_types"] = await self.reliability("action_type", {})
        out["repeat_offenders"] = await self.db.fetch(
            """SELECT n.asset_id, s.asset_name, s.criticality, count(*)::int AS anomalies,
                      count(*) FILTER (WHERE n.severity IN ('HIGH','CRITICAL'))::int AS serious
                 FROM anomalies n LEFT JOIN assets s ON s.asset_id = n.asset_id
                GROUP BY 1, 2, 3 ORDER BY 4 DESC, 1 LIMIT 5""")
        out["severity_vs_criticality"] = await self.db.fetch(
            "SELECT n.severity, s.criticality, count(*)::int AS n FROM anomalies n JOIN assets s "
            "ON s.asset_id = n.asset_id GROUP BY 1, 2 ORDER BY 1, 2")
        out["readings_quality"] = await self.db.fetch(
            "SELECT r.quality, a.status, count(*)::int AS n FROM readings r JOIN actions a ON a.action_id = r.action_id "
            "GROUP BY 1, 2 ORDER BY 1, 2")
        out["live"] = await self.db.fetchone(
            """SELECT (SELECT count(*)::int FROM events) AS events_ingested,
                      (SELECT count(*)::int FROM events WHERE payload -> 'triage' ->> 'kind' IN ('action', 'mission'))
                        AS events_triaged,
                      (SELECT count(*)::int FROM flags WHERE escalated) AS flags_escalated,
                      (SELECT count(*)::int FROM flags WHERE escalated AND status = 'OPEN') AS flags_open,
                      (SELECT coalesce(json_object_agg(severity, n), '{}'::json) FROM (
                          SELECT severity, count(*) AS n FROM flags WHERE escalated AND status = 'OPEN'
                           GROUP BY 1) x) AS open_by_severity,
                      (SELECT count(*)::int FROM assessments WHERE state = 'DONE') AS explained""")
        out["stored"] = await self.db.fetch("SELECT key, label, value, detail, source, synced_at FROM insights "
                                            "ORDER BY key")
        return out

    # ════ assets / locations ════════════════════════════════════════════════════════════════════
    async def list_assets(self) -> List[Dict[str, Any]]:
        return await self.db.fetch(
            """SELECT s.*, l.location_name, l.environment,
                      (SELECT count(*)::int FROM actions a WHERE a.asset_id = s.asset_id) AS actions,
                      (SELECT count(*)::int FROM actions a WHERE a.asset_id = s.asset_id AND a.status = 'FAILED')
                        AS failed_actions,
                      (SELECT count(*)::int FROM anomalies n WHERE n.asset_id = s.asset_id) AS anomalies,
                      (SELECT count(*)::int FROM anomalies n WHERE n.asset_id = s.asset_id
                          AND n.severity IN ('HIGH','CRITICAL')) AS serious_anomalies,
                      (SELECT count(*)::int FROM flags f WHERE f.asset_id = s.asset_id AND f.escalated
                          AND f.status = 'OPEN') AS open_flags
                 FROM assets s LEFT JOIN locations l ON l.location_id = s.location_id
                ORDER BY CASE s.criticality WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2 WHEN 'MEDIUM' THEN 3 ELSE 4 END,
                         s.asset_id""")

    async def asset_detail(self, asset_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetchone(
            """SELECT row_to_json(s) AS asset, row_to_json(l) AS location,
                      (SELECT coalesce(json_agg(r ORDER BY r.timestamp), '[]'::json) FROM readings r
                        WHERE r.asset_id = s.asset_id) AS readings,
                      (SELECT coalesce(json_agg(n ORDER BY n.timestamp), '[]'::json) FROM anomalies n
                        WHERE n.asset_id = s.asset_id) AS anomalies,
                      (SELECT coalesce(json_agg(f ORDER BY f.created_at DESC), '[]'::json) FROM (
                          SELECT flag_id, title, severity, status, score, escalated, created_at FROM flags
                           WHERE asset_id = s.asset_id) f) AS flags,
                      (SELECT json_build_object('n', count(*), 'n_success', count(*) FILTER (WHERE status = 'SUCCESS'))
                         FROM actions a WHERE a.asset_id = s.asset_id) AS action_stats
                 FROM assets s LEFT JOIN locations l ON l.location_id = s.location_id WHERE s.asset_id = %s""",
            (asset_id,))
        return row if row and row.get("asset") else None

    async def list_locations(self) -> List[Dict[str, Any]]:
        return await self.db.fetch(
            """SELECT l.*, coalesce(a.n, 0) AS actions, coalesce(a.n_success, 0) AS successes,
                      coalesce(n.n, 0) AS anomalies,
                      (SELECT count(*)::int FROM assets s WHERE s.location_id = l.location_id) AS assets,
                      (SELECT count(*)::int FROM flags f WHERE f.location_id = l.location_id AND f.escalated
                          AND f.status = 'OPEN') AS open_flags,
                      a.failure_types
                 FROM locations l
                 LEFT JOIN (SELECT location_id, count(*)::int AS n,
                                   count(*) FILTER (WHERE status = 'SUCCESS')::int AS n_success,
                                   (SELECT json_object_agg(ft, c) FROM (
                                       SELECT coalesce(x.failure_type, 'UNKNOWN') AS ft, count(*) AS c FROM actions x
                                        WHERE x.location_id = actions.location_id AND x.status = 'FAILED'
                                        GROUP BY 1) y) AS failure_types
                              FROM actions GROUP BY 1) a ON a.location_id = l.location_id
                 LEFT JOIN (SELECT location_id, count(*)::int AS n FROM anomalies GROUP BY 1) n
                        ON n.location_id = l.location_id
                ORDER BY l.location_id""")

    async def location_detail(self, location_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetchone(
            """SELECT row_to_json(l) AS location,
                      (SELECT coalesce(json_agg(s ORDER BY s.asset_id), '[]'::json) FROM assets s
                        WHERE s.location_id = l.location_id) AS assets,
                      (SELECT coalesce(json_agg(x), '[]'::json) FROM (
                          SELECT action_type, count(*)::int AS n,
                                 count(*) FILTER (WHERE status = 'SUCCESS')::int AS n_success
                            FROM actions WHERE location_id = l.location_id GROUP BY 1 ORDER BY 1) x) AS by_action_type,
                      (SELECT coalesce(json_object_agg(ft, c), '{}'::json) FROM (
                          SELECT coalesce(failure_type, 'UNKNOWN') AS ft, count(*) AS c FROM actions
                           WHERE location_id = l.location_id AND status = 'FAILED' GROUP BY 1) y) AS failure_types,
                      (SELECT coalesce(json_agg(n ORDER BY n.timestamp DESC), '[]'::json) FROM anomalies n
                        WHERE n.location_id = l.location_id) AS anomalies,
                      (SELECT coalesce(json_agg(f ORDER BY f.created_at DESC), '[]'::json) FROM (
                          SELECT flag_id, title, severity, status, score, escalated, created_at FROM flags
                           WHERE location_id = l.location_id) f) AS flags
                 FROM locations l WHERE l.location_id = %s""", (location_id,))
        return row if row and row.get("location") else None

    async def failure_type_rows(self, column: str, key: str) -> List[Dict[str, Any]]:
        assert column in ("location_id", "asset_id")
        return await self.db.fetch("SELECT failure_type, count(*)::int AS n FROM actions WHERE %s = %%s AND "
                                   "status = 'FAILED' GROUP BY 1 ORDER BY 2 DESC" % column, (key,))

    # ════ search ════════════════════════════════════════════════════════════════════════════════
    async def search_hybrid(self, vec: str, q: str, kinds: Optional[List[str]], asset: Optional[str],
                            location: Optional[str], mission: Optional[str], k: int) -> List[Dict[str, Any]]:
        return await self.db.fetch(
            "SELECT * FROM match_documents_hybrid(%s::vector, %s, %s::text[], %s, %s, %s, %s)",
            (vec, q, kinds, asset, location, mission, k))

    async def search_fulltext(self, q: str, kinds: Optional[List[str]], asset: Optional[str],
                              location: Optional[str], mission: Optional[str], k: int) -> List[Dict[str, Any]]:
        """Postgres full-text on documents.content_tsv (terms ORed, like the hybrid function's lexical side),
        then an exact-substring pass for ids such as 'HX-204' if nothing matched."""
        filt, fp = [], []
        for col, val in (("kind = ANY(%s)", kinds), ("asset_id = %s", asset), ("location_id = %s", location),
                         ("mission_id = %s", mission)):
            if val:
                filt.append(col)
                fp.append(val)
        w = (" AND " + " AND ".join(filt)) if filt else ""
        rows = await self.db.fetch(
            "WITH q AS (SELECT CASE WHEN btrim(%%s) = '' THEN NULL ELSE "
            "replace(plainto_tsquery('english', %%s)::text, ' & ', ' | ')::tsquery END AS tsq) "
            "SELECT d.doc_id, d.kind, d.title, d.content, d.metadata, d.asset_id, d.location_id, d.mission_id, "
            "d.action_id, d.ts, ts_rank_cd(d.content_tsv, q.tsq)::real AS score FROM documents d, q "
            "WHERE q.tsq IS NOT NULL AND d.content_tsv @@ q.tsq%s ORDER BY score DESC, d.doc_id LIMIT %%s" % w,
            [q, q] + fp + [k])
        if rows:
            return rows
        return await self.db.fetch(
            "SELECT d.doc_id, d.kind, d.title, d.content, d.metadata, d.asset_id, d.location_id, d.mission_id, "
            "d.action_id, d.ts, 0.0::real AS score FROM documents d WHERE (d.content ILIKE %%s OR d.title ILIKE %%s)%s "
            "ORDER BY d.doc_id LIMIT %%s" % w, ["%" + q + "%", "%" + q + "%"] + fp + [k])

    # ════ ops ═══════════════════════════════════════════════════════════════════════════════════
    async def model_call_stats(self, since_minutes: int, system: Optional[str]) -> List[Dict[str, Any]]:
        where, params = ["m.ts >= now() - make_interval(mins => %s)"], [since_minutes]
        if system:
            where.append("m.system = %s")
            params.append(system)
        return await self.logs.fetch(
            """SELECT m.system, m.model, m.endpoint, m.device, count(*)::int AS calls,
                      count(*) FILTER (WHERE NOT m.ok)::int AS errors,
                      (percentile_cont(0.5)  WITHIN GROUP (ORDER BY m.latency_ms) FILTER (WHERE m.ok))::real AS p50_ms,
                      (percentile_cont(0.95) WITHIN GROUP (ORDER BY m.latency_ms) FILTER (WHERE m.ok))::real AS p95_ms,
                      (avg(m.latency_ms) FILTER (WHERE m.ok))::real AS mean_ms,
                      (percentile_cont(0.5) WITHIN GROUP (ORDER BY m.tokens_per_sec) FILTER (WHERE m.ok))::real
                        AS p50_tokens_per_sec,
                      (avg(m.tokens_per_sec) FILTER (WHERE m.ok))::real AS mean_tokens_per_sec,
                      coalesce(sum(m.input_tokens), 0)::bigint AS input_tokens,
                      coalesce(sum(m.output_tokens), 0)::bigint AS output_tokens,
                      min(m.ts) AS first_ts, max(m.ts) AS last_ts
                 FROM app.model_calls m WHERE %s
                GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3""" % " AND ".join(where), params)

    async def recent_model_calls(self, limit: int) -> List[Dict[str, Any]]:
        return await self.logs.fetch("SELECT * FROM app.model_calls ORDER BY ts DESC LIMIT %s", (limit,))

    async def jobs(self, limit: int) -> List[Dict[str, Any]]:
        return await self.logs.fetch("SELECT * FROM app.job_runs ORDER BY started_at DESC LIMIT %s", (limit,))

    async def triage_counts(self) -> Dict[str, Any]:
        return await self.db.fetchone(
            """SELECT count(*) FILTER (WHERE payload -> 'triage' ->> 'kind' IN ('action', 'mission'))::int AS triaged,
                      count(*) FILTER (WHERE payload -> 'triage' ->> 'kind' = 'pre_action')::int AS forecasts,
                      count(*) FILTER (WHERE (payload -> 'triage' ->> 'escalated')::boolean)::int AS escalated,
                      count(*) FILTER (WHERE payload -> 'triage' ->> 'agreement' = 'laya_unavailable')::int
                        AS rules_only
                 FROM events""") or {}

    # ════ stepped replay (REPLAY_MODE=stepped; db/migrations/010_replay_runs.sql) ═══════════════════════════════
    # At most one row is 'running' (partial unique index). Only the holder of the lease token writes progress.
    _RUN = "*, now() AS db_now"

    async def replay_create(self, params: Dict[str, Any], event_ids: List[str],
                            job_run_id: Optional[int]) -> Optional[Dict[str, Any]]:
        """New running run, or None when one is already running."""
        return await self.db.fetchone(
            "INSERT INTO replay_runs (params, event_ids, job_run_id) VALUES (%%s, %%s, %%s) "
            "ON CONFLICT DO NOTHING RETURNING %s" % self._RUN, (Jsonb(params), list(event_ids), job_run_id))

    async def replay_latest(self) -> Optional[Dict[str, Any]]:
        return await self.db.fetchone("SELECT %s FROM replay_runs ORDER BY run_id DESC LIMIT 1" % self._RUN)

    async def replay_claim(self, token: str, lease_s: float, run_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Take the running run's lease if it is free or expired; None if there is no such run or it is held."""
        return await self.db.fetchone(
            "UPDATE replay_runs SET lease_token = %%s, lease_until = now() + make_interval(secs => %%s), "
            "updated_at = now() WHERE state = 'running' AND (lease_until IS NULL OR lease_until < now()) "
            "AND (%%s::bigint IS NULL OR run_id = %%s::bigint) RETURNING %s" % self._RUN,
            (token, float(lease_s), run_id, run_id))

    async def replay_advance(self, run_id: int, token: str, next_index: int, counts: Dict[str, Any],
                             last_event: Optional[Dict[str, Any]]) -> bool:
        """Record progress after one event; False if this step no longer holds the lease (stopped, expired)."""
        row = await self.db.fetchone(
            "UPDATE replay_runs SET next_index = %s, counts = %s, last_event = %s, updated_at = now(), "
            "lease_until = greatest(lease_until, now() + interval '30 seconds') "
            "WHERE run_id = %s AND lease_token = %s AND state = 'running' RETURNING run_id",
            (next_index, Jsonb(counts), Jsonb(last_event) if last_event is not None else None, run_id, token))
        return row is not None

    async def replay_release(self, run_id: int, token: str, state: Optional[str] = None,
                             error: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Give the lease back; with `state` ('finished' | 'error') also end the run."""
        return await self.db.fetchone(
            "UPDATE replay_runs SET lease_token = NULL, lease_until = NULL, updated_at = now(), "
            "state = COALESCE(%%s::text, state), error = COALESCE(%%s::text, error), "
            "finished_at = CASE WHEN %%s::text IS NULL THEN finished_at ELSE now() END "
            "WHERE run_id = %%s AND lease_token = %%s AND state = 'running' RETURNING %s" % self._RUN,
            (state, error, state, run_id, token))

    async def replay_stop(self) -> Optional[Dict[str, Any]]:
        """Stop the running run (whoever holds its lease stops at its next event). None if none is running."""
        return await self.db.fetchone(
            "UPDATE replay_runs SET state = 'stopped', finished_at = now(), lease_token = NULL, lease_until = NULL, "
            "updated_at = now() WHERE state = 'running' RETURNING %s" % self._RUN)

    async def reset_demo(self, clear_predictions: bool) -> Dict[str, int]:
        """Clear the live pipeline (events, flags, assessments, acknowledgements, demo-generated documents).
        Kit tables, raw.* and app.* (append-only) are untouched."""
        counts: Dict[str, int] = {}
        async with self.db.transaction() as cur:
            await cur.execute("DELETE FROM documents WHERE doc_id LIKE 'note:%%' OR doc_id LIKE 'explanation:%%' "
                              "OR doc_id LIKE 'incident:flag-%%'")
            counts["documents"] = cur.rowcount
            for t in ("acknowledgements", "assessments", "flags", "events"):
                await cur.execute("DELETE FROM %s" % t)
                counts[t] = cur.rowcount
            if clear_predictions:
                await cur.execute("DELETE FROM system1_predictions")
                counts["system1_predictions"] = cur.rowcount
        return counts
