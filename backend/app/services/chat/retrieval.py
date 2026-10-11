"""Backend-side retrieval for one chat turn (docs/chat-contract.md §2). Runs BEFORE the single Gemini call.

1. Query = the current message; with an earlier user turn, a second, smaller pass on "previous + current" so a
   follow-up ("what about the second one?") still finds its subject (full text only: no second embedding).
2. Embed with the existing EmbedClient (SEARCH_EMBED_TIMEOUT_S, capped to 60 % of the budget). Down or slow ->
   vec=None and everything runs on full text and the graph's text match (`mode: fulltext`).
3. Entities: regex ids in the question, the previous user turn and (`chat.2`) the previous answer (at most 5 ids
   from it, its cited sources' refs counting too); graph entry nodes of kind mission/asset/location/flag add more.
4. In parallel: search_documents (k 8), match_graph_nodes (k 6), fleet_snapshot (cached 60 s), entity_digests.
   With a vector, documents and vector-only graph nodes below cosine 0.50 are dropped (a document is still kept when
   it contains every query term, or every id in the question), so an off-topic question gets no documents and no
   vector-matched graph nodes. The snapshot still comes along, and the full-text passes (the follow-up pass, and
   everything when the embedder is down) have no floor.
   Then graph_neighborhood from the top 4 entry nodes (2 hops, 25 nodes; instance kinds filtered for hub seeds).
5. Sources with stable citation ids, merged by id; total context <= MAX_CONTEXT_CHARS, lowest-ranked dropped
   first, the snapshot always kept.
6. (`chat.2`) Carry-over: the sources the previous answer cited (<= 6, stored title + snippet) join after the fresh
   ones and are dropped first; an id retrieved again keeps its fresh text.
7. (`chat.3`, §9) Mission chats: the mission's digest is pinned first and never dropped for the budget; a
   mission-filtered document search (k 6) runs alongside the global one and ranks ahead of it (merged by id);
   `mission:<id>` is always a graph seed. A conversation with no mission retrieves exactly as before.
Everything is bounded by CHAT_RETRIEVAL_BUDGET_S: a step that has not finished is skipped. Every degraded step adds a
short string to `gaps` (sent in the `sources` event) and logs a WARNING. Only when documents, graph AND snapshot all
fail is the turn a `retrieval_failed` error.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..embed import vec_literal
from ..http import ServiceUnavailable, short_error
from .prompt import render_source, sources_block

log = logging.getLogger("backend.chat.retrieval")

MAX_CONTEXT_CHARS = 12_000
DOC_CHARS, DIGEST_CHARS, NODE_CHARS, STAT_CHARS = 1_200, 2_000, 450, 1_200
SNIPPET_CHARS = 280
SNAPSHOT_TTL_S = 60.0
SEED_COUNT = 4
MISSION_DOCS_K = 6       # chat.3: the mission-filtered document search
PRIOR_ENTITY_CAP = 5     # ids taken from the previous answer (its text, then its cited sources' refs)
CARRY_MAX = 6            # sources the previous answer cited, carried into this turn
# match_graph_nodes returns raw cosine similarity for vector-only hits, with no threshold: below this they are
# noise (text-match scores are >= 0.3 by construction and always kept). Measured with Qwen3-Embedding-0.6B and the
# query instruction (vector-store audit VS-06, 2026-10-11): off-topic questions score 0.40-0.54, relevant hits
# 0.54-0.83, so the floor sits above most of the noise. Documents get the same floor in ChatRepo.search_documents
# (db/chat.py CHAT_MIN_DOC_SIMILARITY).
MIN_VECTOR_SCORE = 0.50

MISSION_RE = re.compile(r"\bM\d{3}\b", re.I)
ASSET_RE = re.compile(r"\b[A-Z]{2,6}-\d{3}\b", re.I)
WAYPOINT_RE = re.compile(r"\bWP\d{2}\b", re.I)
FLAG_RES = (re.compile(r"flag\s*[#:]?\s*(\d+)", re.I), re.compile(r"#(\d+)"))

KG_KINDS = ("mission", "action", "asset", "location", "environment", "action_type", "failure_type", "anomaly_type",
            "anomaly", "flag", "policy_rule", "topic", "manual", "operator_note", "explanation")
INSTANCE_KINDS = ("mission", "action", "anomaly", "flag")
HUB_KINDS = {"location", "environment", "action_type", "failure_type", "anomaly_type", "topic", "policy_rule"}
NON_INSTANCE_KINDS = [k for k in KG_KINDS if k not in INSTANCE_KINDS]
ENTITY_KINDS = {"mission": "missions", "asset": "assets", "location": "locations", "flag": "flags"}
ORIGIN_RANK = {"sql": 0, "rag": 1, "graph": 2}
# Short, operator-readable names for the retrieval steps, used in `gaps`.
STEP_LABELS = {"snapshot": "fleet stats", "digests": "entity digests", "docs": "document search",
               "graph": "graph match", "docs2": "follow-up document search", "graph2": "follow-up graph match",
               "nb": "graph neighbourhood", "nb_hub": "graph neighbourhood (hubs)",
               "digests2": "entity digests (graph)", "mission": "mission digest",
               "mission_docs": "mission document search"}


class RetrievalFailed(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


@dataclass
class Retrieval:
    sources: List[Dict[str, Any]]          # public Source objects (contract §3), numbered n = 1..
    texts: List[str]                       # the exact text sent to the model for each source (same order)
    block: str                             # the <sources> block
    mode: str                              # hybrid | fulltext
    graph_seeds: List[str]
    latency_ms: int
    counts: Dict[str, int]
    gaps: List[str] = field(default_factory=list)
    mission_id: Optional[str] = None       # chat.3: the conversation's mission (None for an ordinary chat)
    mission: Optional[Dict[str, Any]] = None   # its digest, when it was retrieved

    def meta(self) -> Dict[str, Any]:
        return {"mode": self.mode, "graph_seeds": self.graph_seeds, "latency_ms": self.latency_ms,
                "counts": self.counts, "gaps": list(self.gaps), "mission_id": self.mission_id}


@dataclass
class Prior:
    """The previous assistant answer (`chat.2`): its text and the sources it cited (stored Source dicts, in citation
    order, at most CARRY_MAX)."""
    text: str
    cited: List[Dict[str, Any]] = field(default_factory=list)


def prior_answer(history: List[Dict[str, Any]]) -> Optional[Prior]:
    """The latest assistant answer with text that is not an error, from stored messages (oldest first)."""
    for m in reversed(history or []):
        if m.get("role") != "assistant" or m.get("status") == "error" or not (m.get("content") or "").strip():
            continue
        by_id = {s.get("id"): s for s in m.get("sources") or [] if isinstance(s, dict) and s.get("id")}
        cited = [by_id[c] for c in dict.fromkeys(m.get("citations") or []) if c in by_id][:CARRY_MAX]
        return Prior(text=m.get("content") or "", cited=cited)
    return None


# ── small helpers ─────────────────────────────────────────────────────────────────────────────────
def snippet(text: Optional[str], n: int = SNIPPET_CHARS) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1].rsplit(" ", 1)[0] + "…"


def _cap(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _fmt(v: Any) -> str:
    if isinstance(v, dt.datetime):        # `2026-09-01 18:00:00`: a space, never `T` (`chat.2` number rule)
        out = v.replace(microsecond=0).isoformat(sep=" ")
        return out[:-6] + " UTC" if out.endswith("+00:00") else out
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, float):
        return ("%.2f" % v).rstrip("0").rstrip(".") if v != int(v) else str(int(v))
    if isinstance(v, dict):
        return ", ".join("%s=%s" % (k, _fmt(x)) for k, x in v.items() if x not in (None, "", [], {}))
    if isinstance(v, (list, tuple)):
        return "; ".join(_fmt(x) for x in v)
    return " ".join(str(v).split())


def render_facts(d: Dict[str, Any], skip: Tuple[str, ...] = ()) -> str:
    """A digest dict as `key: value` lines; lists of rows as `- k=v, k=v` lines (numbers kept verbatim)."""
    lines: List[str] = []
    for k, v in d.items():
        if k in skip or v in (None, "", [], {}):
            continue
        if isinstance(v, list):
            lines.append("%s (%d):" % (k, len(v)))
            lines.extend("- " + _fmt(x) for x in v)
        else:
            lines.append("%s: %s" % (k, _fmt(v)))
    return "\n".join(lines)


def _ordered_unique(items) -> List[Any]:
    return list(dict.fromkeys(items))


def extract_entities(*texts: Optional[str]) -> Dict[str, List[Any]]:
    out: Dict[str, List[Any]] = {"missions": [], "assets": [], "locations": [], "flags": []}
    for t in texts:
        if not t:
            continue
        out["missions"] += [m.upper() for m in MISSION_RE.findall(t)]
        out["assets"] += [m.upper() for m in ASSET_RE.findall(t)]
        out["locations"] += [m.upper() for m in WAYPOINT_RE.findall(t)]
        for rx in FLAG_RES:
            out["flags"] += [int(x) for x in rx.findall(t)]
    return {k: _ordered_unique(v) for k, v in out.items()}


def prior_entity_list(prior: Optional[Prior], cap: int = PRIOR_ENTITY_CAP) -> List[Tuple[str, Any]]:
    """At most `cap` (kind, id) pairs from the previous answer: the ids named in its text, in order of appearance,
    then the refs of the sources it cited (mission, asset, location, flag)."""
    if prior is None:
        return []
    text = prior.text or ""
    hits: List[Tuple[int, str, Any]] = []
    for key, rx in (("missions", MISSION_RE), ("assets", ASSET_RE), ("locations", WAYPOINT_RE)):
        hits += [(m.start(), key, m.group(0).upper()) for m in rx.finditer(text)]
    for rx in FLAG_RES:
        hits += [(m.start(), "flags", int(m.group(1))) for m in rx.finditer(text)]
    ordered = [(k, v) for _, k, v in sorted(hits, key=lambda h: h[0])]
    for s in prior.cited:
        refs = s.get("refs") or {}
        for key, ref in (("missions", "mission_id"), ("assets", "asset_id"), ("locations", "location_id"),
                         ("flags", "flag_id")):
            v = refs.get(ref)
            if v in (None, ""):
                continue
            if key == "flags":
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    continue
            else:
                v = str(v).upper()
            ordered.append((key, v))
    return _ordered_unique(ordered)[:cap]


def group_entities(pairs: List[Tuple[str, Any]]) -> Dict[str, List[Any]]:
    out: Dict[str, List[Any]] = {"missions": [], "assets": [], "locations": [], "flags": []}
    for key, v in pairs:
        if v not in out[key]:
            out[key].append(v)
    return out


def prior_entities(prior: Optional[Prior], cap: int = PRIOR_ENTITY_CAP) -> Dict[str, List[Any]]:
    return group_entities(prior_entity_list(prior, cap))


def _digest_key(kind: str, d: Dict[str, Any]) -> Tuple[str, Any]:
    if kind == "flags":
        try:
            return kind, int(d.get("flag_id"))
        except (TypeError, ValueError):
            return kind, d.get("flag_id")
    col = {"missions": "mission_id", "assets": "asset_id", "locations": "location_id"}[kind]
    return kind, str(d.get(col) or "").upper()


def merge_entities(*groups: Dict[str, List[Any]]) -> Dict[str, List[Any]]:
    return {k: _ordered_unique(x for g in groups for x in g.get(k, [])) for k in
            ("missions", "assets", "locations", "flags")}


def entity_node_ids(ents: Dict[str, List[Any]]) -> List[str]:
    return (["mission:%s" % x for x in ents["missions"]] + ["asset:%s" % x for x in ents["assets"]]
            + ["location:%s" % x for x in ents["locations"]] + ["flag:%s" % x for x in ents["flags"]])


def refs_from_id(node_id: str) -> Dict[str, Any]:
    kind, _, key = node_id.partition(":")
    if kind == "flag" and key.isdigit():
        return {"flag_id": int(key)}
    k = {"mission": "mission_id", "asset": "asset_id", "location": "location_id", "action": "action_id"}.get(kind)
    return {k: key} if k else {}


def _refs(row: Dict[str, Any], keys=("mission_id", "asset_id", "location_id", "action_id", "flag_id")):
    return {k: row[k] for k in keys if row.get(k) not in (None, "")}


def _score(v: Any) -> Optional[float]:
    try:
        return None if v is None else round(float(v), 4)
    except (TypeError, ValueError):
        return None


# ── digests -> sources ────────────────────────────────────────────────────────────────────────────
def _join(*parts: Optional[str]) -> str:
    return " ".join(p for p in parts if p)


def digest_source(kind: str, d: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    if kind == "missions":
        sid = "mission:%s" % d.get("mission_id")
        title = _join("Mission %s" % d.get("mission_id"),
                      ("· %s" % d["mission_name"]) if d.get("mission_name") else None)
        snip = _join(d.get("scenario") and "%s." % d["scenario"],
                     d.get("mission_status") and "Reported %s." % d["mission_status"],
                     d.get("actions") is not None and "%s actions, %s failed." % (d.get("actions"), d.get("failed")),
                     d.get("start_battery_pct") is not None and "Battery %s%% to %s%%." % (
                         _fmt(d.get("start_battery_pct")), _fmt(d.get("end_battery_pct"))))
        refs = {"mission_id": d.get("mission_id")}
        skind = "mission"
    elif kind == "assets":
        sid = "asset:%s" % d.get("asset_id")
        title = _join("Asset %s" % d.get("asset_id"), ("· %s" % d["asset_name"]) if d.get("asset_name") else None)
        snip = _join(d.get("asset_type"), d.get("criticality") and "criticality %s" % d["criticality"],
                     d.get("location_name") and "at %s." % d["location_name"],
                     d.get("inspections") is not None and "%s inspections, %s failed, %d anomalies." % (
                         d.get("inspections"), d.get("failed"), len(d.get("anomalies") or [])))
        refs = _refs(d, ("asset_id", "location_id"))
        skind = "asset"
    elif kind == "locations":
        sid = "location:%s" % d.get("location_id")
        title = _join("Waypoint %s" % d.get("location_id"),
                      ("· %s" % d["location_name"]) if d.get("location_name") else None)
        snip = _join(d.get("zone_type"), d.get("environment") and "(%s)." % d["environment"],
                     d.get("actions") is not None and "%s actions, %s failed" % (d.get("actions"), d.get("failed")),
                     d.get("success_rate_pct") is not None and "(%s%% success)." % _fmt(d["success_rate_pct"]))
        refs = _refs(d, ("location_id",))
        skind = "location"
    else:
        sid = "flag:%s" % d.get("flag_id")
        title = _join("Flag #%s" % d.get("flag_id"), ("· %s" % d["title"]) if d.get("title") else None)
        snip = _join(d.get("severity"), d.get("status") and "· %s" % d["status"],
                     d.get("mission_id") and "· mission %s" % d["mission_id"],
                     d.get("asset_id") and "· asset %s" % d["asset_id"])
        refs = _refs(d)
        skind = "flag"
    text = _cap(render_facts(d), DIGEST_CHARS)
    src = {"id": sid, "origin": "sql", "kind": skind, "title": title, "snippet": snippet(snip or text),
           "score": None, "refs": {k: v for k, v in refs.items() if v not in (None, "")}}
    return src, text


STAT_DEFS = (
    ("stat:fleet", "Fleet totals (missions, actions, anomalies, open flags)", None),
    ("stat:battery_bands", "Action success rate by battery band", "battery_bands"),
    ("stat:config", "Action success rate by configuration version", "by_config"),
    ("stat:locations", "Waypoints with the lowest success rate", "worst_locations"),
    ("stat:assets", "Assets with the most anomalies (repeat offenders)", "repeat_offenders"),
    ("stat:failure_types", "Most common failure types", "failure_types"),
)
SNAPSHOT_LISTS = {k for _, _, k in STAT_DEFS if k}


def stat_sources(snap: Dict[str, Any]) -> List[Tuple[Dict[str, Any], str]]:
    out = []
    for sid, title, key in STAT_DEFS:
        if key is None:
            text = render_facts({k: v for k, v in snap.items() if k not in SNAPSHOT_LISTS})
        else:
            rows = snap.get(key) or []
            text = "\n".join("- " + _fmt(r) for r in rows)
        if not text:
            continue
        text = _cap(text, STAT_CHARS)
        out.append(({"id": sid, "origin": "sql", "kind": "stat", "title": title, "snippet": snippet(text),
                     "score": None, "refs": {}}, text))
    return out


# ── the retriever ─────────────────────────────────────────────────────────────────────────────────
class Retriever:
    def __init__(self, repo, embed, search_embed_timeout_s: float = 4.0, budget_s: float = 8.0,
                 max_chars: int = MAX_CONTEXT_CHARS):
        self.repo = repo
        self.embed = embed
        self.search_embed_timeout_s = search_embed_timeout_s
        self.budget_s = budget_s
        self.max_chars = max_chars
        self._snapshot: Optional[Dict[str, Any]] = None
        self._snapshot_at = float("-inf")

    async def snapshot(self) -> Dict[str, Any]:
        if self._snapshot is not None and time.monotonic() - self._snapshot_at < SNAPSHOT_TTL_S:
            return self._snapshot
        snap = await self.repo.fleet_snapshot()
        self._snapshot, self._snapshot_at = snap, time.monotonic()
        return snap

    async def _embed(self, q: str, request_id: Optional[str], timeout_s: float) -> Tuple[Optional[str], Optional[str]]:
        if self.embed is None:
            return None, "no embedding client: full-text search only"
        try:
            vec = await asyncio.wait_for(self.embed.embed_query(q, request_id=request_id, timeout_s=timeout_s),
                                         timeout_s + 0.25)
            return vec_literal(vec), None
        except ServiceUnavailable as exc:
            return None, "embedding unavailable (%s): full-text search only" % _short(exc.reason)
        except asyncio.TimeoutError:
            return None, "embedding slower than %gs: full-text search only" % round(timeout_s, 1)

    @staticmethod
    async def _settle(tasks: Dict[str, asyncio.Task], timeout: float) -> Tuple[Dict[str, Any], Dict[str, str]]:
        """Wait for `tasks` up to `timeout`; returns (results, failures) by name. Late tasks are cancelled."""
        if not tasks:
            return {}, {}
        try:
            done, pending = await asyncio.wait(list(tasks.values()), timeout=max(0.0, timeout))
        except asyncio.CancelledError:        # the turn was stopped: don't leave queries running
            for t in tasks.values():
                t.cancel()
            raise
        for t in pending:
            t.cancel()
        results, failed = {}, {}
        for name, t in tasks.items():
            if t in pending:
                failed[name] = "timed out (retrieval budget)"
            elif t.exception() is not None:
                failed[name] = "failed: %s" % _short(short_error(t.exception()))
            else:
                results[name] = t.result()
        return results, failed

    async def retrieve(self, question: str, previous: Optional[str] = None, request_id: Optional[str] = None,
                       budget_s: Optional[float] = None, prior: Optional[Prior] = None,
                       mission_id: Optional[str] = None) -> Retrieval:
        """`previous` = the previous user message; `prior` = the previous assistant answer (entities + carry-over);
        `mission_id` = the conversation's mission (chat.3, §9), or None for an ordinary chat."""
        t0 = time.perf_counter()
        loop = asyncio.get_running_loop()
        budget = budget_s or self.budget_s
        deadline = loop.time() + budget
        gaps: List[str] = []
        mid = str(mission_id).strip().upper() if mission_id else None
        named_ents = extract_entities(question, previous)
        # Ids only the previous answer names: their digests rank with the fresh results (interleaved, just ahead of
        # the i-th document) rather than with the question's own ids, so a list-style answer can't crowd out the
        # documents of a follow-up on a new topic.
        prior_only = [(k, v) for k, v in prior_entity_list(prior) if v not in named_ents[k]]
        prior_rank = {kv: i for i, kv in enumerate(prior_only)}
        ents = merge_entities(named_ents, group_entities(prior_only))
        if mid:      # the pinned digest has its own query (below), so it can't be crowded out of the <= 5 per kind
            ents["missions"] = [m for m in ents["missions"] if m != mid]

        tasks: Dict[str, asyncio.Task] = {"snapshot": asyncio.ensure_future(self.snapshot())}
        if mid:
            tasks["mission"] = asyncio.ensure_future(self.repo.entity_digests(mission_ids=[mid]))
        if any(ents.values()):
            tasks["digests"] = asyncio.ensure_future(self.repo.entity_digests(
                mission_ids=ents["missions"], asset_ids=ents["assets"], location_ids=ents["locations"],
                flag_ids=ents["flags"]))
        if previous:
            q2 = "%s\n%s" % (previous, question)
            tasks["docs2"] = asyncio.ensure_future(self.repo.search_documents(q2, None, k=4))
            tasks["graph2"] = asyncio.ensure_future(self.repo.match_graph_nodes(q2, None, k=3))
        try:
            vec, why = await self._embed(question, request_id,
                                         min(self.search_embed_timeout_s, max(0.5, 0.6 * budget)))
        except asyncio.CancelledError:
            for t in tasks.values():
                t.cancel()
            raise
        if why:
            gaps.append(why)
        tasks["docs"] = asyncio.ensure_future(self.repo.search_documents(question, vec, k=8))
        if mid:
            tasks["mission_docs"] = asyncio.ensure_future(self.repo.search_documents(
                question, vec, k=MISSION_DOCS_K, mission_id=mid))
        tasks["graph"] = asyncio.ensure_future(self.repo.match_graph_nodes(question, vec, k=6))
        res, failed = await self._settle(tasks, deadline - loop.time())
        gaps += ["%s %s" % (STEP_LABELS.get(k, k), v) for k, v in failed.items()]
        if all(k in failed for k in ("docs", "graph", "snapshot")):
            _warn_gaps(gaps, request_id)
            raise RetrievalFailed("retrieval failed: " + "; ".join("%s %s" % (STEP_LABELS[k], failed[k])
                                                                   for k in ("docs", "graph", "snapshot")))
        docs, mode = res.get("docs") or ([], "hybrid" if vec else "fulltext")
        graph = [r for r in res.get("graph") or [] if _useful_node(r)]
        docs2 = (res.get("docs2") or ([], "fulltext"))[0]
        graph2 = [r for r in res.get("graph2") or [] if _useful_node(r)]
        mission_docs = (res.get("mission_docs") or ([], mode))[0]
        mission = next(iter((res.get("mission") or {}).get("missions") or []), None)

        # Graph entry nodes of entity kinds add digests; the top entry nodes seed the neighbourhood walk.
        extra = {"missions": [], "assets": [], "locations": [], "flags": []}
        for r in graph + graph2:
            key = ENTITY_KINDS.get(r.get("kind"))
            ident = str(r.get("node_id", "")).partition(":")[2]
            if key and ident:
                val = int(ident) if key == "flags" and ident.isdigit() else ident
                if key == "flags" and not isinstance(val, int):
                    continue
                if key == "missions" and val == mid:        # already fetched as the pinned digest
                    continue
                if val not in ents[key] and val not in extra[key]:
                    extra[key].append(val)
        seeds = _ordered_unique(entity_node_ids(ents) + [r["node_id"] for r in graph] +
                                [r["node_id"] for r in graph2])
        if mid:      # chat.3: the mission node is always a seed, on top of the usual entry nodes
            mnode = "mission:%s" % mid
            seeds = [mnode] + [x for x in seeds if x != mnode][:SEED_COUNT]
        else:
            seeds = seeds[:SEED_COUNT]
        tasks2: Dict[str, asyncio.Task] = {}
        hubs = [s for s in seeds if s.partition(":")[0] in HUB_KINDS]
        others = [s for s in seeds if s not in hubs]
        if others:
            tasks2["nb"] = asyncio.ensure_future(self.repo.graph_neighborhood(others, max_hops=2, max_nodes=25))
        if hubs:
            tasks2["nb_hub"] = asyncio.ensure_future(self.repo.graph_neighborhood(
                hubs, max_hops=2, max_nodes=25, kind_filter=NON_INSTANCE_KINDS))
        if any(extra.values()):
            tasks2["digests2"] = asyncio.ensure_future(self.repo.entity_digests(
                mission_ids=extra["missions"], asset_ids=extra["assets"], location_ids=extra["locations"],
                flag_ids=extra["flags"]))
        if tasks2 and deadline - loop.time() <= 0:
            for t in tasks2.values():
                t.cancel()
            gaps.append("graph neighbourhood skipped: retrieval budget used up")
            res2: Dict[str, Any] = {}
        else:
            res2, failed2 = await self._settle(tasks2, deadline - loop.time())
            gaps += ["%s %s" % (STEP_LABELS.get(k, k), v) for k, v in failed2.items()]
        neighbours = sorted(list(res2.get("nb") or []) + list(res2.get("nb_hub") or []),
                            key=lambda r: (r.get("hop") or 9, -(r.get("weight") or 0.0)))

        b = _Builder()
        pinned: List[str] = []
        if mission is not None:
            msrc, mtext = digest_source("missions", mission)
            b.add(msrc, mtext, rank=(-1,))
            pinned.append(msrc["id"])
        for i, r in enumerate(mission_docs):       # ahead of every global result; a doc found twice merges here
            b.add(*doc_source(r), rank=(0.5, i))
        named = res.get("digests") or {}
        for kind in ("missions", "assets", "locations", "flags"):
            for d in named.get(kind) or []:
                i = prior_rank.get(_digest_key(kind, d))
                b.add(*digest_source(kind, d), rank=(0, b.next()) if i is None else (1, i, -1))
        found = res2.get("digests2") or {}
        later = [(kind, d) for kind in ("missions", "assets", "locations", "flags") for d in found.get(kind) or []]
        doc_ids = {r.get("doc_id") for r in docs} | {r.get("doc_id") for r in mission_docs}
        for i in range(max(len(docs), len(graph), len(later))):
            if i < len(docs):
                b.add(*doc_source(docs[i]), rank=(1, i, 0))
            if i < len(graph):
                b.add(*node_source(graph[i], doc_ids), rank=(1, i, 1))
            if i < len(later):
                b.add(*digest_source(*later[i]), rank=(1, i, 2))
        for i, r in enumerate(docs2):
            b.add(*doc_source(r), rank=(2, i, 0))
        for i, r in enumerate(graph2):
            b.add(*node_source(r, doc_ids), rank=(2, i, 1))
        for i, r in enumerate(neighbours[:25]):     # a seed reached from another seed merges in with its path
            b.add(*node_source(r, doc_ids), rank=(3, i))
        stats = stat_sources(res["snapshot"]) if "snapshot" in res else []
        carry = [carry_source(s) for s in (prior.cited if prior else [])]
        sources, texts, block = b.finish(stats, self.max_chars, carry=carry, pinned=pinned)
        counts = {"rag": 0, "graph": 0, "sql": 0}
        for s in sources:
            counts[s["origin"]] = counts.get(s["origin"], 0) + 1
        _warn_gaps(gaps, request_id)
        return Retrieval(sources=sources, texts=texts, block=block, mode=mode, graph_seeds=seeds,
                         latency_ms=int((time.perf_counter() - t0) * 1000), counts=counts, gaps=gaps,
                         mission_id=mid, mission=mission)


