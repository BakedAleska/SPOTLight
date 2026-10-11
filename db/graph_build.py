#!/usr/bin/env python3
"""Build the Graph RAG knowledge graph (contract §7) into kg_nodes / kg_edges. Idempotent.

  python db/graph_build.py               # build + embed new/changed node summaries (EMBED_URL)
  python db/graph_build.py --no-embed    # build only; summary-embedded nodes keep their old vector (or none)
  python db/graph_build.py --force-embed # re-embed every summary-embedded node
  python db/graph_build.py --dry-run     # build in memory, print counts, write nothing

Run after `setup.py migrate && setup.py seed && amd/embed/ingest.py` (db-init: `db/job.sh graph`, after ingest).
- Nodes are upserted (unchanged rows keep their updated_at); nodes this script made that no longer exist are deleted.
- Edges with props->>'source' IN ('fk','stat','extract','curated') are deleted and re-inserted in the same
  transaction, so readers see either the old or the new graph. Edges with any other source are left alone.
- Nodes and edges the live simulation wrote (props->>'source' = 'live', backend/app/services/live.py) are never
  overwritten, re-embedded or deleted, even when a live flag also appears in `flags`.
- Flag summaries list policy rules only (db/policy_rules.py), not the laya_verdict / laya_unavailable /
  rules_verdict entries, whose text can carry connection errors.
- Embeddings: document-backed nodes (mission, action, anomaly, flag, policy_rule, manual, operator_note,
  explanation) reuse documents.embedding. The others (asset, location, environment, action_type, failure_type,
  anomaly_type, topic) embed their `summary` over HTTP (no document prefix), in batches, and only when the summary
  or EMBED_MODEL changed (props.embedded_hash), batch by batch: vectors of the batches that succeeded are kept and
  the rest stay pending. If the embedding service is down the graph is still written. A server that reports another
  model or dimension than EMBED_MODEL / EMBED_DIM stops the run before any write.
- Logged to app.job_runs (job 'graph': nodes per kind, edges per rel) and each embedding batch to app.model_calls.

Env: DATABASE_URL, LOGS_DATABASE_URL, EMBED_URL, EMBED_MODEL, EMBED_DIM; GRAPH_EMBED_WAIT_S (default 120).
"""
import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "amd" / "embed"))
import spotdb  # noqa: E402  (puts amd/system1 on sys.path for the triage package)
import policy_rules as PR  # noqa: E402
from triage import policy as P  # noqa: E402

BUILD_SOURCES = ["fk", "stat", "extract", "curated"]
BUILT_BY = "graph_build"
DOC_BACKED = {"mission", "action", "anomaly", "flag", "policy_rule", "manual", "operator_note", "explanation"}
MIN_STAT_N = 2            # common_at: at least 2 failures / anomalies of the type at that place
MIN_ENV_N = 3             # fails_more_in: at least 3 actions of the type in that environment

FAILURE_TYPES = sorted(P.NAV_FAILURES | P.DATA_FAILURES | P.COMMS_FAILURES)
ANOMALY_TYPES = sorted(P.EQUIPMENT_ANOMALIES | P.NAV_ANOMALIES | P.DATA_ANOMALIES)


def anomaly_category(t: str) -> str:
    return ("equipment" if t in P.EQUIPMENT_ANOMALIES else "navigation" if t in P.NAV_ANOMALIES
            else "data_quality" if t in P.DATA_ANOMALIES else "unknown")


# ── curated vocabulary ────────────────────────────────────────────────────────────────────────────
# Topics: a small fixed vocabulary that links the Spot User Guide to the kit data. (label, description, synonyms)
TOPICS: Dict[str, Tuple[str, str, List[str]]] = {
    "localization": ("Localization", "Where Spot thinks it is on the recorded map: fiducials, relocalization, the "
                     "robot declaring itself lost during mission replay.",
                     [r"locali[sz]\w*", r"relocali[sz]\w*", r"fiducials?", r"(?:robot|spot) (?:is|was|declares itself) lost",
                      r"lost position", r"graph ?nav"]),
    "battery": ("Battery", "Spot's battery: state of charge, runtime, charging and hot-swapping. Kit evidence: action "
                "success falls to about a third at or below %d%% battery." % P.BATTERY_LOW,
                [r"batter(?:y|ies)", r"charg(?:e|es|ed|ing)", r"state of charge", r"soc", r"runtime"]),
    "docking": ("Docking", "Returning to the dock or charger to recharge; a low battery means planning the return.",
                [r"dock(?:s|ed|ing)?", r"charg(?:er|ers)", r"charging (?:station|dock)",
                 r"return(?:s|ing)? to (?:the )?(?:dock|charger|base)"]),
    "communication": ("Communication", "The robot-to-controller link: Wi-Fi / radio network, signal strength, "
                      "communication loss.",
                      [r"communicat\w*", r"wi-?fi", r"radio", r"network\w*", r"connection",
                       r"signal (?:loss|strength|degradation|degraded)", r"comms"]),
    "obstacles": ("Obstacles", "Obstacles, stairs, blocked paths and obstructions in Spot's way.",
                  [r"obstacles?", r"obstruct\w*", r"stairs?", r"stairways?", r"blocked", r"stuck", r"cliff\w*",
                   r"terrain", r"slippery"]),
    "thermal": ("Thermal", "Temperature: thermal inspections and abnormal temperature readings.",
                [r"therm(?:al|o\w*)", r"temperatures?", r"overheat\w*", r"infrared"]),
    "acoustic": ("Acoustic", "Sound and vibration: acoustic inspections, abnormal vibration, noisy areas.",
                 [r"acoustic\w*", r"vibrat\w*", r"(?<!signal )nois(?:e|y)", r"sound", r"microphones?", r"audio"]),
    "gauge": ("Gauge", "Pressure gauges: gauge readings and out-of-range values.",
              [r"gauges?", r"pressure", r"dials?", r"psi"]),
    "visual": ("Visual", "Cameras and images: visual inspections, PTZ / Spot CAM, visual defects.",
               [r"visual\w*", r"cameras?", r"images?", r"imagery", r"photos?", r"ptz", r"spot ?cam", r"zoom"]),
    "sensors": ("Sensors", "Sensor and payload data quality: timeouts, unreliable readings, signal noise, calibration.",
                [r"sensors?", r"lidar", r"payloads?", r"calibrat\w*", r"time ?outs?", r"timed out", r"unreliable",
                 r"signal noise"]),
    "safety": ("Safety", "Safe operation: hazards, E-Stop, falls and collisions; critical findings are escalated.",
               [r"safety", r"hazard\w*", r"warning", r"caution", r"e-?stop", r"emergency", r"injur\w*",
                r"collisions?", r"falls?"]),
}

