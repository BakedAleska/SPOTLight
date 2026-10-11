"""Graph RAG helpers (contract §7): readable triples, ranking, de-duplication and a token-bounded pack.

Pure functions over the rows the graph functions return, so they are unit-testable without a database.

graph_neighborhood gives the node set (one row per reached node, with the edge it was reached by). The facts are the
stored edges among that set (repo.kg_details), which also catches edges between two seeds or two same-hop nodes and
carries each edge's direction and props; if that lookup fails, the traversal's own edges are used, oriented from the
relation table below. Each fact is cited by one endpoint's id (its doc_id for manual / operator_note / explanation
nodes, the same ids the RAG sections use), plus the edge's evidence id when there is one.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

NODE_KINDS = ("mission", "action", "asset", "location", "environment", "action_type", "failure_type",
              "anomaly_type", "anomaly", "flag", "policy_rule", "topic", "manual", "operator_note", "explanation")
ENUM_KINDS = {"environment", "action_type", "failure_type", "anomaly_type", "policy_rule", "topic"}
DOC_KINDS = {"manual", "operator_note", "explanation"}
INSTANCE_KINDS = {"action", "mission", "anomaly", "flag"}
STAT_RELS = {"common_at", "fails_more_in"}
FK_RELS = {"part_of", "at", "inspects", "of_type", "failed_with", "located_at", "has_environment", "raised_on",
           "during", "flags", "triggered", "annotates", "explains"}
# rel -> (src kinds, dst kinds), from the contract's relation table.
REL_KINDS: Dict[str, Tuple[Set[str], Set[str]]] = {
    "part_of": ({"action"}, {"mission"}),
    "at": ({"action"}, {"location"}),
    "inspects": ({"action"}, {"asset"}),
    "of_type": ({"action", "anomaly"}, {"action_type", "anomaly_type"}),
    "failed_with": ({"action"}, {"failure_type"}),
    "located_at": ({"asset"}, {"location"}),
    "has_environment": ({"location"}, {"environment"}),
    "raised_on": ({"anomaly"}, {"asset"}),
    "during": ({"anomaly"}, {"action"}),
    "flags": ({"flag"}, {"action", "mission"}),
    "triggered": ({"flag"}, {"policy_rule"}),
    "common_at": ({"failure_type", "anomaly_type"}, {"location", "asset"}),
    "fails_more_in": ({"action_type"}, {"environment"}),
    "about": ({"policy_rule"}, {"failure_type", "anomaly_type", "topic"}),
    "relates_to": ({"failure_type", "anomaly_type", "action_type"}, {"topic"}),
    "mentions": ({"manual", "operator_note", "explanation", "anomaly"},
                 {"asset", "location", "failure_type", "anomaly_type", "action_type", "topic"}),
    # also built by db/graph_build.py (fk), not in the contract's table yet
    "annotates": ({"operator_note"}, {"flag"}),
    "explains": ({"explanation"}, {"flag"}),
}
# Node kinds a walk returns (graph_neighborhood's kind_filter filters returned rows; the walk itself passes through
# any kind, and LIMIT applies after the filter). Two walks run side by side so neither crowds the other out of the
# node cap: the reusable structure (where statistics live) and the documents linked to it. Individual actions,
# anomalies, flags and missions are left out of the packet: a waypoint has dozens of actions.
STRUCT_KINDS = ["asset", "location", "environment", "action_type", "failure_type", "anomaly_type", "policy_rule",
                "topic"]
DOC_LINK_KINDS = ["manual", "operator_note"]
# Ranking tiers, best first: 0 statistics, 1 manual links, 2 topic links, 3 operator notes / explanations,
# 4 other structure, 5 instances. Caps per (tier, on_point) keep a section varied: the packet takes only these
# (a waypoint's failure type is also common at five other waypoints; one or two of those is plenty), the tools
# take these first and then fill up to k in rank order.
TIER_CAPS = {(0, True): 4, (0, False): 2, (1, True): 3, (1, False): 1, (2, True): 2, (2, False): 1,
             (3, True): 2, (3, False): 1, (4, True): 2, (4, False): 1, (5, True): 1, (5, False): 1}
_MANUAL_HEADER = re.compile(r"^\s*\d+\s+R\s+\d+(?:\.\d+)*\s+V\s+A\s+")   # Spot User Guide page header in the text
LABEL_CHARS = 40
MANUAL_TEXT_CHARS = 100

_NODE_ID = re.compile(r"^(%s):(.+)$" % "|".join(NODE_KINDS))
_BARE_IDS = ((re.compile(r"^AN\d+$"), "anomaly:"), (re.compile(r"^A\d{3,}$"), "action:"),
             (re.compile(r"^M\d{3,}$"), "mission:"), (re.compile(r"^WP\d+$"), "location:"))


def split_node(node_id: Optional[str]) -> Tuple[Optional[str], str]:
    """'asset:HX-204' -> ('asset', 'HX-204'); anything else -> (None, text)."""
    m = _NODE_ID.match(node_id or "")
    return (m.group(1), m.group(2)) if m else (None, node_id or "")


def kind_of(node_id: str) -> Optional[str]:
    return split_node(node_id)[0]


def _clip(text: Optional[str], n: int) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def node_text(node_id: str, label: Optional[str] = None) -> str:
    """Vocabulary nodes render as their key (HIGH_NOISE); entities as 'label (key)' (Turbine Hall South (WP09))."""
    kind, key = split_node(node_id)
    label = " ".join((label or "").split())
    if kind in ENUM_KINDS or not label or label == key:
        return key
    return "%s (%s)" % (_clip(label, LABEL_CHARS), key)


CITE_BY_DOC = DOC_KINDS | {"policy_rule"}


def cite_id(node_id: str, kind: Optional[str], doc_id: Optional[str]) -> str:
    """Manual pages, notes, explanations and policy rules are cited by their doc_id (manual:p42-3, note:5,
    policy:battery_low: the ids the RAG sections use); everything else by node_id (asset:HX-204, action:A0136:
    the packet's row ids)."""
    return doc_id if (kind in CITE_BY_DOC and doc_id) else node_id


def norm_evidence(ev: Any) -> Optional[str]:
    """Evidence ids in citation form: 'AN002' -> 'anomaly:AN002', 'A0136' -> 'action:A0136'; doc ids unchanged."""
    if ev in (None, ""):
        return None
    ev = str(ev).strip()
    if ":" in ev:
        return ev
    for pat, prefix in _BARE_IDS:
        if pat.match(ev):
            return prefix + ev
    return ev


def _fmt(v: float) -> str:
    return ("%.1f" % v).rstrip("0").rstrip(".")


def _pct(x: Any) -> Optional[str]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return _fmt(v * 100) + "%" if 0.0 <= v <= 1.0 else _fmt(v)


def stat_suffix(rel: str, weight: Optional[float], props: Optional[Dict[str, Any]]) -> str:
    """' (n=6 of 22, rate 27%)' for common_at; ' (n=20, success 45% vs 71% overall)' for fails_more_in.
    (db/graph_build.py: common_at props n/of/rate, weight = rate; fails_more_in props n/rate/overall,
    weight = overall - rate.)"""
    props = props or {}
    parts = []
    if props.get("n") is not None:
        parts.append("n=%s of %s" % (props["n"], props["of"]) if props.get("of") is not None else "n=%s" % props["n"])
    rate = props.get("rate", weight if rel == "common_at" else None)
    overall = next((props[k] for k in ("overall", "overall_rate", "base_rate") if _pct(props.get(k))), None)
    if rel == "fails_more_in":
        if _pct(rate):
            parts.append("success %s" % _pct(rate) + (" vs %s overall" % _pct(overall) if overall is not None else ""))
    elif _pct(rate):
        parts.append("rate %s" % _pct(rate))
    return " (%s)" % ", ".join(parts) if parts else ""


def orient(a: str, rel: str, b: str) -> Tuple[str, str]:
    """(src, dst) for an edge seen as a -rel- b, from the relation table (no relation joins a kind to itself)."""
    src_k, dst_k = REL_KINDS.get(rel, (set(), set()))
    ka, kb = kind_of(a), kind_of(b)
    if kb in src_k and ka in dst_k and not (ka in src_k and kb in dst_k):
        return b, a
    return a, b


def candidates(rows: Sequence[Dict[str, Any]], seeds: Sequence[str],
               details: Optional[Dict[str, List[Dict[str, Any]]]] = None,
               context: Optional[Set[str]] = None,
               extra: Sequence[str] = ()) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Candidate facts over the neighbourhood's node set (seeds, `extra` nodes known to be one hop away, and the
    reached nodes).

    With `details` (repo.kg_details): every stored edge among those nodes, in its true direction with props.
    Without: only the edge each row was reached by (the traversal tree), oriented from the relation table.
    Each candidate names the endpoint it cites (`node`): a document-backed end first, else the end that is new to
    `context`, else the farther one from the seeds. Returns (candidates, node info by node_id)."""
    context = context or set()
    hop: Dict[str, int] = {s: 0 for s in seeds}
    for x in extra:
        hop.setdefault(x, 1)
    nodes: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        nid = r.get("node_id")
        if not nid:
            continue
        h = int(r.get("hop") or 1)
        hop[nid] = min(hop.get(nid, h), h)
        nodes.setdefault(nid, r)
    for n in (details or {}).get("nodes") or []:
        if n.get("node_id"):
            nodes[n["node_id"]] = {**nodes.get(n["node_id"], {}), **n}
    if details is not None:
        edges = list(details.get("edges") or [])
    else:
        edges = []
        for r in rows:
            via, rel, nid = r.get("via_node"), r.get("via_rel"), r.get("node_id")
            if via and rel and nid:  # a seed row (hop 0) is not an edge
                src, dst = orient(via, rel, nid)
                # a row's weight is its PATH strength (product of edge weights); only at hop 1 is it the edge's own
                edges.append({"src": src, "rel": rel, "dst": dst, "evidence": r.get("evidence"),
                              "weight": r.get("weight") if int(r.get("hop") or 1) == 1 else None})
    out = []
    for e in edges:
        src, rel, dst = e.get("src"), e.get("rel"), e.get("dst")
        if src not in hop or dst not in hop or not rel:
            continue
        ks, kd = kind_of(src), kind_of(dst)
        if kd in DOC_KINDS and ks not in DOC_KINDS:
            node = dst
        elif ks in DOC_KINDS and kd not in DOC_KINDS:
            node = src
        elif (src in context) != (dst in context):
            node = dst if src in context else src
        elif hop[src] != hop[dst]:
            node = src if hop[src] > hop[dst] else dst
        else:
            node = src if rel in STAT_RELS else dst
        via = dst if node == src else src
        props = e.get("props") or {}
        if isinstance(props, str):
            try:
                props = json.loads(props)
            except ValueError:
                props = {}
        info = nodes.get(node) or {}
        w = e.get("weight")
        out.append({"src": src, "rel": rel, "dst": dst, "node": node, "via": via,
                    "kind": info.get("kind") or kind_of(node), "doc_id": info.get("doc_id"),
                    "summary": info.get("summary"), "hop": max(hop[src], hop[dst]),
                    "weight": float(w) if w is not None else 0.0, "edge_weight": w, "props": props,
                    "evidence": e.get("evidence")})
    return out, nodes


def tier(c: Dict[str, Any]) -> int:
    kinds = {kind_of(c["src"]), kind_of(c["dst"])}
    if c["rel"] in STAT_RELS:
        return 0
    if "manual" in kinds:
        return 1
    if "topic" in kinds:
        return 2
    if kinds & {"operator_note", "explanation"}:
        return 3
    if kinds & INSTANCE_KINDS:
        return 5
    return 4


MERGE_MAX = 4


def rank(cands: Iterable[Dict[str, Any]], context: Set[str],
         drop_fk_within: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
    """De-duplicate by (src, rel, dst); drop FK facts between nodes already in `drop_fk_within` (the packet's
    flag/event sections state them); order by tier, where an off-point fact (one that doesn't touch this flag's own
    nodes) drops two tiers, then hop, then weight descending: on-point stats, on-point manual links, off-point stats
    and on-point topics, ... Finally merge non-statistical facts that share an endpoint and relation into one
    ('Autowalk gauge reading —mentions→ GAUGE_READING, gauge'), keeping the best one's place."""
    seen: Set[Tuple[str, str, str]] = set()
    out = []
    for c in cands:
        t = (c["src"], c["rel"], c["dst"])
        if t in seen:
            continue
        seen.add(t)
        if drop_fk_within and c["rel"] in FK_RELS and c["src"] in drop_fk_within and c["dst"] in drop_fk_within:
            continue
        c = dict(c, tier=tier(c))
        c["on_point"] = c["via"] in context and (c["node"] in context or kind_of(c["node"]) in DOC_KINDS | {"topic"})
        out.append(c)
    out.sort(key=lambda c: (c["tier"] + (0 if c["on_point"] else 2), not c["on_point"], c["hop"], -c["weight"],
                            c["src"], c["dst"]))
    # Merge: one fact per (shared end, rel, direction). Usually the shared end is the cited node (a manual page that
    # mentions several things); for instances it's the other end (many actions -> one asset), cited by the first.
    merged: List[Dict[str, Any]] = []
    groups: Dict[Tuple[str, str, bool], Dict[str, Any]] = {}
    for c in out:
        if c["rel"] in STAT_RELS:
            merged.append(dict(c, one=c["node"], many=[c["via"]]))
            continue
        one, other = c["node"], c["via"]
        if c["tier"] == 5:  # group instances on their non-instance end: 'A0136, A1000, … —inspects→ HX-204'
            if kind_of(c["node"]) in INSTANCE_KINDS and kind_of(c["via"]) not in INSTANCE_KINDS:
                one, other = c["via"], c["node"]
        key = (one, c["rel"], one == c["src"])
        g = groups.get(key)
        if g is None:
            groups[key] = g = dict(c, one=one, many=[other])
            merged.append(g)
        elif len(g["many"]) < MERGE_MAX:
            g["many"].append(other)
        else:
            g["truncated"] = True
    return merged


