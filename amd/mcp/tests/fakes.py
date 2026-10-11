"""In-memory fakes: a Repo over a tiny SPOT fixture and a bag-of-words embedder (similar text -> high cosine)."""
from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Dict, List, Optional

from spot_mcp.config import Settings
from spot_mcp.embed import EmbeddingError

DIM = 64


def settings(**kw) -> Settings:
    base = dict(database_url=None, logs_database_url=None, embed_url="http://fake", embed_model="fake",
                embed_dim=DIM, embed_query_prefix="", embed_timeout_s=1.0, min_similarity=0.40,
                packet_token_budget=1500, statement_timeout_ms=1000, pool_max=1, log_calls=True)
    base.update(kw)
    return Settings(**base)


def bow(text: str) -> List[float]:
    v = [0.0] * DIM
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if len(w) < 3:
            continue
        h = int(hashlib.md5(w.encode()).hexdigest(), 16)
        v[h % DIM] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cos(a: List[float], b: List[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class FakeEmbedder:
    def __init__(self, fail: bool = False):
        self.fail, self.calls = fail, []

    async def embed_queries(self, texts, request_id=None):
        self.calls.append(list(texts))
        if self.fail:
            raise EmbeddingError("embedding service unreachable at http://fake (ConnectError)")
        return [bow(t) for t in texts]

    async def close(self):
        pass


class FakeSink:
    def __init__(self, broken: bool = False):
        self.tools, self.models, self.broken = [], [], broken

    def tool_call(self, row):
        if self.broken:
            raise RuntimeError("logs DB down")
        self.tools.append(row)

    def model_call(self, row):
        self.models.append(row)

    async def close(self):
        pass


ACTIONS = {
    "A0136": {"action_id": "A0136", "mission_id": "M014", "sequence": 6, "timestamp": "2026-09-06T02:15:36",
              "location_id": "WP07", "asset_id": "HX-204", "action_type": "GAUGE_READING", "battery_pct": 22,
              "status": "SUCCESS", "failure_type": None, "duration_sec": 60, "configuration_version": "V1",
              "synthetic": True},
    "A0099": {"action_id": "A0099", "mission_id": "M010", "sequence": 3, "timestamp": "2026-09-04T10:00:00",
              "location_id": "WP09", "asset_id": "PUMP-205", "action_type": "NAVIGATION", "battery_pct": 70,
              "status": "FAILED", "failure_type": "LOCALIZATION_FAILURE", "duration_sec": 50,
              "configuration_version": "V1"},
}
ASSETS = {"HX-204": {"asset_id": "HX-204", "asset_name": "Heat Exchanger 04", "asset_type": "Heat Exchanger",
                     "location_id": "WP07", "criticality": "HIGH"},
          "PUMP-205": {"asset_id": "PUMP-205", "asset_name": "Pump 05", "asset_type": "Pump",
                       "location_id": "WP09", "criticality": "MEDIUM"}}
LOCATIONS = {"WP07": {"location_id": "WP07", "location_name": "Heat Exchanger Bay", "zone_type": "PROCESS",
                      "environment": "INDOOR_DRY"},
             "WP09": {"location_id": "WP09", "location_name": "Turbine Hall South", "zone_type": "TURBINE",
                      "environment": "HIGH_NOISE"}}
READINGS = {"A0136": [{"reading_id": "R0002", "action_id": "A0136", "asset_id": "HX-204",
                       "timestamp": "2026-09-06T02:15:36", "measurement_type": "PRESSURE_PSI", "value": 146.8,
                       "unit": "psi", "quality": "GOOD"}]}
ANOMALIES = {"A0136": [{"anomaly_id": "AN002", "action_id": "A0136", "mission_id": "M014", "asset_id": "HX-204",
                        "timestamp": "2026-09-06T02:15:36", "anomaly_type": "GAUGE_OUT_OF_RANGE", "severity": "HIGH",
                        "operator_review_required": True, "description": "Gauge out of range detected."}]}
MISSIONS = {"M014": {"mission_id": "M014", "mission_name": "Autowalk Inspection Route 14", "configuration_version": "V1",
                     "start_time": "2026-09-06T02:00:00", "end_time": "2026-09-06T02:31:00", "duration_min": 31,
                     "start_battery_pct": 70, "end_battery_pct": 18, "mission_status": "COMPLETED"},
            "M010": {"mission_id": "M010", "mission_name": "Route 10", "configuration_version": "V1",
                     "start_battery_pct": 80, "end_battery_pct": 40, "mission_status": "COMPLETED"}}
FLAGS = {
    1: {"flag": {"flag_id": 1, "event_id": "E00300", "mission_id": "M014", "action_id": "A0136", "asset_id": "HX-204",
                 "location_id": "WP07", "score": 82.0, "severity": "HIGH", "title": "Gauge out of range on HX-204",
                 "reasons": [{"rule": "gauge_out_of_range", "detail": "PRESSURE_PSI 146.8 > 140", "weight": 1}],
                 "escalated": True, "status": "OPEN", "created_at": "2026-09-06T02:16:00"},
        "event": {"event_id": "E00300", "action_id": "A0136", "mission_id": "M014", "ts": "2026-09-06T02:15:36",
                  "payload": {"big": "x" * 500}},
        "acks": [], "assessment": None},
    2: {"flag": {"flag_id": 2, "action_id": "A9999", "mission_id": "M099", "score": 50, "severity": "MEDIUM",
                 "title": "Orphan flag", "reasons": [], "escalated": False, "status": "OPEN"},
        "event": None, "acks": [], "assessment": None},
    3: {"flag": {"flag_id": 3, "score": 10, "severity": "LOW", "title": "No subject", "reasons": [],
                 "escalated": False, "status": "OPEN"}, "event": None, "acks": [], "assessment": None},
    4: {"flag": {"flag_id": 4, "mission_id": "M014", "score": 60, "severity": "MEDIUM",
                 "title": "Mission reported COMPLETED but hides failures", "reasons": [], "escalated": True,
                 "status": "OPEN"}, "event": None,
        "acks": [{"ack_id": 9, "action": "NOTE", "note": "Checked with tech", "operator": "op1"}], "assessment": None},
}
DOCS = [
    {"doc_id": "incident:AN002", "kind": "incident", "asset_id": "HX-204", "action_id": "A0136",
     "title": "GAUGE_OUT_OF_RANGE HIGH on HX-204", "content": "Gauge out of range on heat exchanger HX-204 pressure high"},
    {"doc_id": "incident:flag-1", "kind": "incident", "asset_id": "HX-204", "action_id": "A0136",
     "title": "flag 1", "content": "Gauge out of range on HX-204 pressure psi heat exchanger flag"},
    {"doc_id": "incident:AN007", "kind": "incident", "asset_id": "HX-208", "action_id": "A0050",
     "title": "GAUGE_OUT_OF_RANGE HIGH on HX-208", "content": "Gauge out of range on heat exchanger HX-208 pressure "
                                                             "high during gauge reading, technician replaced gauge"},
    {"doc_id": "incident:AN010", "kind": "incident", "asset_id": "PANEL-212", "action_id": "A0060",
     "title": "VISUAL_DEFECT on PANEL-212", "content": "Visual defect scorch marks electrical panel"},
    {"doc_id": "event:E00100", "kind": "event", "asset_id": "HX-204", "action_id": "A0040",
     "title": "Gauge reading HX-204", "content": "SPOT read the pressure gauge on heat exchanger HX-204: 131 psi GOOD"},
    {"doc_id": "note:5", "kind": "operator_note", "asset_id": "HX-204", "action_id": "A0040",
     "title": "Operator note", "content": "Pressure on HX-204 creeping up, scheduled maintenance for gauge exchanger"},
    {"doc_id": "manual:p42-1", "kind": "manual", "title": "Docking and charging",
     "content": "When battery is low Spot should return to the dock for charging before continuing the mission"},
    {"doc_id": "manual:p30-2", "kind": "manual", "title": "Autowalk gauge reading",
     "content": "Autowalk gauge reading actions capture an image of the gauge; operator should handle out of range gauge "
                "readings during an inspection mission by reviewing the image"},
    {"doc_id": "policy:gauge_out_of_range", "kind": "policy", "title": "Rule gauge_out_of_range",
     "content": "Triage rule: a trusted reading above the safe band on a HIGH criticality asset is priority P2 equipment"},
    {"doc_id": "policy:battery_low", "kind": "policy", "title": "Rule battery_low",
     "content": "Triage rule: battery at or below 25 percent is priority P2 robot health"},
    {"doc_id": "explanation:3", "kind": "explanation", "title": "Explanation of flag 0",
     "content": "The gauge on HX-208 read above its safe band; recommended SCHEDULE_MAINTENANCE"},
]
for _d in DOCS:
    _d["embedding"] = bow((_d.get("title") or "") + " " + _d["content"])

RAW = {"M014": [{"log_id": 11, "source": "replay.ndjson", "event_id": "E00299", "event_type": "ACTION_STARTED",
                 "event_ts": "2026-09-06T02:15:00", "payload": {"event_id": "E00299"}},
                {"log_id": 12, "source": "replay.ndjson", "event_id": "E00300", "event_type": "ACTION_COMPLETED",
                 "event_ts": "2026-09-06T02:15:36", "payload": {"event_id": "E00300"}}]}

# (action_type, location_id, configuration_version, battery_band) -> (n, n_success)
COUNTS = [("GAUGE_READING", "WP07", "V1", "<=25", 9, 3), ("GAUGE_READING", "WP07", "V1", "61-80", 10, 9),
          ("NAVIGATION", "WP09", "V1", "61-80", 14, 9), ("VISUAL_INSPECTION", "WP01", "V2", "81+", 30, 28),
          ("GAUGE_READING", "WP07", "V2", "81+", 6, 6)]


# ── knowledge graph (contract §7) ───────────────────────────────────────────────────────────────
KG_NODES = {nid: {"node_id": nid, "kind": nid.split(":", 1)[0], "key": nid.split(":", 1)[1], "label": label,
                  "summary": summary, "doc_id": doc}
            for nid, label, summary, doc in [
    ("action:A0136", "GAUGE_READING on HX-204", "Gauge reading A0136 on HX-204 at WP07, SUCCESS", None),
    ("mission:M014", "Autowalk Inspection Route 14", "Mission M014, reported COMPLETED", None),
    ("flag:1", "Gauge out of range on HX-204", "Flag 1, HIGH", None),
    ("anomaly:AN002", "GAUGE_OUT_OF_RANGE HIGH on HX-204", "Gauge out of range HIGH on HX-204", "incident:AN002"),
    ("asset:HX-204", "Heat Exchanger 04", "Heat Exchanger 04, HIGH criticality, at WP07", None),
    ("asset:PUMP-205", "Pump 05", "Pump 05 at Turbine Hall South", None),
    ("location:WP07", "Heat Exchanger Bay", "Heat Exchanger Bay, process zone, indoor dry", None),
    ("location:WP09", "Turbine Hall South", "Turbine Hall South, turbine zone, high noise", None),
    ("environment:INDOOR_DRY", "INDOOR_DRY", "Indoor dry environment", None),
    ("environment:HIGH_NOISE", "HIGH_NOISE", "High noise environment near turbines", None),
    ("action_type:GAUGE_READING", "GAUGE_READING", "Gauge reading actions", None),
    ("action_type:NAVIGATION", "NAVIGATION", "Navigation actions between waypoints", None),
    ("failure_type:LOCALIZATION_FAILURE", "LOCALIZATION_FAILURE", "Robot lost its position on the map", None),
    ("anomaly_type:GAUGE_OUT_OF_RANGE", "GAUGE_OUT_OF_RANGE", "Gauge reading outside its safe band", None),
    ("policy_rule:gauge_out_of_range", "Gauge out of range", "Rule: trusted reading above the safe band",
     "policy:gauge_out_of_range"),
    ("topic:gauge", "gauge", "Gauges and gauge reading", None),
    ("topic:localization", "localization", "Localization, fiducials and getting lost in loud noisy areas", None),
    ("manual:p30-2", "Autowalk gauge reading", "Autowalk gauge reading captures an image of the gauge; review "
                                                "out of range readings by checking the captured image", "manual:p30-2"),
    ("manual:p50-1", "Localization and fiducials", "Spot localizes with fiducials; add fiducials where it gets lost",
     "manual:p50-1"),
    ("operator_note:5", "Operator note on HX-204", "Pressure on HX-204 creeping up, scheduled maintenance", "note:5"),
    # more distinct facts around HX-204, so a tight packet budget has a graph tail to trim (test_graph)
    ("failure_type:READING_UNRELIABLE", "READING_UNRELIABLE", "Reading could not be trusted (glare, blur)", None),
    ("failure_type:CAMERA_OCCLUDED", "CAMERA_OCCLUDED", "Camera view blocked during capture", None),
    ("topic:pressure", "pressure", "Pressure gauges, pressure bands and relief valves", None),
    ("topic:thermal", "thermal", "Thermal imaging and overheating", None),
] + [("action:A1%03d" % i, "GAUGE_READING on HX-204", "Routine gauge reading", None) for i in range(30)]}
# (src, rel, dst, weight, props, evidence)
KG_EDGES = [
    ("action:A0136", "part_of", "mission:M014", 1.0, {"source": "fk"}, None),
    ("action:A0136", "at", "location:WP07", 1.0, {"source": "fk"}, None),
    ("action:A0136", "inspects", "asset:HX-204", 1.0, {"source": "fk"}, None),
    ("action:A0136", "of_type", "action_type:GAUGE_READING", 1.0, {"source": "fk"}, None),
    ("anomaly:AN002", "during", "action:A0136", 1.0, {"source": "fk"}, "AN002"),
    ("anomaly:AN002", "raised_on", "asset:HX-204", 1.0, {"source": "fk"}, "AN002"),
    ("anomaly:AN002", "of_type", "anomaly_type:GAUGE_OUT_OF_RANGE", 1.0, {"source": "fk"}, "AN002"),
    ("asset:HX-204", "located_at", "location:WP07", 1.0, {"source": "fk"}, None),
    ("asset:PUMP-205", "located_at", "location:WP09", 1.0, {"source": "fk"}, None),
    ("location:WP07", "has_environment", "environment:INDOOR_DRY", 1.0, {"source": "fk"}, None),
    ("location:WP09", "has_environment", "environment:HIGH_NOISE", 1.0, {"source": "fk"}, None),
    ("flag:1", "flags", "action:A0136", 1.0, {"source": "fk"}, None),
    ("flag:1", "triggered", "policy_rule:gauge_out_of_range", 1.0, {"source": "fk"}, None),
    ("anomaly_type:GAUGE_OUT_OF_RANGE", "common_at", "asset:HX-204", 0.5, {"n": 3, "rate": 0.5, "source": "stat"},
     "stat:anomaly_type=GAUGE_OUT_OF_RANGE,asset_id=HX-204"),
    ("failure_type:LOCALIZATION_FAILURE", "common_at", "location:WP09", 0.43, {"n": 6, "of": 14, "rate": 0.43, "source": "stat"},
     None),
    ("action_type:NAVIGATION", "fails_more_in", "environment:HIGH_NOISE", 0.64,
     {"n": 14, "rate": 0.64, "overall": 0.81, "source": "stat"}, None),
    ("action_type:GAUGE_READING", "fails_more_in", "environment:INDOOR_DRY", 0.7,
     {"n": 10, "rate": 0.7, "overall_rate": 0.81, "source": "stat"}, None),
    ("policy_rule:gauge_out_of_range", "about", "anomaly_type:GAUGE_OUT_OF_RANGE", 1.0, {"source": "curated"}, None),
    ("policy_rule:gauge_out_of_range", "about", "topic:gauge", 1.0, {"source": "curated"}, None),
    ("anomaly_type:GAUGE_OUT_OF_RANGE", "relates_to", "topic:gauge", 1.0, {"source": "curated"}, None),
    ("action_type:GAUGE_READING", "relates_to", "topic:gauge", 1.0, {"source": "curated"}, None),
    ("failure_type:LOCALIZATION_FAILURE", "relates_to", "topic:localization", 1.0, {"source": "curated"}, None),
    ("manual:p30-2", "mentions", "topic:gauge", 3.0, {"source": "extract"}, "manual:p30-2"),
    ("manual:p30-2", "mentions", "action_type:GAUGE_READING", 2.0, {"source": "extract"}, "manual:p30-2"),
    ("manual:p50-1", "mentions", "topic:localization", 2.0, {"source": "extract"}, "manual:p50-1"),
    ("operator_note:5", "mentions", "asset:HX-204", 1.0, {"source": "extract"}, "note:5"),
    ("failure_type:READING_UNRELIABLE", "common_at", "asset:HX-204", 0.3, {"n": 2, "rate": 0.3, "source": "stat"},
     "stat:failure_type=READING_UNRELIABLE,asset_id=HX-204"),
    ("failure_type:CAMERA_OCCLUDED", "common_at", "location:WP07", 0.25, {"n": 2, "of": 8, "rate": 0.25,
                                                                         "source": "stat"},
     "stat:failure_type=CAMERA_OCCLUDED,location_id=WP07"),
    ("topic:pressure", "relates_to", "topic:gauge", 1.0, {"source": "curated"}, None),
    ("topic:thermal", "relates_to", "topic:pressure", 1.0, {"source": "curated"}, None),
] + [("action:A1%03d" % i, rel, dst, 1.0, {"source": "fk"}, None) for i in range(30)
     for rel, dst in (("at", "location:WP07"), ("inspects", "asset:HX-204"))]
for _n in KG_NODES.values():
    _n["embedding"] = bow(_n["label"] + " " + _n["summary"])


class GraphMissing(Exception):
    """Mimics psycopg's UndefinedFunction on a DB without migration 009."""
    sqlstate = "42883"


class FakeRepo:
    def __init__(self, hybrid_has_similarity: bool = False, graph: str = "ok"):
        """graph: 'ok' | 'missing' (no migration 009) | 'broken' (traversal errors) | 'no_details' (lookup fails)."""
        self.hybrid_has_similarity = hybrid_has_similarity
        self.graph = graph
        self.calls: List[str] = []
        self.graph_calls: List[tuple] = []

    def _graph_gate(self, name):
        self.graph_calls.append((name,))
        if self.graph == "missing":
            from spot_mcp.repo import GraphUnavailable
            try:
                raise GraphMissing("function graph_neighborhood(text[], ...) does not exist")
            except GraphMissing as exc:
                raise GraphUnavailable("knowledge graph not available in this database (UndefinedFunction; "
                                       "migration 009)") from exc
        if self.graph == "broken" and name == "graph_neighborhood":
            raise RuntimeError("statement timeout")

    async def match_kg_nodes(self, vec, kinds, k, min_sim):
        self._graph_gate("match_kg_nodes")
        rows = []
        for n in KG_NODES.values():
            if kinds and n["kind"] not in kinds:
                continue
            sim = cos(vec, n["embedding"])
            if sim >= min_sim:
                rows.append({**{x: n[x] for x in ("node_id", "kind", "label", "summary", "doc_id")}, "similarity": sim})
        return sorted(rows, key=lambda r: -r["similarity"])[:k]

    async def match_kg_nodes_text(self, text, kinds, k):
        self._graph_gate("match_kg_nodes_text")
        norm = lambda s: re.sub(r"[^a-z0-9]+", "", s.lower())  # noqa: E731 - like the real function
        q = norm(text)
        rows = []
        for n in KG_NODES.values():
            if kinds and n["kind"] not in kinds:
                continue
            key, label = norm(n["key"]), norm(n["label"])
            score = 1.0 if q and (q in (key, label) or text.strip().lower() == n["node_id"].lower()) else \
                0.6 if q and (q in key or q in label or (len(key) >= 4 and key in q)) else 0.0
            if score:
                rows.append({**{x: n[x] for x in ("node_id", "kind", "label", "summary", "doc_id")}, "score": score})
        return sorted(rows, key=lambda r: (-r["score"], r["node_id"]))[:k]

    async def graph_neighborhood(self, seeds, hops, rels, kinds, max_nodes):
        """Same semantics as db/migrations/009_graph.sql: simple paths over both edge directions; one row per reached
        node (best = shortest, then heaviest); seeds only when reached from another seed; kind_filter filters the
        RETURNED rows (the walk passes through any kind); order hop, weight desc, semantic kinds first; LIMIT last."""
        self._graph_gate("graph_neighborhood")
        self.graph_calls.append(("graph_neighborhood", tuple(seeds), hops, tuple(kinds or ()), max_nodes))
        hops = min(hops, 3)
        best: Dict[str, Dict[str, Any]] = {}
        frontier = [(s, [s]) for s in seeds if s in KG_NODES]
        for hop in range(1, hops + 1):
            nxt = []
            for node, path in frontier:
                for src, rel, dst, w, props, ev in KG_EDGES:
                    if node not in (src, dst):
                        continue
                    other = dst if node == src else src
                    if other in path or (rels and rel not in rels):
                        continue
                    row = {**{x: KG_NODES[other][x] for x in ("node_id", "kind", "label", "summary", "doc_id")},
                           "hop": hop, "via_rel": rel, "via_node": node, "path": path + [other], "weight": w,
                           "evidence": ev}
                    if other not in best or (best[other]["hop"], -best[other]["weight"]) > (hop, -w):
                        best[other] = row
                    nxt.append((other, path + [other]))
            frontier = nxt
        order = ["topic", "manual", "failure_type", "anomaly_type", "environment", "action_type", "policy_rule",
                 "location", "asset", "operator_note", "explanation", "anomaly", "flag", "mission", "action"]
        rows = [r for r in best.values() if not kinds or r["kind"] in kinds]
        return sorted(rows, key=lambda r: (r["hop"], -r["weight"], order.index(r["kind"]), r["node_id"]))[:max_nodes]

    async def kg_details(self, node_ids):
        self._graph_gate("kg_details")
        if self.graph == "no_details":
            raise RuntimeError("permission denied for table kg_edges")
        ids = set(node_ids)
        return {"nodes": [{x: KG_NODES[n][x] for x in ("node_id", "kind", "label", "summary", "doc_id")}
                          for n in ids if n in KG_NODES],
                "edges": [{"src": s, "rel": r, "dst": d, "weight": w, "props": p, "evidence": ev}
                          for s, r, d, w, p, ev in KG_EDGES if s in ids and d in ids]}

    async def flag(self, flag_id):
        self.calls.append("flag")
        return FLAGS.get(flag_id)

    async def action_bundle(self, action_id):
        a = ACTIONS.get(action_id)
        if not a:
            return None
        m = MISSIONS.get(a["mission_id"])
        return {"action": dict(a), "asset": ASSETS.get(a["asset_id"]), "location": LOCATIONS.get(a["location_id"]),
                "mission": m, "readings": READINGS.get(action_id, []), "anomalies": ANOMALIES.get(action_id, [])}

    async def mission_bundle(self, mission_id):
        m = MISSIONS.get(mission_id)
        if not m:
            return None
        acts = [a for a in ACTIONS.values() if a["mission_id"] == mission_id]
        if mission_id == "M014":
            return {"mission": m, "kpis": None, "actions_total": 10, "actions_failed": 3,
                    "failure_types": {"SENSOR_TIMEOUT": 2, "PATH_BLOCKED": 1}, "anomalies_by_severity": {"HIGH": 1},
                    "worst_anomaly": {"anomaly_id": "AN002", "anomaly_type": "GAUGE_OUT_OF_RANGE", "severity": "HIGH",
                                      "asset_id": "HX-204", "action_id": "A0136"}}
        return {"mission": m, "kpis": None, "actions_total": len(acts), "actions_failed": 0, "failure_types": {},
                "anomalies_by_severity": {}, "worst_anomaly": None}

    async def s1_predictions(self, action_id, mission_id):
        if action_id == "A0136":
            return [{"prediction_id": 501, "event_kind": "pre_action", "action_id": "A0136", "question": "will_fail",
                     "answer": "A", "probs": {"A": 0.71, "B": 0.29}, "model": "laya-ft"},
                    {"prediction_id": 502, "event_kind": "pre_action", "action_id": "A0136",
                     "question": "anomaly_expected", "answer": "A", "probs": {"A": 0.66, "B": 0.34}, "model": "laya-ft"},
                    {"prediction_id": 503, "event_kind": "action", "action_id": "A0136", "question": "priority",
                     "answer": "P1", "probs": {"P1": 0.8}, "model": "laya-ft"}]
        return []

    async def reliability_counts(self, filters, group_by):
        dims = ("action_type", "location_id", "configuration_version", "battery_band")
        agg: Dict[str, List[int]] = {}
        for row in COUNTS:
            rec = dict(zip(dims, row[:4]))
            if any(v is not None and rec[k] != v for k, v in filters.items()):
                continue
            g = rec[group_by] if group_by else "all"
            agg.setdefault(g, [0, 0])
            agg[g][0] += row[4]
            agg[g][1] += row[5]
        return [{"grp": g, "n": n, "n_success": s} for g, (n, s) in sorted(agg.items())]

    async def asset_profile(self, asset_id, exclude_flag_id):
        s = ASSETS.get(asset_id)
        if not s:
            return None
        return {"asset": s, "anomaly_count": 4, "anomalies_by_type": {"GAUGE_OUT_OF_RANGE": 3, "ABNORMAL_VIBRATION": 1},
                "recent_readings": [
                    {"reading_id": "R0001", "measurement_type": "PRESSURE_PSI", "value": 128, "quality": "GOOD"},
                    {"reading_id": "R0003", "measurement_type": "PRESSURE_PSI", "value": 135.5, "quality": "GOOD"},
                    {"reading_id": "R0002", "measurement_type": "PRESSURE_PSI", "value": 146.8, "quality": "GOOD"}],
                "prior_flags": [{"flag_id": 0, "title": "Pressure high on HX-204", "severity": "MEDIUM",
                                 "status": "RESOLVED", "notes": "Recalibrated gauge"}]}

    async def documents_by_id(self, doc_ids):
        return [{k: v for k, v in d.items() if k != "embedding"} for d in DOCS if d["doc_id"] in doc_ids]

    def _scored(self, vec, kinds, asset, location, mission):
        out = []
        for d in DOCS:
            if kinds and d["kind"] not in kinds:
                continue
            if asset and d.get("asset_id") != asset:
                continue
            row = {k: v for k, v in d.items() if k != "embedding"}
            row["similarity"] = cos(vec, d["embedding"])
            out.append(row)
        return sorted(out, key=lambda r: -r["similarity"])

    async def match(self, vec, kinds, asset, location, mission, k, min_sim):
        self.calls.append("match")
        return [r for r in self._scored(vec, kinds, asset, location, mission) if r["similarity"] >= min_sim][:k]

    async def match_hybrid(self, vec, text, kinds, asset, location, mission, k):
        self.calls.append("hybrid")
        rows = self._scored(vec, kinds, asset, location, mission)
        tokens = set(re.findall(r"[A-Z]+-\d+", text))
        for r in rows:
            hit = any(t in (r.get("title") or "") + r["content"] for t in tokens)
            r["score"] = r["similarity"] + (1.0 if hit else 0.0)
            if self.hybrid_has_similarity:
                r["text_match"] = hit
            else:
                del r["similarity"]
        return sorted(rows, key=lambda r: -r["score"])[:k]

    async def raw_event_log(self, mission_id, around_ts, limit):
        return RAW.get(mission_id, [])[:limit]