# Synonyms per enum value (the humanized enum name, e.g. 'localization failure', is always added).
FAILURE_SYNONYMS: Dict[str, List[str]] = {
    "LOCALIZATION_FAILURE": [r"lost (?:its |the )?locali[sz]ation", r"(?:robot|spot) (?:is|was|declares itself) lost",
                             r"locali[sz]ation (?:failure|failed|fails|lost|loss)",
                             r"(?:fail(?:s|ed)?|unable|cannot|can't) (?:to )?locali[sz]e", r"lost position"],
    "OBSTACLE_DETECTION": [r"obstacle detection", r"obstacle avoidance", r"running into obstacles"],
    "PATH_BLOCKED": [r"path (?:is |was )?blocked", r"blocked (?:path|route|waypoint)",
                     r"(?:robot|spot) (?:is|becomes|gets) stuck", r"becomes stuck"],
    "COMMUNICATION_LOSS": [r"signal loss", r"loss of (?:communication|comms|connection|signal)",
                           r"(?:lost|loses|losing|lose) (?:its )?(?:connection|communication|comms|signal)",
                           r"connection (?:to the controller )?(?:has been |was |is )?lost",
                           r"without controller communication", r"(?:communication|comms|connection) (?:loss|dropped)"],
    "SENSOR_TIMEOUT": [r"sensor time ?out", r"timed out", r"time ?out"],
    "READING_UNRELIABLE": [r"unreliable (?:reading|readings|value|values|data)",
                           r"(?:reading|readings|value|data) (?:is |are |was |were )?unreliable"],
    "SIGNAL_NOISE": [r"signal noise", r"noisy (?:signal|reading|readings|data)"],
    "TARGET_NOT_DETECTED": [r"(?:could not|cannot|can't|unable to|fails? to|failed to|does not|doesn't) "
                            r"(?:see|detect|find|locate) (?:the |a )?(?:target|fiducial)"],
}
ANOMALY_SYNONYMS: Dict[str, List[str]] = {
    "ABNORMAL_VIBRATION": [r"(?:excessive|high) vibration", r"vibrat\w*"],
    "ABNORMAL_TEMPERATURE": [r"(?:excessive|high) temperature", r"overheat\w*", r"too hot", r"hot spot"],
    "GAUGE_OUT_OF_RANGE": [r"out[- ]of[- ]range"],
    "VISUAL_DEFECT": [r"corro(?:sion|ded)", r"cracks?", r"cracked", r"leak(?:s|ing|age)?"],
    "NAVIGATION_OBSTRUCTION": [r"obstruct\w*"],
    "LOCALIZATION_ISSUE": [r"locali[sz]ation (?:issue|problem)s?"],
    "SIGNAL_DEGRADATION": [r"degraded signal", r"weak (?:wi-?fi |network |radio )?signal",
                           r"poor (?:signal|network|video quality)", r"network instability"],
}
ACTION_SYNONYMS: Dict[str, List[str]] = {
    "NAVIGATION": [r"navigat\w*", r"autowalk", r"waypoints?", r"mission replay",
                   r"replay(?:s|ing)? (?:a |the )?mission"],
    "VISUAL_INSPECTION": [r"visual inspections?"],
    "THERMAL_INSPECTION": [r"thermal inspections?", r"thermal (?:camera|imag\w*)"],
    "ACOUSTIC_INSPECTION": [r"acoustic inspections?", r"acoustic (?:sensor|imag\w*)"],
    "GAUGE_READING": [r"gauge readings?", r"read(?:s|ing)? (?:a |the )?gauges?"],
}

# relates_to: failure_type / anomaly_type / action_type -> topic.
RELATES_TO: Dict[str, Dict[str, List[str]]] = {
    "failure_type": {
        "LOCALIZATION_FAILURE": ["localization"], "OBSTACLE_DETECTION": ["obstacles", "sensors"],
        "PATH_BLOCKED": ["obstacles"], "COMMUNICATION_LOSS": ["communication"], "SENSOR_TIMEOUT": ["sensors"],
        "READING_UNRELIABLE": ["sensors"], "SIGNAL_NOISE": ["sensors"], "TARGET_NOT_DETECTED": ["visual", "sensors"]},
    "anomaly_type": {
        "ABNORMAL_VIBRATION": ["acoustic"], "ABNORMAL_TEMPERATURE": ["thermal"], "GAUGE_OUT_OF_RANGE": ["gauge"],
        "VISUAL_DEFECT": ["visual"], "NAVIGATION_OBSTRUCTION": ["obstacles"], "LOCALIZATION_ISSUE": ["localization"],
        "SIGNAL_DEGRADATION": ["communication", "sensors"]},
    "action_type": {
        "NAVIGATION": ["localization", "obstacles"], "VISUAL_INSPECTION": ["visual"],
        "THERMAL_INSPECTION": ["thermal"], "ACOUSTIC_INSPECTION": ["acoustic"], "GAUGE_READING": ["gauge", "visual"]},
}


def _ft(names: Iterable[str]) -> List[str]:
    return ["failure_type:" + n for n in sorted(names)]


def _at(names: Iterable[str]) -> List[str]:
    return ["anomaly_type:" + n for n in sorted(names)]


def _tp(*names: str) -> List[str]:
    return ["topic:" + n for n in names]


ALL_FAILURES = P.NAV_FAILURES | P.DATA_FAILURES | P.COMMS_FAILURES
ALL_ANOMALIES = P.EQUIPMENT_ANOMALIES | P.NAV_ANOMALIES | P.DATA_ANOMALIES
EQUIPMENT_TOPICS = _tp("thermal", "gauge", "acoustic", "visual")
# about: policy_rule -> failure_type / anomaly_type / topic, read off the rule texts and policy.py's sets.
ABOUT: Dict[str, List[str]] = {
    "anomaly_critical": _tp("safety") + _at(P.EQUIPMENT_ANOMALIES),
    "anomaly_high_on_critical_asset": _tp("safety"),
    "failed_at_critical_battery": _tp("battery", "docking"),
    "comms_loss_low_battery": _ft(P.COMMS_FAILURES) + _tp("communication", "battery"),
    "anomaly_high": _tp("safety"),
    "anomaly_medium_on_critical_asset": _tp("safety"),
    "failed_on_critical_asset": _ft(ALL_FAILURES),
    "battery_low": _tp("battery", "docking"),
    "equipment_reading_out_of_band": _at(P.EQUIPMENT_ANOMALIES) + EQUIPMENT_TOPICS,
    "action_failed": _ft(ALL_FAILURES),
    "reading_untrusted": _ft(P.DATA_FAILURES) + _at(P.DATA_ANOMALIES) + _tp("sensors"),
    "battery_watch": _tp("battery", "docking"),
    "reading_out_of_band": EQUIPMENT_TOPICS + _tp("localization"),
    "mission_end_battery_low": _tp("battery"),
    "escalation": _tp("safety"),
    "safe_bands": EQUIPMENT_TOPICS + _tp("localization"),
    "reading_trust": _tp("sensors") + _ft(P.DATA_FAILURES),
    "category_assignment": _ft(ALL_FAILURES) + _at(ALL_ANOMALIES),
    "battery_thresholds": _tp("battery", "docking"),
}