def _end_text(node_id: str, nodes: Dict[str, Dict[str, Any]]) -> str:
    label = (nodes.get(node_id) or {}).get("label")
    if kind_of(node_id) in DOC_KINDS and label:   # its doc id is the fact's `id`
        return _clip(label, LABEL_CHARS)
    kind, key = split_node(node_id)
    if kind == "flag":
        return "flag " + key
    if kind in INSTANCE_KINDS:                    # 'A0136', not 'GAUGE_READING on HX-204 (A0136)'
        return key
    return node_text(node_id, label)


def render(c: Dict[str, Any], nodes: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """{'id', 'fact': 'Turbine Hall South (WP09) —has_environment→ HIGH_NOISE', 'text'?, 'ev'?}."""
    one_id = c.get("one", c["node"])
    many = c.get("many") or [c["via"]]
    one = _end_text(one_id, nodes)
    many_txt = ", ".join(_end_text(o, nodes) for o in many) + (", …" if c.get("truncated") else "")
    src, dst = (one, many_txt) if one_id == c["src"] else (many_txt, one)
    fact = "%s —%s→ %s" % (src, c["rel"], dst)
    if c["rel"] in STAT_RELS:
        fact += stat_suffix(c["rel"], c.get("edge_weight", c.get("weight")), c.get("props"))
    item: Dict[str, Any] = {"id": cite_id(c["node"], c.get("kind"), c.get("doc_id")), "fact": fact}
    if c.get("kind") == "manual" and c.get("summary"):
        item["text"] = _clip(_MANUAL_HEADER.sub("", c["summary"]), MANUAL_TEXT_CHARS)
    ev = norm_evidence(c.get("evidence"))
    if ev and ev not in (item["id"], c["src"], c["dst"]):
        item["ev"] = ev
    return item


def pack(ranked: Sequence[Dict[str, Any]], nodes: Dict[str, Dict[str, Any]], measure: Callable[[Any], int],
         token_budget: Optional[int] = None, max_items: Optional[int] = None,
         caps: Optional[Dict[Tuple[int, bool], int]] = None, fill: bool = True) -> List[Dict[str, Any]]:
    """Render facts in rank order under a token and/or item bound. With caps, a first pass takes at most
    caps[(tier, on_point)] of each kind; with fill, a second pass then fills what's left in rank order.

    Every returned item has a distinct `id`: a lower-ranked fact that cites a node already cited (a second stat edge
    of one failure_type, a second link of one manual page or topic) is left out. System 2 rejects a packet that
    repeats an id with different content (422 'conflicting content for duplicate id')."""
    picked: Dict[int, Dict[str, Any]] = {}
    cited: Set[str] = set()
    used: Dict[Tuple[int, bool], int] = {}
    for capped in ((True, False) if (caps and fill) else (True,) if caps else (False,)):
        for i, c in enumerate(ranked):
            if i in picked:
                continue
            if max_items is not None and len(picked) >= max_items:
                break
            key = (c["tier"], bool(c.get("on_point")))
            if capped and used.get(key, 0) >= caps.get(key, 0):
                continue
            item = render(c, nodes)
            if item["id"] in cited:
                continue
            if token_budget is not None and measure(list(picked.values()) + [item]) > token_budget:
                continue  # a shorter fact further down may still fit
            picked[i] = item
            cited.add(item["id"])
            used[key] = used.get(key, 0) + 1
    return [picked[i] for i in sorted(picked)]