def _short(text: Any, n: int = 160) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _warn_gaps(gaps: List[str], request_id: Optional[str]) -> None:
    for g in gaps:
        log.warning("chat retrieval degraded (request %s): %s", request_id or "-", g)


def carry_source(s: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    """A source the previous answer cited, as stored (title + snippet): carried into this turn's <sources>."""
    src = {"id": s["id"], "origin": s.get("origin") or "sql", "kind": s.get("kind") or s["id"].partition(":")[0],
           "title": s.get("title") or s["id"], "snippet": s.get("snippet") or "", "score": s.get("score"),
           "refs": dict(s.get("refs") or {})}
    if s.get("path"):
        src["path"] = s["path"]
    return src, " ".join((s.get("snippet") or "").split())


def _useful_node(r: Dict[str, Any]) -> bool:
    return r.get("match") != "vector" or (r.get("score") or 0.0) >= MIN_VECTOR_SCORE


def doc_source(r: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    content = r.get("content") or ""
    src = {"id": r["doc_id"], "origin": "rag", "kind": r.get("kind") or "document",
           "title": r.get("title") or r["doc_id"], "snippet": snippet(content), "score": _score(r.get("score")),
           "refs": _refs(r)}
    return src, _cap(" ".join(content.split()), DOC_CHARS)


def node_source(r: Dict[str, Any], doc_ids: set) -> Tuple[Dict[str, Any], str]:
    nid = r["node_id"]
    path = r.get("path")
    src = {"id": nid, "origin": "graph", "kind": r.get("kind") or nid.partition(":")[0],
           "title": r.get("label") or nid, "snippet": snippet(r.get("summary")),
           "score": _score(r.get("score") if r.get("score") is not None else r.get("weight")),
           "refs": refs_from_id(nid)}
    if path:
        src["path"] = " ".join(str(p) for p in path)
    text = r.get("summary") or ""
    if r.get("evidence"):
        text += " (evidence: %s)" % r["evidence"]
    if r.get("doc_id") and r["doc_id"] in doc_ids and r["doc_id"] != nid:
        text += " (document: %s)" % r["doc_id"]
    return src, _cap(" ".join(text.split()), NODE_CHARS)


class _Builder:
    """Collects candidate sources, merges duplicates by id, then applies the character budget."""

    def __init__(self):
        self.items: Dict[str, Dict[str, Any]] = {}
        self._n = 0

    def next(self) -> int:
        self._n += 1
        return self._n

    def add(self, src: Dict[str, Any], text: str, rank: tuple) -> None:
        sid = src["id"]
        cur = self.items.get(sid)
        if cur is None:
            self.items[sid] = {"src": dict(src), "text": text, "rank": rank}
            return
        # A digest and a graph node (or a document) with the same id are ONE source: keep the richer origin,
        # the better rank, every text once, and the graph path.
        s = cur["src"]
        if ORIGIN_RANK.get(src["origin"], 9) < ORIGIN_RANK.get(s["origin"], 9):
            keep_path = s.get("path")
            cur["src"] = s = {**src, **({"path": keep_path} if keep_path and not src.get("path") else {})}
        elif src.get("path") and not s.get("path"):
            s["path"] = src["path"]
        if text and text not in cur["text"]:
            cur["text"] = _cap(cur["text"] + "\n" + text, DIGEST_CHARS)
        if s.get("score") is None and src.get("score") is not None:
            s["score"] = src["score"]
        for k, v in (src.get("refs") or {}).items():
            s.setdefault("refs", {}).setdefault(k, v)
        cur["rank"] = min(cur["rank"], rank)

    def finish(self, stats: List[Tuple[Dict[str, Any], str]], max_chars: int,
               carry: Optional[List[Tuple[Dict[str, Any], str]]] = None, pinned: Optional[List[str]] = None):
        """Budget order: pinned sources (chat.3: the mission digest) and the snapshot always stay, then fresh sources
        best rank first, then carry-over (dropped first). Block order: pinned, fresh, carry-over, snapshot. A carried
        id that is also kept fresh keeps the fresh text."""
        stat_ids = {s["id"] for s, _ in stats}
        pin_ids = [p for p in (pinned or []) if p in self.items and p not in stat_ids]
        ranked = sorted((v for k, v in self.items.items() if k not in stat_ids and k not in pin_ids),
                        key=lambda v: v["rank"])
        stat_blocks = [(s, t, render_source(s, t)) for s, t in stats]
        used = len(sources_block([])) + sum(len(r) + 1 for _, _, r in stat_blocks)
        kept: List[Tuple[Dict[str, Any], str, str]] = []
        for p in pin_ids:                          # never dropped, whatever the budget
            v = self.items[p]
            r = render_source(v["src"], v["text"])
            kept.append((v["src"], v["text"], r))
            used += len(r) + 1
        for v in ranked:
            r = render_source(v["src"], v["text"])
            if used + len(r) + 1 <= max_chars:
                kept.append((v["src"], v["text"], r))
                used += len(r) + 1
        have = {s["id"] for s, _, _ in kept} | stat_ids
        for s, t in carry or []:
            if s["id"] in have:
                continue
            r = render_source(s, t)
            if used + len(r) + 1 <= max_chars:
                kept.append((s, t, r))
                have.add(s["id"])
                used += len(r) + 1
        final = kept + stat_blocks
        sources, texts = [], []
        for i, (s, t, rendered) in enumerate(final, start=1):
            out = {"id": s["id"], "n": i, "origin": s["origin"], "kind": s.get("kind"), "title": s.get("title"),
                   "snippet": s.get("snippet") or "", "score": s.get("score"), "refs": s.get("refs") or {}}
            if s.get("path"):
                out["path"] = s["path"]
            sources.append(out)
            texts.append(rendered)
        return sources, texts, sources_block([r for _, _, r in final])