# The curated maps must cover exactly policy.py's enums and rules, so they can't drift from the code.
assert set(FAILURE_SYNONYMS) == ALL_FAILURES, "FAILURE_SYNONYMS != policy.py failure types"
assert set(ANOMALY_SYNONYMS) == ALL_ANOMALIES, "ANOMALY_SYNONYMS != policy.py anomaly types"
assert set(RELATES_TO["failure_type"]) == ALL_FAILURES and set(RELATES_TO["anomaly_type"]) == ALL_ANOMALIES
assert set(ABOUT) <= set(PR.BY_NAME), "ABOUT names a rule that db/policy_rules.py doesn't have"
assert all(t[len("topic:"):] in TOPICS for ts in ABOUT.values() for t in ts if t.startswith("topic:"))
assert all(t in TOPICS for m in RELATES_TO.values() for ts in m.values() for t in ts)


def _rx(patterns: List[str]) -> "re.Pattern":
    return re.compile(r"\b(?:%s)\b" % "|".join("(?:%s)" % p for p in patterns), re.I)


def _enum_rx(name: str, synonyms: List[str]) -> "re.Pattern":
    return _rx([r"[ _-]".join(re.escape(w) for w in name.lower().split("_"))] + synonyms)


VOCAB: Dict[str, "re.Pattern"] = {}
VOCAB.update({"failure_type:" + k: _enum_rx(k, v) for k, v in FAILURE_SYNONYMS.items()})
VOCAB.update({"anomaly_type:" + k: _enum_rx(k, v) for k, v in ANOMALY_SYNONYMS.items()})
VOCAB.update({"action_type:" + k: _enum_rx(k, v) for k, v in ACTION_SYNONYMS.items()})
VOCAB.update({"topic:" + k: _rx(v[2]) for k, v in TOPICS.items()})
WP_RX = re.compile(r"\bWP[- ]?(\d{1,2})\b", re.I)


class Extractor:
    """Mentions in free text: asset ids (HX-204, PANEL-212, PUMP-205), WP ids, location / asset names, and the
    synonym dictionary above (failure / anomaly / action types, topics). Returns {node_id: (hits, terms)}."""

    def __init__(self, assets: List[dict], locations: List[dict]):
        self.asset_ids = {a["asset_id"].upper() for a in assets}
        self.wp_ids = {l["location_id"].upper() for l in locations}
        prefixes = sorted({a.split("-")[0] for a in self.asset_ids if "-" in a}, key=len, reverse=True)
        self.asset_rx = re.compile(r"\b(%s)[- ]?(\d{2,4})\b" % "|".join(map(re.escape, prefixes)), re.I) if prefixes else None
        self.names = [(re.compile(r"\b%s\b" % re.escape(a["asset_name"]), re.I), "asset:" + a["asset_id"])
                      for a in assets if a.get("asset_name")]
        self.names += [(re.compile(r"\b%s\b" % re.escape(l["location_name"]), re.I), "location:" + l["location_id"])
                       for l in locations if l.get("location_name")]

    def __call__(self, text: Optional[str]) -> Dict[str, Tuple[int, List[str]]]:
        hits: Counter = Counter()
        terms: Dict[str, set] = defaultdict(set)
        if not text:
            return {}

        def add(node: str, term: str) -> None:
            hits[node] += 1
            terms[node].add(term.lower())

        if self.asset_rx:
            for m in self.asset_rx.finditer(text):
                aid = "%s-%s" % (m.group(1).upper(), m.group(2))
                if aid in self.asset_ids:
                    add("asset:" + aid, m.group(0))
        for m in WP_RX.finditer(text):
            wp = "WP%02d" % int(m.group(1))
            if wp in self.wp_ids:
                add("location:" + wp, m.group(0))
        for rx, node in self.names:
            for m in rx.finditer(text):
                add(node, m.group(0))
        for node, rx in VOCAB.items():
            for m in rx.finditer(text):
                add(node, m.group(0))
        return {n: (c, sorted(terms[n])[:5]) for n, c in hits.items()}


# ── helpers ───────────────────────────────────────────────────────────────────────────────────────
def h(s: Optional[str]) -> str:
    """'LOCALIZATION_FAILURE' -> 'localization failure'."""
    return (s or "").replace("_", " ").lower()


def pct(x: float) -> str:
    return "%d%%" % round(100 * x)


def counts_text(c: Counter, limit: int = 4, fmt=h) -> str:
    """'3 reading unreliable, 2 sensor timeout + 2 more', most frequent first."""
    items = sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))
    s = ", ".join("%d %s" % (n, fmt(k)) for k, n in items[:limit])
    return s + (" + %d more" % (len(items) - limit) if len(items) > limit else "")


def ranked(c: Counter, limit: int, fmt=h) -> str:
    """'WP05 Pipe Gallery (5), WP06 Compressor Area (1)', most frequent first, '+ N more' past the limit."""
    items = sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))
    s = ", ".join("%s (%d)" % (fmt(k), n) for k, n in items[:limit])
    return s + (" + %d more" % (len(items) - limit) if len(items) > limit else "")


def sentence(text: str, n: int) -> str:
    """The first sentence of `text`, clipped to ~n chars, ending in '.' or '...'."""
    first = clip(text.split(". ")[0], n)
    return first if first.endswith(("...", ".")) else first + "."


def success_text(actions: List[dict]) -> str:
    if not actions:
        return "no actions"
    ok = sum(a["status"] == "SUCCESS" for a in actions)
    failed = Counter(a["failure_type"] or "UNKNOWN" for a in actions if a["status"] == "FAILED")
    s = "%d actions, %s success" % (len(actions), pct(ok / len(actions)))
    if failed:
        s += ", %d failed (%s)" % (sum(failed.values()), counts_text(failed))
    return s


SEV_ORDER = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


def anomalies_text(anoms: List[dict]) -> str:
    if not anoms:
        return "no anomalies"
    worst = max((a["severity"] for a in anoms), key=lambda s: SEV_ORDER.get(s, 0))
    return "%d anomal%s (%s; worst %s)" % (len(anoms), "y" if len(anoms) == 1 else "ies",
                                          counts_text(Counter(a["anomaly_type"] for a in anoms), 3), worst)


def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def clip(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0]
    return cut.rstrip(",;:.") + "..."


def reasons(fl: dict) -> List[dict]:
    """flags.reasons as a list of dicts ([{rule, detail, weight, priority}]; live flags add non-policy entries)."""
    r = fl.get("reasons")
    return [x for x in r if isinstance(x, dict)] if isinstance(r, list) else []


# ── load ──────────────────────────────────────────────────────────────────────────────────────────
def _rows(conn, sql: str, args=()) -> List[dict]:
    cur = conn.execute(sql, args)
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def load(conn) -> dict:
    d = {
        "locations": _rows(conn, "SELECT * FROM locations ORDER BY location_id"),
        "assets": _rows(conn, "SELECT * FROM assets ORDER BY asset_id"),
        "missions": _rows(conn, "SELECT * FROM missions ORDER BY mission_id"),
        "actions": _rows(conn, "SELECT * FROM actions ORDER BY action_id"),
        "readings": _rows(conn, "SELECT * FROM readings ORDER BY reading_id"),
        "anomalies": _rows(conn, "SELECT * FROM anomalies ORDER BY anomaly_id"),
        "flags": _rows(conn, "SELECT flag_id, triage_event_id, mission_id, action_id, asset_id, location_id, score, "
                             "severity, title, reasons, status, priority, category FROM flags ORDER BY flag_id"),
        # same filters as amd/embed/ingest.py build_operator_notes / build_explanations (audit M10)
        "notes": _rows(conn, "SELECT k.ack_id, k.flag_id, k.action, k.note, k.operator FROM acknowledgements k "
                             "JOIN flags f ON f.flag_id = k.flag_id WHERE k.note IS NOT NULL AND btrim(k.note) <> '' "
                             "ORDER BY k.ack_id"),
        "explanations": _rows(conn, "SELECT s.assessment_id, s.flag_id, s.answer, s.reasoning, s.recommended_action, "
                                    "s.model FROM assessments s JOIN flags f ON f.flag_id = s.flag_id "
                                    "WHERE s.state = 'DONE' AND coalesce(s.model, 'template') <> 'template' "
                                    "AND s.answer IS NOT NULL ORDER BY s.assessment_id"),
        "manual": _rows(conn, "SELECT doc_id, title, content, metadata FROM documents WHERE kind = 'manual' "
                              "ORDER BY doc_id"),
    }
    d["docs"] = {r[0]: r[1] for r in conn.execute("SELECT doc_id, embedding IS NOT NULL FROM documents")}
    return d


# ── build ─────────────────────────────────────────────────────────────────────────────────────────
class Graph:
    def __init__(self, docs: Dict[str, bool]):
        self.docs = docs
        self.nodes: Dict[str, dict] = {}
        self.edges: Dict[Tuple[str, str, str], dict] = {}

    def node(self, kind: str, key: str, label: str, summary: str, props: Optional[dict] = None,
             doc_id: Optional[str] = None) -> str:
        nid = "%s:%s" % (kind, key)
        self.nodes[nid] = {"node_id": nid, "kind": kind, "key": key, "label": label, "summary": summary,
                           "props": dict(props or {}), "doc_id": doc_id if doc_id in self.docs else None}
        return nid

    def doc(self, doc_id: str, fallback: Optional[str] = None) -> Optional[str]:
        return doc_id if doc_id in self.docs else fallback

    def edge(self, src: str, rel: str, dst: str, source: str, weight: float = 1.0,
             evidence: Optional[str] = None, **props) -> None:
        if src not in self.nodes or dst not in self.nodes or src == dst:
            return
        self.edges[(src, rel, dst)] = {"weight": float(weight), "evidence": evidence,
                                       "props": dict(props, source=source)}


def build(conn, d: dict) -> Graph:
    g = Graph(d["docs"])
    locs = {l["location_id"]: l for l in d["locations"]}
    assets = {a["asset_id"]: a for a in d["assets"]}
    actions = {a["action_id"]: a for a in d["actions"]}
    reading_of = {r["action_id"]: r for r in d["readings"]}
    anomaly_of = defaultdict(list)
    for an in d["anomalies"]:
        anomaly_of[an["action_id"]].append(an)
    by = defaultdict(lambda: defaultdict(list))          # by[field][value] -> actions
    for a in d["actions"]:
        env = (locs.get(a["location_id"]) or {}).get("environment")
        for f, v in (("location_id", a["location_id"]), ("asset_id", a["asset_id"]), ("mission_id", a["mission_id"]),
                     ("action_type", a["action_type"]), ("failure_type", a["failure_type"]), ("environment", env)):
            if v:
                by[f][v].append(a)
    anoms_by = defaultdict(lambda: defaultdict(list))
    for an in d["anomalies"]:
        for f in ("location_id", "asset_id", "mission_id", "anomaly_type"):
            if an[f]:
                anoms_by[f][an[f]].append(an)
    flags_by = defaultdict(lambda: defaultdict(list))
    for fl in d["flags"]:
        for f in ("location_id", "asset_id", "action_id", "mission_id"):
            if fl[f]:
                flags_by[f][fl[f]].append(fl)
    rule_fired = Counter(r.get("rule") for fl in d["flags"] for r in reasons(fl))
    overall = sum(a["status"] == "SUCCESS" for a in d["actions"]) / max(len(d["actions"]), 1)

    # environments, locations, assets
    for env in sorted({l["environment"] for l in d["locations"] if l["environment"]}):
        ls = [l for l in d["locations"] if l["environment"] == env]
        acts = by["environment"][env]
        g.node("environment", env, h(env).capitalize(),
               "%s environment (%s): %d location%s (%s); %s (all locations: %s)." % (
                   h(env).capitalize(), env, len(ls), "" if len(ls) == 1 else "s",
                   ", ".join("%s %s" % (l["location_id"], l["location_name"]) for l in ls), success_text(acts),
                   pct(overall)),
               {"locations": [l["location_id"] for l in ls], "actions": len(acts)})
    for l in d["locations"]:
        lid, acts = l["location_id"], by["location_id"][l["location_id"]]
        here = sorted(a["asset_id"] for a in d["assets"] if a["location_id"] == lid)
        g.node("location", lid, l["location_name"],
               "%s (%s): %s zone, floor %s, %s environment; assets %s; %s; %s." % (
                   l["location_name"], lid, h(l["zone_type"]), l["floor"], l["environment"],
                   ", ".join(here) or "none", success_text(acts), anomalies_text(anoms_by["location_id"][lid])),
               {"zone_type": l["zone_type"], "floor": l["floor"], "environment": l["environment"], "actions": len(acts),
                "success_rate": round(sum(a["status"] == "SUCCESS" for a in acts) / len(acts), 3) if acts else None})
    for a in d["assets"]:
        aid, acts = a["asset_id"], by["asset_id"][a["asset_id"]]
        loc = locs.get(a["location_id"]) or {}
        g.node("asset", aid, a["asset_name"],
               "%s (%s): %s, %s criticality, at %s (%s); %s; %s; %d flag%s." % (
                   a["asset_name"], aid, (a["asset_type"] or "asset").lower(), a["criticality"],
                   loc.get("location_name"), a["location_id"], success_text(acts),
                   anomalies_text(anoms_by["asset_id"][aid]), len(flags_by["asset_id"][aid]),
                   "" if len(flags_by["asset_id"][aid]) == 1 else "s"),
               {"asset_type": a["asset_type"], "criticality": a["criticality"], "location_id": a["location_id"]})

    # enums: action types, failure types, anomaly types
    for t in sorted(set(by["action_type"]) | set(RELATES_TO["action_type"])):
        acts = by["action_type"][t]
        env_rates = []
        for env, ea in sorted(by["environment"].items()):
            ea = [a for a in ea if a["action_type"] == t]
            if len(ea) >= MIN_ENV_N:
                env_rates.append((sum(a["status"] == "SUCCESS" for a in ea) / len(ea), env, len(ea)))
        worst = min(env_rates) if env_rates else None
        g.node("action_type", t, h(t).capitalize(),
               "%s actions (%s): %s%s." % (h(t).capitalize(), t, success_text(acts),
                                           "; lowest success in %s (%s of %d)" % (worst[1], pct(worst[0]), worst[2])
                                           if worst else ""),
               {"actions": len(acts)})
    for t in sorted(set(FAILURE_TYPES) | set(by["failure_type"])):
        acts = by["failure_type"][t]
        cat = P.failure_mode(t) if t in ALL_FAILURES else "unknown"
        n_flags = sum(len(flags_by["action_id"][a["action_id"]]) for a in acts)
        s = "%s (%s), a %s failure: %d failed action%s" % (h(t).capitalize(), t, cat.replace("_", " "), len(acts),
                                                           "" if len(acts) == 1 else "s")
        if acts:
            s += "; where: %s; action types: %s; flagged %d time%s" % (
                ranked(Counter(a["location_id"] for a in acts), 4,
                       lambda k: "%s %s" % (k, locs[k]["location_name"]) if k in locs else k),
                ranked(Counter(a["action_type"] for a in acts), 3, h), n_flags, "" if n_flags == 1 else "s")
        g.node("failure_type", t, h(t).capitalize(), s + ".", {"category": cat, "count": len(acts)})
    for t in sorted(set(ANOMALY_TYPES) | set(anoms_by["anomaly_type"])):
        ans = anoms_by["anomaly_type"][t]
        sev = Counter(a["severity"] for a in ans)
        cat = anomaly_category(t).replace("_", " ")
        s = "%s (%s), %s %s anomaly: %d anomal%s" % (h(t).capitalize(), t, "an" if cat[0] in "aeiou" else "a", cat,
                                                     len(ans), "y" if len(ans) == 1 else "ies")
        if ans:
            assets_hit = Counter(a["asset_id"] for a in ans)
            s += " (%s) on %d asset%s: %s" % (
                ", ".join("%d %s" % (sev[x], x) for x in ("CRITICAL", "HIGH", "MEDIUM", "LOW") if sev[x]),
                len(assets_hit), "" if len(assets_hit) == 1 else "s", ranked(assets_hit, 4, str))
        g.node("anomaly_type", t, h(t).capitalize(), s + ".", {"category": anomaly_category(t), "count": len(ans)})

    # missions, actions, anomalies, flags
    for m in d["missions"]:
        mid, acts = m["mission_id"], by["mission_id"][m["mission_id"]]
        g.node("mission", mid, m["mission_name"] or mid,
               "Mission %s %s (%s, config %s): reported %s; %s; %s; battery %s%% to %s%%, %s min." % (
                   mid, m["mission_name"], m["scenario"], m["configuration_version"], m["mission_status"],
                   success_text(acts), anomalies_text(anoms_by["mission_id"][mid]), m["start_battery_pct"],
                   m["end_battery_pct"], m["duration_min"]),
               {"scenario": m["scenario"], "configuration_version": m["configuration_version"],
                "mission_status": m["mission_status"]}, doc_id="event:mis-" + mid)
    for a in d["actions"]:
        aid = a["action_id"]
        asset, loc = assets.get(a["asset_id"]) or {}, locs.get(a["location_id"]) or {}
        r, ans = reading_of.get(aid), anomaly_of.get(aid, [])
        s = "Action %s (mission %s step %s, %s): %s on %s (%s) at %s (%s), battery %s%%: %s%s, %ss" % (
            aid, a["mission_id"], a["sequence"], a["timestamp"], h(a["action_type"]), asset.get("asset_name"),
            a["asset_id"], loc.get("location_name"), a["location_id"], a["battery_pct"], a["status"],
            " (%s)" % h(a["failure_type"]) if a["failure_type"] else "", a["duration_sec"])
        if r:
            s += "; reading %s %g %s (%s)" % (h(r["measurement_type"]), r["value"], r["unit"], r["quality"])
        for an in ans:
            s += "; anomaly %s %s (%s)" % (an["anomaly_id"], h(an["anomaly_type"]), an["severity"])
        g.node("action", aid, "%s: %s, %s at %s, %s" % (aid, h(a["action_type"]), a["asset_id"], a["location_id"],
                                                         a["status"]),
               s + ".", {"mission_id": a["mission_id"], "status": a["status"], "battery_pct": a["battery_pct"],
                         "failure_type": a["failure_type"]}, doc_id="event:act-" + aid)
    for an in d["anomalies"]:
        asset, loc = assets.get(an["asset_id"]) or {}, locs.get(an["location_id"]) or {}
        act = actions.get(an["action_id"]) or {}
        g.node("anomaly", an["anomaly_id"], "%s: %s (%s) on %s" % (an["anomaly_id"], h(an["anomaly_type"]),
                                                                   an["severity"], an["asset_id"]),
               "Anomaly %s: %s, %s, on %s (%s) at %s (%s) during action %s (mission %s, %s); \"%s\"; operator review "
               "required: %s." % (an["anomaly_id"], h(an["anomaly_type"]), an["severity"], asset.get("asset_name"),
                                  an["asset_id"], loc.get("location_name"), an["location_id"], an["action_id"],
                                  an["mission_id"], act.get("status"), clip(an["description"], 160),
                                  "yes" if an["operator_review_required"] else "no"),
               {"severity": an["severity"], "anomaly_type": an["anomaly_type"]}, doc_id="incident:" + an["anomaly_id"])
    for fl in d["flags"]:
        fid = fl["flag_id"]
        rules = [r["rule"] for r in reasons(fl) if r.get("rule") in PR.BY_NAME]     # policy rules only
        head = clip((fl["title"] or "").split(": ", 1)[0], 90)
        g.node("flag", str(fid), "Flag #%d: %s" % (fid, head),
               "Flag #%d %s %s (%s, %s), score %g: %s. Rules: %s." % (
                   fid, fl["priority"] or "?", fl["severity"], fl["category"] or "?", fl["status"], fl["score"],
                   clip(fl["title"], 200), ", ".join(rules) or "none"),
               {"priority": fl["priority"], "severity": fl["severity"], "category": fl["category"],
                "status": fl["status"], "triage_event_id": fl["triage_event_id"]}, doc_id="incident:flag-%d" % fid)

    # policy rules, topics, manual, notes, explanations
    for r in PR.ALL_RULES:
        first = sentence(r.text, 220)
        if r.priority:
            s = "Triage rule %s (%s events, %s, %s): %s. %s Fired on %d flag%s." % (
                r.name, r.scope, r.priority, "escalated" if r.priority in ("P1", "P2") else "not escalated", r.title,
                first, rule_fired[r.name], "" if rule_fired[r.name] == 1 else "s")
        else:
            s = "Triage policy reference %s: %s. %s" % (r.name, r.title, first)
        g.node("policy_rule", r.name, r.title, s, {"scope": r.scope, "priority": r.priority,
                                                    "policy_version": P.POLICY_VERSION}, doc_id="policy:" + r.name)
    for t, (label, desc, _) in TOPICS.items():
        g.node("topic", t, label, desc, {})          # summary completed after the edges exist (link counts)
    for m in d["manual"]:
        key = m["doc_id"].split(":", 1)[1]
        meta = m["metadata"] or {}
        page, printed = meta.get("page"), meta.get("printed_page")
        g.node("manual", key, m["title"] or m["doc_id"],
               "%s (Spot User Guide %s)" % (clip(m["content"], 200),
                                            "p.%s, PDF p.%s" % (printed, page) if printed else "PDF p.%s" % page),
               {"page": page, "printed_page": printed, "chunk": meta.get("chunk")}, doc_id=m["doc_id"])
    flag_title = {fl["flag_id"]: fl["title"] for fl in d["flags"]}
    for n in d["notes"]:
        g.node("operator_note", str(n["ack_id"]), "Operator note #%d on flag #%d" % (n["ack_id"], n["flag_id"]),
               "Operator %s (%s) on flag #%d (%s): \"%s\"" % (n["operator"], n["action"], n["flag_id"],
                                                             clip(flag_title.get(n["flag_id"]), 80), clip(n["note"], 200)),
               {"flag_id": n["flag_id"], "ack_action": n["action"]}, doc_id="note:%d" % n["ack_id"])
    for x in d["explanations"]:
        g.node("explanation", str(x["assessment_id"]), "System 2 explanation of flag #%d" % x["flag_id"],
               "System 2 (%s) on flag #%d: %s Recommended: %s" % (x["model"], x["flag_id"], clip(x["answer"], 200),
                                                                 clip(x["recommended_action"] or "-", 120)),
               {"flag_id": x["flag_id"], "model": x["model"]}, doc_id="explanation:%d" % x["assessment_id"])
    for n in g.nodes.values():
        n["props"]["built_by"] = BUILT_BY

    # ── fk edges ──
    for a in d["actions"]:
        an_id = "action:" + a["action_id"]
        ev = g.doc("event:act-" + a["action_id"], a["action_id"])
        g.edge(an_id, "part_of", "mission:%s" % a["mission_id"], "fk", evidence=ev)
        g.edge(an_id, "at", "location:%s" % a["location_id"], "fk", evidence=ev)
        g.edge(an_id, "inspects", "asset:%s" % a["asset_id"], "fk", evidence=ev)
        g.edge(an_id, "of_type", "action_type:%s" % a["action_type"], "fk", evidence=ev)
        if a["status"] == "FAILED" and a["failure_type"]:
            g.edge(an_id, "failed_with", "failure_type:" + a["failure_type"], "fk", evidence=ev,
                   battery_pct=a["battery_pct"])
    for a in d["assets"]:
        g.edge("asset:" + a["asset_id"], "located_at", "location:%s" % a["location_id"], "fk")
    for l in d["locations"]:
        if l["environment"]:
            g.edge("location:" + l["location_id"], "has_environment", "environment:" + l["environment"], "fk")
    for an in d["anomalies"]:
        nid, ev = "anomaly:" + an["anomaly_id"], g.doc("incident:" + an["anomaly_id"], an["anomaly_id"])
        g.edge(nid, "of_type", "anomaly_type:%s" % an["anomaly_type"], "fk", evidence=ev)
        g.edge(nid, "raised_on", "asset:%s" % an["asset_id"], "fk", evidence=ev)
        g.edge(nid, "during", "action:%s" % an["action_id"], "fk", evidence=ev)
    for fl in d["flags"]:
        nid = "flag:%d" % fl["flag_id"]
        ev = g.doc("incident:flag-%d" % fl["flag_id"], nid)
        g.edge(nid, "flags", "action:%s" % fl["action_id"] if fl["action_id"] else "mission:%s" % fl["mission_id"],
               "fk", evidence=ev)
        for r in reasons(fl):
            if r.get("rule") in PR.BY_NAME:      # policy rules only (not laya_verdict / rules_verdict)
                g.edge(nid, "triggered", "policy_rule:" + r["rule"], "fk", evidence=ev,
                       priority=r.get("priority"), detail=clip(r.get("detail") or "", 160))
    for n in d["notes"]:
        g.edge("operator_note:%d" % n["ack_id"], "annotates", "flag:%d" % n["flag_id"], "fk",
               evidence=g.doc("note:%d" % n["ack_id"], "ack:%d" % n["ack_id"]))
    for x in d["explanations"]:
        g.edge("explanation:%d" % x["assessment_id"], "explains", "flag:%d" % x["flag_id"], "fk",
               evidence=g.doc("explanation:%d" % x["assessment_id"], "assessment:%d" % x["assessment_id"]))
    # fk weight = specificity: 1/n, n = edges of that rel into the same dst (props.of = n). One of WP09's 22 actions
    # weighs 1/22, below the location's statistical facts; an action's own flag (usually 1 of 1) weighs 1.
    fk_in = Counter((r, t) for (_, r, t), e in g.edges.items() if e["props"]["source"] == "fk")
    for (_, r, t), e in g.edges.items():
        if e["props"]["source"] == "fk":
            e["weight"], e["props"]["of"] = round(1.0 / fk_in[(r, t)], 4), fk_in[(r, t)]

    # ── stat edges (computed in SQL) ──
    for ft, lid, n, of, rate, ex in conn.execute("""
        WITH per AS (SELECT location_id, count(*) AS total FROM actions GROUP BY 1)
        SELECT a.failure_type, a.location_id, count(*), per.total, round(count(*)::numeric / per.total, 4)::float8,
               (array_agg(a.action_id ORDER BY a.action_id))[1:5]
        FROM actions a JOIN per USING (location_id)
        WHERE a.status = 'FAILED' AND a.failure_type IS NOT NULL
        GROUP BY a.failure_type, a.location_id, per.total HAVING count(*) >= %s
        ORDER BY 1, 2""", (MIN_STAT_N,)):
        g.edge("failure_type:" + ft, "common_at", "location:" + lid, "stat", weight=rate,
               evidence=g.doc("event:act-" + ex[0], ex[0]), n=n, of=of, rate=rate, examples=ex)
    for at, aid, n, of, rate, ex in conn.execute("""
        WITH per AS (SELECT asset_id, count(*) AS total FROM actions GROUP BY 1)
        SELECT an.anomaly_type, an.asset_id, count(*), per.total, round(count(*)::numeric / per.total, 4)::float8,
               (array_agg(an.anomaly_id ORDER BY an.anomaly_id))[1:5]
        FROM anomalies an JOIN per USING (asset_id)
        GROUP BY an.anomaly_type, an.asset_id, per.total HAVING count(*) >= %s
        ORDER BY 1, 2""", (MIN_STAT_N,)):
        g.edge("anomaly_type:" + at, "common_at", "asset:" + aid, "stat", weight=rate,
               evidence=g.doc("incident:" + ex[0], ex[0]), n=n, of=of, rate=rate, examples=ex)
    for t, env, n, rate, base, failures, ex in conn.execute("""
        WITH x AS (SELECT a.action_id, a.action_type, l.environment, (a.status = 'SUCCESS')::int AS ok
                   FROM actions a JOIN locations l USING (location_id) WHERE l.environment IS NOT NULL),
        overall AS (SELECT action_type, avg(ok) AS rate FROM x GROUP BY 1),
        env AS (SELECT action_type, environment, count(*) AS n, avg(ok) AS rate, count(*) - sum(ok) AS failures,
                       (array_agg(action_id ORDER BY action_id) FILTER (WHERE ok = 0))[1:5] AS ex
                FROM x GROUP BY 1, 2)
        SELECT env.action_type, env.environment, env.n, round(env.rate, 4)::float8, round(overall.rate, 4)::float8,
               env.failures, env.ex
        FROM env JOIN overall USING (action_type)
        WHERE env.n >= %s AND env.rate < overall.rate
        ORDER BY 1, 2""", (MIN_ENV_N,)):
        g.edge("action_type:" + t, "fails_more_in", "environment:" + env, "stat", weight=round(base - rate, 4),
               evidence=g.doc("event:act-" + ex[0], ex[0]) if ex else None, n=n, rate=rate, overall=base,
               failures=failures, examples=ex or [])

    # ── curated edges ──
    for rule, targets in ABOUT.items():
        for t in targets:
            g.edge("policy_rule:" + rule, "about", t, "curated", evidence=g.doc("policy:" + rule))
    for kind, m in RELATES_TO.items():
        for k, topics in m.items():
            for t in topics:
                g.edge("%s:%s" % (kind, k), "relates_to", "topic:" + t, "curated")
    # curated weight = 1/n, n = that src's targets for the rel: a rule 'about' all 8 failure types (action_failed)
    # links each at 0.125, a rule about one thing at 1.0.
    cur_out = Counter((s, r) for (s, r, _), e in g.edges.items() if e["props"]["source"] == "curated")
    for (s, r, _), e in g.edges.items():
        if e["props"]["source"] == "curated":
            e["weight"], e["props"]["of"] = round(1.0 / cur_out[(s, r)], 4), cur_out[(s, r)]

    # ── extracted mentions ──
    ex = Extractor(d["assets"], d["locations"])
    fk_pairs = {frozenset((s, t)) for (s, _, t), e in g.edges.items() if e["props"]["source"] == "fk"}
    texts = [("manual:" + m["doc_id"].split(":", 1)[1], m["content"], m["doc_id"]) for m in d["manual"]]
    texts += [("anomaly:" + an["anomaly_id"], an["description"], g.doc("incident:" + an["anomaly_id"], an["anomaly_id"]))
              for an in d["anomalies"]]
    texts += [("operator_note:%d" % n["ack_id"], n["note"], g.doc("note:%d" % n["ack_id"], "ack:%d" % n["ack_id"]))
              for n in d["notes"]]
    texts += [("explanation:%d" % x["assessment_id"],
               " ".join(filter(None, [x["answer"], x["reasoning"], x["recommended_action"]])),
               g.doc("explanation:%d" % x["assessment_id"], "assessment:%d" % x["assessment_id"])) for x in d["explanations"]]
    for src, text, ev in texts:
        for dst, (n, terms) in sorted(ex(text).items()):
            if frozenset((src, dst)) in fk_pairs:     # already linked by a row (e.g. anomaly -of_type-> its type)
                continue
            g.edge(src, "mentions", dst, "extract", weight=n, evidence=ev, hits=n, terms=terms)

    # topic summaries: the curated description + what links to it
    for t, (label, desc, _) in TOPICS.items():
        nid = "topic:" + t
        linked = defaultdict(list)
        for (s, rel, dst), e in g.edges.items():
            if dst == nid:
                linked[s.split(":", 1)[0]].append(s.split(":", 1)[1])
        parts = []
        for kind, name in (("failure_type", "failure types"), ("anomaly_type", "anomaly types"),
                           ("action_type", "action types")):
            if linked[kind]:
                parts.append("%s %s" % (name, ", ".join(h(x) for x in sorted(set(linked[kind])))))
        if linked["manual"]:
            pages = sorted({g.nodes["manual:" + k]["props"].get("page") or 0 for k in linked["manual"]})
            # PDF page numbers (props.page), labelled as such: manual titles cite the printed page too
            parts.append("%d Spot User Guide passages (PDF pages %s)" % (
                len(linked["manual"]), ", ".join(map(str, pages[:8])) + (", ..." if len(pages) > 8 else "")))
        if linked["policy_rule"]:
            parts.append("triage rules %s" % ", ".join(sorted(set(linked["policy_rule"]))))
        g.nodes[nid]["summary"] = "%s: %s%s" % (label, desc, (" Linked: " + "; ".join(parts) + ".") if parts else "")
        g.nodes[nid]["props"].update({"manual_passages": len(linked["manual"])})
    return g


# ── write ─────────────────────────────────────────────────────────────────────────────────────────
NODE_COLS = ["node_id", "kind", "key", "label", "summary", "props", "doc_id"]


def write(conn, g: Graph, vectors: Dict[str, Tuple[str, str, str]], counts: dict) -> None:
    """One transaction: upsert nodes, delete stale built nodes, rewrite build edges, set embeddings.
    vectors: node_id -> (pgvector literal, model, summary hash) for summaries embedded in this run."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO kg_nodes (node_id, kind, key, label, summary, props, doc_id) "
                "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s) ON CONFLICT (node_id) DO UPDATE SET "
                "kind = EXCLUDED.kind, key = EXCLUDED.key, label = EXCLUDED.label, summary = EXCLUDED.summary, "
                "props = EXCLUDED.props, doc_id = EXCLUDED.doc_id, updated_at = now() "
                "WHERE (kg_nodes.kind, kg_nodes.key, kg_nodes.label, kg_nodes.summary, kg_nodes.props, kg_nodes.doc_id) "
                "IS DISTINCT FROM (EXCLUDED.kind, EXCLUDED.key, EXCLUDED.label, EXCLUDED.summary, EXCLUDED.props, "
                "EXCLUDED.doc_id) AND coalesce(kg_nodes.props->>'source', '') <> 'live'",   # live nodes stay as written
                [tuple(json.dumps(n[c], sort_keys=True, default=str) if c == "props" else n[c] for c in NODE_COLS)
                 for n in g.nodes.values()])
            counts["nodes_written"] = max(cur.rowcount, 0)
        counts["nodes_deleted"] = conn.execute(
            "DELETE FROM kg_nodes WHERE props->>'built_by' = %s AND NOT (node_id = ANY (%s))",
            (BUILT_BY, list(g.nodes))).rowcount
        conn.execute("DELETE FROM kg_edges WHERE props->>'source' = ANY (%s)", (BUILD_SOURCES,))
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO kg_edges (src, rel, dst, weight, props, evidence) VALUES (%s, %s, %s, %s, %s::jsonb, %s) "
                "ON CONFLICT (src, rel, dst) DO UPDATE SET weight = EXCLUDED.weight, props = EXCLUDED.props, "
                "evidence = EXCLUDED.evidence WHERE coalesce(kg_edges.props->>'source', '') <> 'live'",
                [(s, r, t, e["weight"], json.dumps(e["props"], sort_keys=True, default=str), e["evidence"])
                 for (s, r, t), e in sorted(g.edges.items())])
        if vectors:
            with conn.cursor() as cur:
                cur.executemany(
                    "UPDATE kg_nodes SET embedding = %s::vector, embed_model = %s, "
                    "props = props || jsonb_build_object('embedded_hash', %s::text) WHERE node_id = %s "
                    "AND coalesce(props->>'source', '') <> 'live'",
                    [(v, model, hsh, nid) for nid, (v, model, hsh) in sorted(vectors.items())])
        # document-backed nodes reuse documents.embedding (and lose a stale summary vector if the doc has none)
        counts["embed_from_documents"] = conn.execute("""
            UPDATE kg_nodes n SET embedding = d.embedding, embed_model = d.embed_model,
                   props = n.props - 'embedded_hash'
            FROM documents d
            WHERE d.doc_id = n.doc_id AND d.embedding IS NOT NULL
              AND (n.embedding IS DISTINCT FROM d.embedding OR n.embed_model IS DISTINCT FROM d.embed_model
                   OR n.props ? 'embedded_hash')""").rowcount


def embed_todo(conn, g: Graph, model: str, force: bool) -> List[str]:
    """Summary-embedded nodes whose summary or model changed since their vector was made."""
    rows = conn.execute("SELECT node_id, props->>'embedded_hash', embed_model, embedding IS NOT NULL, "
                        "coalesce(props->>'source', '') = 'live' FROM kg_nodes").fetchall()
    have = {r[0]: r[1:4] for r in rows}
    live = {r[0] for r in rows if r[4]}
    todo = []
    for nid, n in g.nodes.items():
        if n["doc_id"] and g.docs.get(n["doc_id"]):       # document-backed with a document vector
            continue
        if nid in live:                                   # the live simulation embeds its own nodes
            continue
        old = have.get(nid)
        if force or old is None or old != (md5(n["summary"]), model, True):
            todo.append(nid)
        elif old[0]:
            n["props"]["embedded_hash"] = old[0]          # keep the marker on the upsert
    return sorted(todo)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-embed", action="store_true", help="don't call the embedding service")
    ap.add_argument("--force-embed", action="store_true", help="re-embed every summary-embedded node")
    ap.add_argument("--dry-run", action="store_true", help="build in memory and print counts; write nothing")
    args = ap.parse_args()

    conn = spotdb.connect(autocommit=True)
    if not spotdb.has_table(conn, "public.kg_nodes"):
        sys.exit("kg_nodes is missing: run `python db/setup.py migrate` first (009_graph.sql)")
    g = build(conn, load(conn))
    node_counts = dict(sorted(Counter(n["kind"] for n in g.nodes.values()).items()))
    edge_counts = dict(sorted(Counter(r for (_, r, _) in g.edges).items()))
    source_counts = dict(sorted(Counter(e["props"]["source"] for e in g.edges.values()).items()))
    print("nodes %d  %s" % (len(g.nodes), json.dumps(node_counts)))
    print("edges %d  %s" % (len(g.edges), json.dumps(edge_counts)))
    print("edges by source %s" % json.dumps(source_counts))
    if args.dry_run:
        return

    from client import EmbedClient, to_pgvector
    client = EmbedClient()
    meta = {"embed_model": client.model, "embed_url": spotdb.redact(client.url), "no_embed": args.no_embed,
            "force_embed": args.force_embed}
    with spotdb.job_run("graph", meta) as run:
        run.counts.update({"nodes": node_counts, "edges": edge_counts, "edges_by_source": source_counts,
                           "nodes_total": len(g.nodes), "edges_total": len(g.edges)})
        ready = False
        if not args.no_embed and (not client.model or embed_todo(conn, g, client.model, args.force_embed)):
            ready = _ready(client, run)       # may adopt the server's model when EMBED_MODEL is set but empty
        todo = embed_todo(conn, g, client.model, args.force_embed)
        vectors: Dict[str, Tuple[str, str, str]] = {}
        if todo and ready:
            vectors = _embed(client, g, todo, run, to_pgvector)
        run.counts["embedded"] = len(vectors)
        run.counts["embed_pending"] = len(todo) - len(vectors)
        write(conn, g, vectors, run.counts)
        run.counts["embedded_nodes"] = conn.execute("SELECT count(embedding) FROM kg_nodes").fetchone()[0]
        print("wrote %d changed nodes (%d stale deleted), %d edges; embedded %d summaries (%d pending), "
              "%d nodes refreshed from documents; %d of %d nodes have a vector" % (
                  run.counts["nodes_written"], run.counts["nodes_deleted"], len(g.edges), len(vectors),
                  run.counts["embed_pending"], run.counts["embed_from_documents"], run.counts["embedded_nodes"],
                  len(g.nodes)))
    conn.close()


def _ready(client, run) -> bool:
    """Wait for the embedding service (GRAPH_EMBED_WAIT_S) and check its model and dimension. False (graph still
    written, summaries pending) if it is down; SystemExit before any write if it runs another model or dimension."""
    try:
        health = client.wait_ready(timeout_s=float(os.environ.get("GRAPH_EMBED_WAIT_S", "120")))
    except Exception as e:  # noqa: BLE001 - the graph is still useful without new vectors
        run.counts["embed_error"] = repr(e)[:300]
        print("warning: embedding service not ready (%s); node summaries stay pending for the next run" % e,
              file=sys.stderr)
        return False
    run.metadata["embed_health"] = {k: health.get(k) for k in ("model", "dim", "device", "dtype", "gpu")}
    client.check_health(health)
    run.metadata["embed_model"] = client.model
    return True


def _embed(client, g: Graph, todo: List[str], run, to_pgvector) -> Dict[str, Tuple[str, str, str]]:
    """Embed node summaries batch by batch (client.py retries each batch). Never fails the build: the vectors of the
    batches that succeeded are kept; after a batch fails for good, it and the rest stay pending for the next run."""
    logs = None
    try:
        logs = spotdb.connect(spotdb.logs_database_url(), autocommit=True)
        if not spotdb.has_table(logs, "app.model_calls"):
            logs.close()
            logs = None
    except Exception as e:  # noqa: BLE001 - logging must never break the build
        print("warning: no model-call logging: %s" % e, file=sys.stderr)

    def on_call(c: dict) -> None:
        if logs is None:
            return
        tps = (c["input_tokens"] / (c["latency_ms"] / 1000)) if c.get("input_tokens") and c["latency_ms"] else None
        try:
            spotdb.log_model_call(logs, request_id="graph-%s" % run.run_id, system="embed", model=c["model"],
                                  endpoint=c["endpoint"], input_tokens=c.get("input_tokens"), output_tokens=0,
                                  latency_ms=c["latency_ms"], tokens_per_sec=tps, device=c.get("gpu") or c.get("device"),
                                  ok=True, metadata={"batch": c["n"], "kind": c["kind"], "server_ms": c.get("server_ms"),
                                                     "job": "graph"})
        except Exception as e:  # noqa: BLE001
            print("warning: could not log embedding call: %s" % e, file=sys.stderr)

    client.on_call = on_call
    out: Dict[str, Tuple[str, str, str]] = {}
    print("embedding %d node summaries with %s" % (len(todo), client.model))
    try:
        step = max(1, client.batch_size)
        for i in range(0, len(todo), step):
            ids = todo[i:i + step]
            try:
                vecs = client.embed([g.nodes[nid]["summary"] for nid in ids], kind="doc")
            except Exception as e:  # noqa: BLE001 - keep what we have; the rest stays pending
                run.counts["embed_error"] = repr(e)[:300]
                print("warning: embedding failed (%s); %d node summaries stay pending for the next run"
                      % (e, len(todo) - len(out)), file=sys.stderr)
                break
            for nid, v in zip(ids, vecs):
                hsh = md5(g.nodes[nid]["summary"])
                g.nodes[nid]["props"]["embedded_hash"] = hsh
                out[nid] = (to_pgvector(v), client.model, hsh)
    finally:
        if logs is not None:
            logs.close()
    return out


if __name__ == "__main__":
    main()
