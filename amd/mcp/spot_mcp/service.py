"""Tool implementations, independent of MCP (so they're unit-testable with a fake repo and embedder).

Citation ids every result carries:
  flag:<flag_id>  action:<action_id>  reading:<reading_id>  anomaly:<anomaly_id>  asset:<asset_id>
  location:<location_id>  mission:<mission_id>  note:<ack_id>  explanation:<assessment_id>  s1:<prediction_id>
  log:<log_id>  stat:<filters>  F<n> (derived facts, packet-local)  and RAG doc_ids (event:, incident:, manual:, policy:...)
  Graph facts (contract §7) are cited by the reached node's node_id (failure_type:X, environment:Y, topic:z...) or,
  for document-backed nodes, its doc_id; `ev` carries the edge's evidence id when there is one.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import inspect
import json
import logging
import re
import time
from decimal import Decimal
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Set, Tuple

from . import domain as D
from . import graph as G
from .config import Settings
from .db import LogSink, NullLogSink
from .embed import Embedder, EmbeddingError
from .repo import GraphUnavailable, Repo

log = logging.getLogger(__name__)

SNIPPET_CHARS = 260
# graph walks: (hops, kind_filter, max_nodes). The packet keeps to reusable knowledge; the tools also show the
# entity's own direct neighbours of any kind (an asset's actions and anomalies) in a small 1-hop walk.
PACKET_WALKS = [(2, G.STRUCT_KINDS, 30), (2, G.DOC_LINK_KINDS, 12)]
TOOL_KINDS = G.STRUCT_KINDS + G.DOC_LINK_KINDS + ["explanation"]  # vector entry points for the graph tools
SEV_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
_ID_TOKEN = re.compile(r"\b(?:[A-Z]{1,6}-\d{2,4}|A\d{4}|AN\d{3}|WP\d{2}|M\d{3}|R\d{4}|E\d{5})\b")
_NOISE_KEYS = {"synthetic", "created_at", "ingested_at", "payload", "embedding", "content_tsv"}
# flags.reasons rows that record who decided (System 1 / the rules), not a policy rule that fired: never a
# policy:<rule> doc or a policy_rule:<rule> node (same names as amd/orchestrator/orchestrator/template.py).
VERDICT_RULES = ("laya_verdict", "rules_verdict", "laya_unavailable")
MAX_FIRED_RULES = 8      # fired rules shown in the flag view (the backend's SQL fallback packet keeps 8 too)
KG_CHECK_TTL_S = 60.0    # how long one "is kg_nodes empty?" answer is reused
KG_EMPTY = "knowledge graph is empty (graph_build not run)"
NO_GRAPH_FACTS = "no knowledge-graph facts linked to this flag"
# Older laya_unavailable rows carry the raw connection error: show the backend's stable text instead
# (backend/app/services/validator.py LAYA_UNAVAILABLE_DETAIL), never an exception string.
LAYA_UNAVAILABLE_DETAIL = "System 1 (Laya) unavailable; rules-only verdict"


# ── small helpers ───────────────────────────────────────────────────────────────────────────────
def jsonable(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    if isinstance(x, (_dt.datetime, _dt.date)):
        return x.isoformat()
    if isinstance(x, Decimal):
        return float(x)
    if isinstance(x, float):
        return round(x, 4)
    return x


def compact(d: Dict[str, Any]) -> Dict[str, Any]:
    """Drop None/empty values and noisy columns: every token counts for a 9B model."""
    return {k: v for k, v in d.items() if v not in (None, "", [], {}) and k not in _NOISE_KEYS}


def snippet(text: Optional[str], n: int = SNIPPET_CHARS) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def est_tokens(obj: Any) -> int:
    return int(len(json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str)) / 3.6) + 1


def _num(v: Any) -> Optional[float]:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def _fmt(v: Optional[float]) -> str:
    if v is None:
        return "?"
    return ("%.1f" % v).rstrip("0").rstrip(".")


def _reason_rows(f: Dict[str, Any]) -> List[Dict[str, Any]]:
    reasons = f.get("reasons") or []
    if isinstance(reasons, str):
        try:
            reasons = json.loads(reasons)
        except ValueError:
            reasons = [{"detail": reasons}]
    return [r for r in reasons if isinstance(r, dict)] if isinstance(reasons, list) else []


def fired_rules(f: Dict[str, Any]) -> List[str]:
    """The policy rules that fired for a flag, in stored order, without the verdict rows (VERDICT_RULES)."""
    out: List[str] = []
    for r in _reason_rows(f):
        rule = r.get("rule")
        if isinstance(rule, str) and rule and rule not in VERDICT_RULES and rule not in out:
            out.append(rule)
    return out


def _takes_call_ids(embedder: Any) -> bool:
    """Whether embed_queries accepts flag_id/action_id (for app.model_calls); simpler embedders (test fakes) don't."""
    try:
        params = inspect.signature(embedder.embed_queries).parameters
    except (AttributeError, TypeError, ValueError):
        return False
    return "flag_id" in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


# Packet sections _fit_budget may shorten; a note lists each one it cut (MCP_PACKET_TOKEN_BUDGET).
TRIMMED_SECTIONS = ("similar_incidents", "asset_history", "manual", "policy", "graph", "system1")
TRIM_NOTE_TOKENS = 48       # room kept for that note when trimming starts


class ContextService:
    def __init__(self, repo: Repo, embedder: Optional[Embedder], settings: Settings,
                 sink: Optional[LogSink] = None):
        self.repo, self.embedder, self.s = repo, embedder, settings
        self.sink = sink or NullLogSink()
        self._embed_ids = embedder is not None and _takes_call_ids(embedder)
        self._kg_empty: Optional[Tuple[float, bool]] = None   # (checked_at, empty)

    # ── logging wrapper (app.mcp_tool_calls) ────────────────────────────────────────────────────
    async def logged(self, tool: str, args: Dict[str, Any], fn: Callable[[], Awaitable[Dict[str, Any]]],
                     request_id: Optional[str] = None) -> Dict[str, Any]:
        t0 = time.perf_counter()
        result: Optional[Dict[str, Any]] = None
        err: Optional[str] = None
        try:
            result = jsonable(await fn())
            return result
        except Exception as exc:
            err = "%s: %s" % (type(exc).__name__, exc)
            raise
        finally:
            try:
                count, top, nbytes = None, None, None
                if result is not None:
                    nbytes = len(json.dumps(result, separators=(",", ":")))
                    count = result.get("result_count")
                    top = result.get("top_similarity")
                flag_id = args.get("flag_id")
                self.sink.tool_call({
                    "request_id": request_id, "flag_id": flag_id if isinstance(flag_id, int) else None,
                    "tool": tool, "args": args, "result_count": count, "result_bytes": nbytes,
                    "top_similarity": top, "latency_ms": int((time.perf_counter() - t0) * 1000),
                    "ok": err is None, "error": err})
            except Exception as exc:  # noqa: BLE001
                log.warning("tool-call logging failed: %s", exc)

    # ── retrieval core ──────────────────────────────────────────────────────────────────────────
    async def _embed(self, texts: List[str], request_id: Optional[str], *, flag_id: Optional[int] = None,
                     action_id: Optional[str] = None) -> List[List[float]]:
        """Embed query texts. flag_id/action_id only label the embed call's app.model_calls row."""
        if self.embedder is None:
            raise EmbeddingError("no embedding service configured (EMBED_URL)")
        ids = {k: v for k, v in (("flag_id", flag_id), ("action_id", action_id)) if v is not None}
        if ids and self._embed_ids:
            return await self.embedder.embed_queries(texts, request_id=request_id, **ids)
        return await self.embedder.embed_queries(texts, request_id=request_id)

    async def _retrieve(self, query: str, kinds: List[str], *, k: int, hybrid: bool, asset: Optional[str] = None,
                        location: Optional[str] = None, mission: Optional[str] = None,
                        exclude: Sequence[str] = (), exclude_action: Optional[str] = None,
                        cutoff: Optional[float] = None, vec: Optional[List[float]] = None,
                        request_id: Optional[str] = None) -> Dict[str, Any]:
        """Search, apply the similarity cutoff, drop excluded ids. Returns {results, top_similarity, best_rejected}."""
        cutoff = self.s.min_similarity if cutoff is None else cutoff
        if vec is None:
            vec = (await self._embed([query], request_id))[0]
        fetch_k = min(k * 2 + len(exclude), 30)
        if hybrid:
            rows = await self.repo.match_hybrid(vec, query, kinds, asset, location, mission, fetch_k)
            if rows and "similarity" not in rows[0]:
                # Older hybrid without a similarity column: score rows with a parallel vector search.
                vrows = await self.repo.match(vec, kinds, asset, location, mission, max(fetch_k * 3, 20), 0.0)
                sims = {r["doc_id"]: _num(r.get("similarity")) for r in vrows}
                tokens = set(_ID_TOKEN.findall(query))
                for r in rows:
                    r["similarity"] = sims.get(r["doc_id"])
                    body = (r.get("title") or "") + " " + (r.get("content") or "")
                    r["text_match"] = bool(tokens) and any(t in body for t in tokens)
        else:
            rows = await self.repo.match(vec, kinds, asset, location, mission, fetch_k, 0.0)
        kept, best_rejected, top = [], None, None
        for r in rows:
            if r["doc_id"] in exclude or (exclude_action and r.get("action_id") == exclude_action):
                continue
            sim = _num(r.get("similarity"))
            ok = (sim is not None and sim >= cutoff) or bool(r.get("text_match"))
            if not ok:
                best_rejected = sim if best_rejected is None or (sim or 0) > best_rejected else best_rejected
                continue
            if sim is not None:
                top = sim if top is None else max(top, sim)
            kept.append(r)
            if len(kept) >= k:
                break
        return {"results": [self._doc_item(r) for r in kept], "top_similarity": top,
                "best_rejected": best_rejected, "cutoff": cutoff}

    @staticmethod
    def _doc_item(r: Dict[str, Any], n: int = SNIPPET_CHARS) -> Dict[str, Any]:
        return compact({"id": r["doc_id"], "kind": r.get("kind"),
                        "sim": round(float(r["similarity"]), 3) if r.get("similarity") is not None else None,
                        "exact_id_match": True if r.get("text_match") and (r.get("similarity") or 0) < 0.4 else None,
                        "title": r.get("title"), "text": snippet(r.get("content"), n),
                        "asset_id": r.get("asset_id"), "ts": jsonable(r.get("ts"))})

    def _search_result(self, query: str, found: Dict[str, Any], **echo) -> Dict[str, Any]:
        out: Dict[str, Any] = {"query": query, **{k: v for k, v in echo.items() if v is not None}}
        out["results"] = found["results"]
        out["result_count"] = len(found["results"])
        out["top_similarity"] = found["top_similarity"]
        if not found["results"]:
            best = found["best_rejected"]
            out["no_relevant_results"] = True
            out["message"] = ("No relevant results above similarity %.2f%s. Do not cite anything from this search."
                              % (found["cutoff"], "" if best is None else " (best match was %.2f)" % best))
        return out

    # ── RAG tools ───────────────────────────────────────────────────────────────────────────────
    async def search_similar_incidents(self, flag_id: Optional[int] = None, text: Optional[str] = None,
                                       asset_id: Optional[str] = None, location_id: Optional[str] = None,
                                       k: int = 3, request_id: Optional[str] = None) -> Dict[str, Any]:
        k = D.check_int("k", k, 1, 10)
        asset_id = D.check_id("asset_id", asset_id) if asset_id is not None else None
        location_id = D.check_id("location_id", location_id) if location_id is not None else None
        exclude: List[str] = []
        exclude_action = None
        vec = None
        if flag_id is not None:
            flag_id = D.check_int("flag_id", flag_id, 1, 2**62)
            ctx = await self._load_flag(flag_id)
            q = self._queries(ctx)["incident"]
            exclude, exclude_action = self._self_ids(ctx), ctx.get("action_id")
            if text:
                q = D.check_text("text", text) + " | " + q
            vec = (await self._embed([q], request_id, flag_id=flag_id, action_id=exclude_action))[0]
        elif text is not None:
            q = D.check_text("text", text)
        else:
            raise ValueError("give flag_id (preferred) or text describing the incident")
        found = await self._retrieve(q, ["incident"], k=k, hybrid=True, asset=asset_id, location=location_id,
                                     exclude=exclude, exclude_action=exclude_action, vec=vec,
                                     request_id=request_id)
        return self._search_result(q, found, flag_id=flag_id)

    async def search_asset_history(self, asset_id: str, query: Optional[str] = None, k: int = 5,
                                   request_id: Optional[str] = None) -> Dict[str, Any]:
        asset_id = D.check_id("asset_id", asset_id)
        k = D.check_int("k", k, 1, 10)
        if query is not None:
            q, cutoff = D.check_text("query", query), None
        else:  # every doc already belongs to this asset, so no relevance cutoff
            q, cutoff = "inspection history, failures, anomalies and operator notes for asset %s" % asset_id, -1.0
        found = await self._retrieve(q, ["event", "incident", "operator_note"], k=k, hybrid=True, asset=asset_id,
                                     cutoff=cutoff, request_id=request_id)
        return self._search_result(q, found, asset_id=asset_id)

    async def search_spot_manual(self, query: str, k: int = 3, request_id: Optional[str] = None) -> Dict[str, Any]:
        q = D.check_text("query", query)
        found = await self._retrieve(q, ["manual"], k=D.check_int("k", k, 1, 10), hybrid=False, request_id=request_id)
        return self._search_result(q, found)

    async def search_operator_notes(self, query: str, asset_id: Optional[str] = None, k: int = 5,
                                    request_id: Optional[str] = None) -> Dict[str, Any]:
        q = D.check_text("query", query)
        asset_id = D.check_id("asset_id", asset_id) if asset_id is not None else None
        found = await self._retrieve(q, ["operator_note"], k=D.check_int("k", k, 1, 10), hybrid=True,
                                     asset=asset_id, request_id=request_id)
        return self._search_result(q, found, asset_id=asset_id)

    async def search_past_explanations(self, query: str, k: int = 3,
                                       request_id: Optional[str] = None) -> Dict[str, Any]:
        q = D.check_text("query", query)
        found = await self._retrieve(q, ["explanation"], k=D.check_int("k", k, 1, 10), hybrid=False,
                                     request_id=request_id)
        return self._search_result(q, found)

    async def search_policy(self, query: str, k: int = 3, request_id: Optional[str] = None) -> Dict[str, Any]:
        q = D.check_text("query", query)
        found = await self._retrieve(q, ["policy"], k=D.check_int("k", k, 1, 10), hybrid=True, request_id=request_id)
        out = self._search_result(q, found)
        out["policy_source"] = D.POLICY_SOURCE
        return out

    # ── Graph RAG (contract §7) ─────────────────────────────────────────────────────────────────
    def _graph_cutoff(self) -> float:
        g = self.s.graph_min_similarity
        return self.s.min_similarity if g is None else g

    @staticmethod
    def _tool_walks(hops: int, k: int) -> List[Tuple[int, Optional[List[str]], int]]:
        return [(hops, G.STRUCT_KINDS, min(max(k, 20), 40)), (hops, G.DOC_LINK_KINDS + ["explanation"], 12),
                (1, None, 15)]

    @staticmethod
    def _graph_unavailable(exc: Exception) -> Dict[str, Any]:
        return {"graph_unavailable": True, "facts": [], "result_count": 0,
                "message": "Graph unavailable: %s. Use the search_* and get_* tools instead." % exc}

    async def _graph_is_empty(self) -> bool:
        """True when kg_nodes has no rows (db/graph_build.py not run). One EXISTS check, reused for KG_CHECK_TTL_S.
        Raises GraphUnavailable (no migration 009). A repo without the check, or a failed check, counts as not
        empty, so the normal lookups run and report what they find."""
        check = getattr(self.repo, "kg_has_nodes", None)
        if check is None:
            return False
        now = time.monotonic()
        if self._kg_empty is not None and now - self._kg_empty[0] < KG_CHECK_TTL_S:
            return self._kg_empty[1]
        try:
            empty = not await check()
        except GraphUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - only picks the message; the real lookups still run
            log.info("kg_nodes emptiness check failed: %s", exc)
            return False
        self._kg_empty = (now, empty)
        return empty

    @staticmethod
    def _graph_empty_result(**echo) -> Dict[str, Any]:
        return {**echo, "graph_empty": True, "facts": [], "result_count": 0, "no_relevant_results": True,
                "message": "The %s, so it has no facts yet. Use the search_* and get_* tools instead. Do not cite "
                           "anything from this call." % KG_EMPTY}

    @staticmethod
    def _node_item(n: Dict[str, Any], **extra) -> Dict[str, Any]:
        nid = n["node_id"]
        kind = n.get("kind") or G.kind_of(nid)
        cid = G.cite_id(nid, kind, n.get("doc_id"))
        return compact({"id": cid, "node_id": nid if nid != cid else None, "kind": kind, "label": n.get("label"),
                        "summary": snippet(n.get("summary"), 160), **extra})

    async def _graph_facts(self, seeds: Sequence[str], walks: Sequence[Tuple[int, Optional[List[str]], int]],
                           context: Set[str], *, extra: Sequence[str] = (),
                           drop_fk_within: Optional[Set[str]] = None, token_budget: Optional[int] = None,
                           max_items: Optional[int] = None,
                           caps: Optional[Dict[Tuple[int, bool], int]] = G.TIER_CAPS,
                           fill: bool = True) -> List[Dict[str, Any]]:
        """Neighbourhood -> oriented, labelled, ranked, de-duplicated facts. Raises GraphUnavailable.

        `walks` are (hops, kind_filter, max_nodes) graph_neighborhood calls run in parallel and unioned (see
        G.STRUCT_KINDS). `extra` nodes (the flag's environment and action type) join the node set without being
        walked from. The facts are the stored edges among the whole set."""
        parts = await asyncio.gather(*[self.repo.graph_neighborhood(list(seeds), h, None, kinds, n)
                                       for h, kinds, n in walks], return_exceptions=True)
        for p in parts:
            if isinstance(p, BaseException):
                raise p
        rows = [r for part in parts for r in part]
        details = None
        if rows or extra:
            try:  # every edge among the neighbourhood's nodes, with direction, props (n/rate) and seed labels
                details = await self.repo.kg_details(list(seeds) + list(extra) +
                                                     [r["node_id"] for r in rows if r.get("node_id")])
            except Exception as exc:  # noqa: BLE001 - fall back to the traversal's own edges, keys as labels
                log.info("kg_details lookup failed, using traversal edges oriented by relation kinds: %s", exc)
        cands, nodes = G.candidates(rows, seeds, details, context, extra)
        ranked = G.rank(cands, context, drop_fk_within)
        return G.pack(ranked, nodes, est_tokens, token_budget=token_budget, max_items=max_items, caps=caps, fill=fill)

    async def _resolve_entity(self, entity: str, request_id: Optional[str]):
        """node_id -> exact lookup; else name/id via match_kg_nodes_text; else vector via match_kg_nodes.
        Returns (node or None, resolved_by, alternatives, similarity, note)."""
        kind, key = G.split_node(entity)
        kinds = [kind] if kind else None
        if kind:
            det = await self.repo.kg_details([entity])
            hit = next((n for n in det.get("nodes") or [] if n.get("node_id") == entity), None)
            if hit:
                return hit, "node_id", [], None, None
        text = key if kind else entity
        rows = [r for r in await self.repo.match_kg_nodes_text(text, kinds, 5)
                if (_num(r.get("score")) or 0) >= self.s.graph_text_min_score]
        alt = lambda rs: [compact({"id": r["node_id"], "label": r.get("label")}) for r in rs[1:4]]  # noqa: E731
        if rows:
            return rows[0], "text", alt(rows), None, None
        try:
            vec = (await self._embed([text], request_id))[0]
        except EmbeddingError as exc:
            return None, None, [], None, "no name/id match, and the vector fallback was skipped (%s)" % exc
        rows = await self.repo.match_kg_nodes(vec, kinds or TOOL_KINDS, 4, self._graph_cutoff())
        if rows:
            return rows[0], "vector", alt(rows), _num(rows[0].get("similarity")), None
        return None, None, [], None, None

    async def graph_neighborhood(self, entity: str, hops: int = 2, k: int = 20,
                                 request_id: Optional[str] = None) -> Dict[str, Any]:
        entity = D.check_text("entity", entity, max_len=120)
        hops = D.check_int("hops", hops, 1, 3)
        k = D.check_int("k", k, 1, 30)
        try:
            if await self._graph_is_empty():
                return self._graph_empty_result(entity=entity)
            node, how, alts, sim, note = await self._resolve_entity(entity, request_id)
            if node is None:
                out: Dict[str, Any] = {
                    "entity": entity, "facts": [], "result_count": 0, "no_relevant_results": True,
                    "message": "No graph node matches %r. Pass a node_id such as 'asset:HX-204', 'location:WP09' or "
                               "'failure_type:LOCALIZATION_FAILURE', or use search_graph. Do not cite anything from "
                               "this call." % entity}
                if note:
                    out["note"] = note
                return out
            nid = node["node_id"]
            facts = await self._graph_facts([nid], self._tool_walks(hops, k), {nid}, max_items=k)
        except GraphUnavailable as exc:
            return self._graph_unavailable(exc)
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "57014":  # statement timeout
                raise ValueError("the %d-hop walk from %s timed out; use hops=1 or 2" % (hops, entity)) from exc
            raise
        out = {"entity": self._node_item(node), "resolved_by": how, "hops": hops, "facts": facts,
               "result_count": len(facts), "top_similarity": sim}
        if alts:
            out["alternatives"] = alts
        if note:
            out["note"] = note
        if not facts:
            out["message"] = "This node has no edges within %d hop(s)." % hops
        return out

    async def search_graph(self, query: str, k: int = 5, hops: int = 1,
                           request_id: Optional[str] = None) -> Dict[str, Any]:
        q = D.check_text("query", query)
        k = D.check_int("k", k, 1, 10)
        hops = D.check_int("hops", hops, 1, 3)
        try:
            if await self._graph_is_empty():
                return self._graph_empty_result(query=q, entry_nodes=[], top_similarity=None)
        except GraphUnavailable as exc:
            return self._graph_unavailable(exc)

        async def by_vector() -> List[Dict[str, Any]]:  # meaning -> knowledge nodes; exact ids come from text
            vec = (await self._embed([q], request_id))[0]
            return await self.repo.match_kg_nodes(vec, TOOL_KINDS, k, self._graph_cutoff())

        text_rows, vec_rows = await asyncio.gather(self.repo.match_kg_nodes_text(q, None, k), by_vector(),
                                                   return_exceptions=True)
        for r in (text_rows, vec_rows):
            if isinstance(r, GraphUnavailable):
                return self._graph_unavailable(r)
        note = None
        if isinstance(vec_rows, EmbeddingError):
            note, vec_rows = "vector search skipped (%s); name/id matches only" % vec_rows, []
        for r in (text_rows, vec_rows):
            if isinstance(r, BaseException):
                raise r
        # Merge the two entry lists by reciprocal rank fusion; a node found both ways ranks first.
        entries: Dict[str, Dict[str, Any]] = {}
        text_rows = [r for r in text_rows if (_num(r.get("score")) or 0) >= self.s.graph_text_min_score]
        for how, rows in (("text", text_rows), ("vector", vec_rows)):
            for i, r in enumerate(rows):
                e = entries.setdefault(r["node_id"], {"row": r, "rrf": 0.0, "via": []})
                e["rrf"] += 1.0 / (60 + i)
                e["via"].append(how)
                if how == "vector":
                    e["sim"] = _num(r.get("similarity"))
        top = sorted(entries.values(), key=lambda e: -e["rrf"])[:k]
        out: Dict[str, Any] = {"query": q}
        if not top:
            out.update(entry_nodes=[], facts=[], result_count=0, top_similarity=None, no_relevant_results=True,
                       message="No graph node matches by name/id or above similarity %.2f. Do not cite anything "
                               "from this search." % self._graph_cutoff())
            if note:
                out["note"] = note
            return out
        seeds = [e["row"]["node_id"] for e in top]
        try:
            facts = await self._graph_facts(seeds, self._tool_walks(hops, 2 * k), set(seeds),
                                            max_items=min(4 * k, 30))
        except GraphUnavailable as exc:
            return self._graph_unavailable(exc)
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "57014":  # statement timeout
                raise ValueError("the %d-hop walk timed out; use hops=1 or a narrower query" % hops) from exc
            raise
        sims = [e["sim"] for e in top if e.get("sim") is not None]
        out["entry_nodes"] = [self._node_item(e["row"], sim=round(e["sim"], 3) if e.get("sim") is not None else None,
                                              match="+".join(e["via"])) for e in top]
        out["facts"] = facts
        out["result_count"] = len(top) + len(facts)
        out["top_similarity"] = max(sims) if sims else None
        if note:
            out["note"] = note
        return out

    # ── SQL tools ───────────────────────────────────────────────────────────────────────────────
    async def get_flag(self, flag_id: int) -> Dict[str, Any]:
        flag_id = D.check_int("flag_id", flag_id, 1, 2**62)
        row = await self.repo.flag(flag_id)
        if row is None:
            raise ValueError("no flag with flag_id=%d" % flag_id)
        out = self._flag_view(row)
        ev = row.get("event")
        if ev:
            out["event"] = compact({"id": "event:%s" % ev.get("event_id"), "event_type": ev.get("event_type"),
                                    "ts": ev.get("ts"), "battery_pct": ev.get("battery_pct")})
        a = row.get("assessment")
        if a and a.get("state") == "DONE":
            out["previous_explanation"] = compact({
                "id": "explanation:%s" % a.get("assessment_id"), "answer": snippet(a.get("answer"), 300),
                "recommended_action": a.get("recommended_action"), "model": a.get("model")})
        out["result_count"] = 1
        return out

    async def get_action(self, action_id: str) -> Dict[str, Any]:
        action_id = D.check_id("action_id", action_id)
        b = await self.repo.action_bundle(action_id)
        if b is None:
            raise ValueError("no action with action_id=%r" % action_id)
        out = self._action_view(b)
        out["result_count"] = 1
        return out

    async def get_system1_forecast(self, action_id: str) -> Dict[str, Any]:
        action_id = D.check_id("action_id", action_id)
        preds, b = await asyncio.gather(self.repo.s1_predictions(action_id, None), self.repo.action_bundle(action_id))
        latest: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for p in preds:  # newest first; keep one per (event_kind, question)
            latest.setdefault((p["event_kind"], p["question"]), p)
        actual: Dict[str, str] = {}
        out: Dict[str, Any] = {"action_id": action_id}
        validator: Dict[str, Any] = {}
        if b is not None:
            a = b["action"]
            ft = a.get("failure_type") or None
            try:
                mode = D.policy.failure_mode(ft) if D.policy else None
            except ValueError:
                mode = None
            out["actual"] = compact({"id": "action:%s" % action_id, "status": a.get("status"), "failure_type": ft,
                                     "failure_mode": mode, "anomaly_raised": bool(b.get("anomalies"))})
            actual = {"will_fail": "A" if a.get("status") == "FAILED" else "B",
                      "anomaly_expected": "A" if b.get("anomalies") else "B"}
            if mode:
                actual["failure_mode"] = mode
            validator = self._validator(b) or {}
        rows = []
        for (kind, q), p in sorted(latest.items()):
            item = compact({"id": "s1:%s" % p["prediction_id"], "event_kind": kind, "question": q,
                            "answer": p["answer"], "meaning": D.answer_meaning(q, p["answer"]),
                            "p": _num((p.get("probs") or {}).get(p["answer"])), "model": p.get("model")})
            if kind == "pre_action" and q in actual:
                item["actual"] = actual[q]
                item["correct"] = actual[q] == p["answer"]
            elif kind == "action" and q in validator:
                item["validator"] = validator[q]
                item["agrees_with_validator"] = validator[q] == p["answer"]
            rows.append(item)
        out["forecast"] = rows
        out["result_count"] = len(rows)
        if not rows:
            out["message"] = "System 1 has no stored predictions for this action."
        return out

    async def get_mission_summary(self, mission_id: str) -> Dict[str, Any]:
        mission_id = D.check_id("mission_id", mission_id)
        b = await self.repo.mission_bundle(mission_id)
        if b is None:
            raise ValueError("no mission with mission_id=%r" % mission_id)
        out = self._mission_view(b)
        out["result_count"] = 1
        return out

    async def get_reliability_stats(self, action_type: Optional[str] = None, location_id: Optional[str] = None,
                                    configuration_version: Optional[str] = None, battery_band: Optional[str] = None,
                                    group_by: Optional[str] = None) -> Dict[str, Any]:
        filters = {
            "action_type": D.check_enum("action_type", action_type, D.ACTION_TYPES) if action_type else None,
            "location_id": D.check_id("location_id", location_id) if location_id else None,
            "configuration_version": (D.check_enum("configuration_version", configuration_version, D.CONFIG_VERSIONS)
                                      if configuration_version else None),
            "battery_band": D.check_enum("battery_band", battery_band, D.BATTERY_BANDS) if battery_band else None,
        }
        if group_by is not None:
            D.check_enum("group_by", group_by, D.GROUP_BY)
        rows = await self.repo.reliability_counts(filters, group_by)
        base = ",".join("%s=%s" % (k, v) for k, v in filters.items() if v)
        out_rows = []
        for r in rows:
            sid = "stat:" + ",".join(x for x in (base, "%s=%s" % (group_by, r["grp"]) if group_by else "") if x)
            out_rows.append({"id": sid if sid != "stat:" else "stat:all", **({group_by: r["grp"]} if group_by else {}),
                             **D.rate_summary(int(r["n"]), int(r["n_success"]))})
        out = {"filters": {k: v for k, v in filters.items() if v}, "group_by": group_by, "rows": out_rows,
               "result_count": len(out_rows)}
        if not out_rows:
            out["message"] = "No actions match these filters."
        return out

    async def get_battery_risk(self, battery_pct: float, configuration_version: Optional[str] = None) -> Dict[str, Any]:
        if isinstance(battery_pct, bool) or not isinstance(battery_pct, (int, float)) or not 0 <= battery_pct <= 100:
            raise ValueError("battery_pct must be a number in [0, 100]; got %r" % (battery_pct,))
        cfg = (D.check_enum("configuration_version", configuration_version, D.CONFIG_VERSIONS)
               if configuration_version else None)
        band = D.battery_band(battery_pct)
        rows = await self.repo.reliability_counts({"configuration_version": cfg}, "battery_band")
        by_band = {r["grp"]: D.rate_summary(int(r["n"]), int(r["n_success"])) for r in rows}
        prefix = "stat:" + ("configuration_version=%s," % cfg if cfg else "")
        out: Dict[str, Any] = {
            "battery_pct": battery_pct, "band": band, "policy_status": D.battery_status(battery_pct),
            "policy_thresholds": {"critical_le": D.BATTERY_CRITICAL, "low_le": D.BATTERY_LOW,
                                  "watch_le": D.BATTERY_WATCH, "source": D.POLICY_SOURCE},
            "this_band": {"id": prefix + "battery_band=" + band, **by_band.get(band, D.rate_summary(0, 0))},
            "all_bands": [{"id": prefix + "battery_band=" + b, "band": b, "success_rate_pct": v["success_rate_pct"],
                           "n": v["n"]} for b, v in sorted(by_band.items(), key=lambda kv: D.BATTERY_BANDS.index(kv[0])
                                                            if kv[0] in D.BATTERY_BANDS else 99)],
            "result_count": len(by_band),
        }
        if cfg:
            out["configuration_version"] = cfg
        return out

    async def list_allowed_actions(self, category: str) -> Dict[str, Any]:
        if category == "all":
            return {"categories": {c: list(a) for c, a in D.ALLOWED_ACTIONS.items()},
                    "result_count": len(D.ALL_ACTIONS)}
        D.check_enum("category", category, D.CATEGORIES)
        acts = D.ALLOWED_ACTIONS[category]
        return {"category": category, "actions": [{"action": a, "meaning": m} for a, m in acts.items()],
                "result_count": len(acts),
                "note": "recommended_action must be exactly one of these names."}

    async def get_raw_event_log(self, mission_id: str, around_ts: Optional[str] = None,
                                limit: int = 20) -> Dict[str, Any]:
        mission_id = D.check_id("mission_id", mission_id)
        limit = D.check_int("limit", limit, 1, 50)
        if around_ts is not None:
            try:
                around_ts = _dt.datetime.fromisoformat(around_ts.replace("Z", "")).isoformat()
            except (ValueError, AttributeError):
                raise ValueError("around_ts must be an ISO timestamp like 2026-09-06T02:15:36; got %r" % around_ts)
        rows = await self.repo.raw_event_log(mission_id, around_ts, limit)
        events = [{"id": "log:%s" % r["log_id"], "event_ts": r.get("event_ts"), "event_type": r.get("event_type"),
                   "event_id": r.get("event_id"), "source": r.get("source"),
                   "verbatim": r.get("payload")} for r in rows]
        out: Dict[str, Any] = {"mission_id": mission_id, "events": events, "result_count": len(events)}
        if not events:
            out["message"] = "No raw SPOT events stored for this mission."
        return out

    # ── views ───────────────────────────────────────────────────────────────────────────────────
    def _flag_view(self, row: Dict[str, Any]) -> Dict[str, Any]:
        f = row["flag"]
        rows = _reason_rows(f)
        # `reasons` = the policy rules that fired (up to MAX_FIRED_RULES); `verdicts` = who decided (System 1 /
        # the rules), kept apart so they never push a fired rule out of the view.
        fired = [r for r in rows if r.get("rule") not in VERDICT_RULES]
        verdicts = [r for r in rows if r.get("rule") in VERDICT_RULES]
        out = compact({
            "id": "flag:%s" % f["flag_id"], "title": f.get("title"), "severity": f.get("severity"),
            "score": f.get("score"), "status": f.get("status"), "escalated": f.get("escalated"),
            "priority": f.get("priority"), "category": f.get("category"),
            "mission_id": f.get("mission_id"), "action_id": f.get("action_id"), "asset_id": f.get("asset_id"),
            "location_id": f.get("location_id"), "created_at": f.get("created_at"),
            "reasons": [compact({"rule": r.get("rule"), "detail": r.get("detail")}) for r in fired[:MAX_FIRED_RULES]],
            "verdicts": [compact({"rule": r.get("rule"), "detail": LAYA_UNAVAILABLE_DETAIL
                                  if r.get("rule") == "laya_unavailable" else snippet(r.get("detail"), 160)})
                         for r in verdicts],
        })
        acks = [compact({"id": "note:%s" % k.get("ack_id"), "action": k.get("action"),
                         "note": snippet(k.get("note"), 200), "operator": k.get("operator")})
                for k in (row.get("acks") or [])[:5]]
        if acks:
            out["operator_acks"] = acks
        return out

    def _validator(self, b: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Re-run the validator's own policy on this action, so System 2 sees the same verdict."""
        if D.policy is None:
            return None
        a, asset = b["action"], b.get("asset") or {}
        rd = (b.get("readings") or [None])[0]
        an = max(b.get("anomalies") or [], key=lambda x: SEV_RANK.get(x.get("severity"), 0), default=None)
        if a.get("battery_pct") is None or not asset.get("criticality"):
            return None
        ev = {"event_id": a.get("action_id"), "battery_pct": a["battery_pct"], "status": a.get("status"),
              "failure_type": a.get("failure_type") or None, "asset": {"criticality": asset["criticality"]},
              "anomaly": {"severity": an["severity"], "anomaly_type": an["anomaly_type"]} if an else None,
              "reading": ({"measurement_type": rd["measurement_type"], "value": float(rd["value"]),
                           "quality": rd.get("quality")} if rd and rd.get("value") is not None
                          and rd.get("measurement_type") in D.SAFE_BANDS else None)}
        try:
            lab = D.policy.label_action(ev)
        except Exception as exc:  # noqa: BLE001
            log.info("validator could not label action %s: %s", a.get("action_id"), exc)
            return None
        lab["suggested_action"] = D.suggested_action(lab["category"], lab["priority"], {
            "battery_pct": a.get("battery_pct"), "failure_type": ev["failure_type"],
            "severity": an["severity"] if an else None, "environment": (b.get("location") or {}).get("environment")})
        return lab

    def _action_view(self, b: Dict[str, Any]) -> Dict[str, Any]:
        a, s, l = b["action"], b.get("asset") or {}, b.get("location") or {}
        batt = _num(a.get("battery_pct"))
        readings = []
        for r in (b.get("readings") or [])[:3]:
            chk = D.band_check(r.get("measurement_type"), _num(r.get("value")), r.get("quality"), a.get("status"))
            readings.append(compact({"id": "reading:%s" % r.get("reading_id"), "measurement_type": r.get("measurement_type"),
                                     "value": _num(r.get("value")), "unit": r.get("unit"), "quality": r.get("quality"),
                                     "safe_band": [chk["safe_low"], chk["safe_high"]], "trust": chk["trust"],
                                     "assessment": chk["reason"]}))
        anomalies = [compact({"id": "anomaly:%s" % n.get("anomaly_id"), "anomaly_type": n.get("anomaly_type"),
                              "severity": n.get("severity"), "operator_review_required": n.get("operator_review_required"),
                              "description": snippet(n.get("description"), 160)})
                     for n in (b.get("anomalies") or [])[:3]]
        out = compact({
            "id": "action:%s" % a.get("action_id"), "mission_id": a.get("mission_id"), "sequence": a.get("sequence"),
            "timestamp": a.get("timestamp"), "action_type": a.get("action_type"), "status": a.get("status"),
            "failure_type": a.get("failure_type"), "battery_pct": batt,
            "battery_status": D.battery_status(batt) if batt is not None else None,
            "duration_sec": a.get("duration_sec"), "configuration_version": a.get("configuration_version"),
            "asset": compact({"id": "asset:%s" % s["asset_id"], "name": s.get("asset_name"), "type": s.get("asset_type"),
                              "criticality": s.get("criticality")}) if s.get("asset_id") else None,
            "location": compact({"id": "location:%s" % l["location_id"], "name": l.get("location_name"),
                                 "zone": l.get("zone_type"), "environment": l.get("environment")})
            if l.get("location_id") else None,
            "readings": readings, "anomalies": anomalies,
        })
        v = self._validator(b)
        if v:
            out["validator"] = {**v, "policy": D.POLICY_SOURCE}
        return out

    def _mission_view(self, b: Dict[str, Any]) -> Dict[str, Any]:
        m, k = b["mission"], b.get("kpis") or {}
        sev = {s: int((b.get("anomalies_by_severity") or {}).get(s, 0)) for s in ("LOW", "MEDIUM", "HIGH", "CRITICAL")}
        total, failed = int(b.get("actions_total") or 0), int(b.get("actions_failed") or 0)
        serious = sev["HIGH"] + sev["CRITICAL"]
        reported = m.get("mission_status")
        misleading = reported == "COMPLETED" and (failed > 0 or serious > 0)
        if k.get("true_health") is not None:
            health, src = _num(k["true_health"]), "mission_kpis"
        else:
            health, src = (round(100.0 * (total - failed) / total, 1) if total else None), "computed: action success rate"
        why = []
        if failed:
            why.append("%d of %d actions FAILED" % (failed, total))
        if serious:
            why.append("%d HIGH/CRITICAL anomalies" % serious)
        out = compact({
            "id": "mission:%s" % m.get("mission_id"), "name": m.get("mission_name"),
            "configuration_version": m.get("configuration_version"), "start_time": m.get("start_time"),
            "end_time": m.get("end_time"), "duration_min": m.get("duration_min"),
            "start_battery_pct": m.get("start_battery_pct"), "end_battery_pct": m.get("end_battery_pct"),
            "reported_status": reported, "actions_total": total, "actions_failed": failed,
            "failure_types": b.get("failure_types"), "anomalies_by_severity": sev,
            # asset_name / asset_criticality: the orchestrator's canonical mission event (System 1) needs both.
            "worst_anomaly": compact({"id": "anomaly:%s" % b["worst_anomaly"].get("anomaly_id"),
                                      **{x: b["worst_anomaly"].get(x) for x in ("anomaly_type", "severity", "asset_id",
                                                                                 "asset_name", "asset_criticality",
                                                                                 "action_id")}})
            if b.get("worst_anomaly") else None,
        })
        out["true_health"] = compact({"true_health_pct": health, "source": src, "status_misleading": misleading,
                                      "why": "; ".join(why) if misleading else None})
        if D.policy is not None and m.get("end_battery_pct") is not None:
            try:
                out["validator"] = D.policy.label_mission({
                    "anomalies_by_severity": sev, "reported_status": reported, "actions_failed": failed,
                    "end_battery_pct": m["end_battery_pct"]})
            except Exception:  # noqa: BLE001
                pass
        return out

    # ── Tier 1: one packet per flag ─────────────────────────────────────────────────────────────
    async def _load_flag(self, flag_id: int) -> Dict[str, Any]:
        row = await self.repo.flag(flag_id)
        if row is None:
            raise ValueError("no flag with flag_id=%d" % flag_id)
        f, ev = row["flag"], row.get("event") or {}
        action_id = f.get("action_id") or ev.get("action_id")
        mission_id = f.get("mission_id") or ev.get("mission_id")
        bundle = await self.repo.action_bundle(action_id) if action_id else None
        a = (bundle or {}).get("action") or {}
        mission_id = mission_id or a.get("mission_id")
        return {"row": row, "flag": f, "event": ev, "flag_id": flag_id, "action_id": action_id,
                "mission_id": mission_id, "bundle": bundle,
                "asset_id": f.get("asset_id") or a.get("asset_id") or ev.get("asset_id"),
                "location_id": f.get("location_id") or a.get("location_id") or ev.get("location_id")}

    @staticmethod
    def _self_ids(ctx: Dict[str, Any]) -> List[str]:
        """The flag's own documents, never returned as "similar": its incident, its own event doc and the incidents
        of its action's anomalies. Event docs are keyed by the System 1 triage event id (event:act-A0001 /
        event:mis-M001, amd/embed/ingest.py), i.e. flags.triage_event_id, not the replay's events.event_id."""
        ids = ["incident:flag-%s" % ctx["flag_id"]]
        tei = ctx["flag"].get("triage_event_id") or (
            "act-%s" % ctx["action_id"] if ctx.get("action_id") else
            "mis-%s" % ctx["mission_id"] if ctx.get("mission_id") else None)
        if tei:
            ids.append("event:%s" % tei)
        for n in ((ctx.get("bundle") or {}).get("anomalies") or []):
            ids.append("incident:%s" % n.get("anomaly_id"))
        return ids

    @staticmethod
    def _queries(ctx: Dict[str, Any]) -> Dict[str, str]:
        f, b = ctx["flag"], ctx.get("bundle") or {}
        a, s, l = b.get("action") or {}, b.get("asset") or {}, b.get("location") or {}
        an = (b.get("anomalies") or [{}])[0]
        what = an.get("anomaly_type") or a.get("failure_type") or f.get("category") or "issue"
        where = " ".join(x for x in (s.get("asset_type"), ctx.get("asset_id"), "at", l.get("location_name") or
                                     ctx.get("location_id"), "(%s)" % l["environment"] if l.get("environment") else "")
                         if x)
        rules = fired_rules(f)
        title = f.get("title") or ""
        incident = "%s on %s during %s: %s" % (what, where, a.get("action_type") or "mission", title)
        manual = ("How should a Spot operator handle %s during an Autowalk %s mission? %s"
                  % (str(what).replace("_", " ").lower(), str(a.get("action_type") or "inspection").replace("_", " ").lower(),
                     title))
        policy = "triage rule: %s %s %s" % (" ".join(rules), what, f.get("category") or "")
        return {"incident": incident, "history": incident, "manual": manual, "policy": policy}

    async def get_flag_context(self, flag_id: int, request_id: Optional[str] = None) -> Dict[str, Any]:
        flag_id = D.check_int("flag_id", flag_id, 1, 2**62)
        ctx = await self._load_flag(flag_id)
        f, b = ctx["flag"], ctx.get("bundle")
        missing: List[str] = []    # core context absent: the packet is insufficient
        gaps: List[str] = []       # something failed (lookup/search error, retrieval skipped, over budget)
        notes: List[str] = []      # informational, non-blocking: a search found nothing relevant, empty graph...
        is_action = bool(ctx["action_id"])

        # Phase A (SQL): mission, asset profile, System 1, exact policy docs.
        rules = fired_rules(f)     # policy rules only; the verdict rows have no policy:<rule> doc
        a = (b or {}).get("action") or {}
        coros = {
            "mission": self.repo.mission_bundle(ctx["mission_id"]) if ctx["mission_id"] else None,
            "asset": self.repo.asset_profile(ctx["asset_id"], flag_id) if ctx["asset_id"] else None,
            "s1": self.repo.s1_predictions(ctx["action_id"], ctx["mission_id"]) if (ctx["action_id"] or ctx["mission_id"]) else None,
            "policy_docs": self.repo.documents_by_id(["policy:%s" % r for r in rules[:4]]) if rules else None,
            "batt": (self.repo.reliability_counts({}, "battery_band") if a.get("battery_pct") is not None else None),
            "loc": (self.repo.reliability_counts({"action_type": a.get("action_type"),
                                                  "location_id": a.get("location_id")}, None)
                    if a.get("action_type") and a.get("location_id") else None),
        }
        keys = [k for k, c in coros.items() if c is not None]
        res = await asyncio.gather(*[coros[k] for k in keys], return_exceptions=True)
        got: Dict[str, Any] = {}
        for k, r in zip(keys, res):
            if isinstance(r, Exception):
                gaps.append("%s lookup failed (%s)" % (k, type(r).__name__))
            else:
                got[k] = r

        # Graph (no embedding needed: the seeds are exact node ids) runs while Phase B embeds.
        graph_task = asyncio.ensure_future(self._graph_section(ctx, got, rules))

        # Phase B (RAG): embed all queries in one batch, search in parallel.
        qs = self._queries(ctx)
        rag: Dict[str, Dict[str, Any]] = {}
        try:
            names = ["incident", "manual", "policy"] + (["history"] if ctx["asset_id"] else [])
            vecs = await self._embed([qs[n] for n in names], request_id, flag_id=flag_id, action_id=ctx["action_id"])
            vec = dict(zip(names, vecs))
            excl, xa = self._self_ids(ctx), ctx["action_id"]
            jobs = {
                "similar_incidents": self._retrieve(qs["incident"], ["incident"], k=3, hybrid=True, exclude=excl,
                                                    exclude_action=xa, vec=vec["incident"]),
                "manual": self._retrieve(qs["manual"], ["manual"], k=2, hybrid=False, vec=vec["manual"]),
                "policy": self._retrieve(qs["policy"], ["policy"], k=2, hybrid=True, vec=vec["policy"]),
            }
            if ctx["asset_id"]:
                jobs["asset_history"] = self._retrieve(qs["history"], ["event", "incident", "operator_note"], k=3,
                                                       hybrid=True, asset=ctx["asset_id"], exclude=excl,
                                                       exclude_action=xa, vec=vec["history"])
            out = await asyncio.gather(*jobs.values(), return_exceptions=True)
            for name, r in zip(jobs.keys(), out):
                if isinstance(r, Exception):
                    gaps.append("%s search failed (%s)" % (name, type(r).__name__))
                else:
                    rag[name] = r
        except EmbeddingError as exc:
            gaps.append("retrieval skipped: %s" % exc)
        graph_items, graph_gap, graph_note = await graph_task

        # Assemble: key facts first, question last.
        packet: Dict[str, Any] = {"packet_version": "1", "flag": self._flag_view(ctx["row"])}
        if is_action:
            if b is None:
                missing.append("action %s not found in actions table" % ctx["action_id"])
            else:
                packet["event"] = self._action_view(b)
                if not b.get("asset"):
                    missing.append("asset record for %s" % (ctx["asset_id"] or "this action"))
        elif ctx["mission_id"]:
            if got.get("mission") is None:
                missing.append("mission %s not found" % ctx["mission_id"])
            else:
                packet["event"] = {"mission": self._mission_view(got["mission"])}
        else:
            missing.append("flag has neither action_id nor mission_id")

        s1 = self._s1_compact(got.get("s1") or [], b)
        if s1:
            packet["system1"] = s1
        packet["facts"] = self._facts(ctx, got, packet.get("event"))
        if graph_items:
            packet["graph"] = graph_items
        if graph_gap:
            gaps.append(graph_gap)
        if graph_note:
            notes.append(graph_note)
        for name in ("similar_incidents", "asset_history", "manual", "policy"):
            items = (rag.get(name) or {}).get("results") or []
            if name == "policy":  # exact rule docs first
                exact = [self._doc_item({**d, "similarity": None}) for d in got.get("policy_docs") or []]
                seen = {e["id"] for e in exact}
                items = (exact + [i for i in items if i["id"] not in seen])[:3]
            if items:
                packet[name] = items
            elif name in rag:  # searched, nothing relevant (a failed or skipped search is already a gap)
                # A search that ran and found nothing relevant is a note, not a gap: System 2 treats any gap as
                # blocking (insufficient_context), and the SQL facts can still answer the question.
                notes.append("no relevant %s above similarity %.2f" % (name.replace("_", " "), self.s.min_similarity))
        if packet.get("graph"):
            # One id, one content: a node another section already cites (a manual page found by both the graph
            # walk and the manual search) keeps that section's item; System 2 rejects a repeated id with different
            # content (422 'conflicting content for duplicate id').
            cited = set(self._collect_ids({k: v for k, v in packet.items() if k != "graph"}))
            packet["graph"] = [g for g in packet["graph"] if g.get("id") not in cited]
            if not packet["graph"]:
                del packet["graph"]

        validator =(packet.get("event") or {}).get("validator") if is_action else \
            ((packet.get("event") or {}).get("mission") or {}).get("validator")
        category = (validator or {}).get("category") or f.get("category") or ("mission" if not is_action else None)
        if category not in D.ALLOWED_ACTIONS:
            category = "mission" if not is_action else None
        if category:
            packet["allowed_actions"] = dict(D.ALLOWED_ACTIONS[category])
            if is_action and validator:
                packet["suggested_action"] = validator.get("suggested_action")
            elif not is_action:
                packet["suggested_action"] = "REVIEW_MISSION" if (validator or {}).get("mission_misleading") == "A" \
                    else "MONITOR"
        else:
            packet["allowed_actions"] = {a_: m for c in D.ALLOWED_ACTIONS.values() for a_, m in c.items()}
            notes.append("category unknown; all actions allowed")

        packet["insufficient_context"] = bool(missing)
        if missing:
            packet["missing"] = missing
        if gaps:
            packet["gaps"] = gaps
        if notes:
            packet["notes"] = notes
        packet["question"] = self._question(ctx, packet)
        packet = self._fit_budget(packet)
        packet["citation_ids"] = self._collect_ids(packet)
        sims = [i.get("sim") for n in ("similar_incidents", "asset_history", "manual", "policy")
                for i in packet.get(n, []) if i.get("sim") is not None]
        packet["result_count"] = len(packet["citation_ids"])
        packet["top_similarity"] = max(sims) if sims else None
        packet["question"] = packet.pop("question")  # the question always comes last
        return packet

    @staticmethod
    def _graph_seeds(ctx: Dict[str, Any], got: Dict[str, Any],
                     rules: List[str]) -> Tuple[List[str], Set[str], List[str]]:
        """Seeds: the flag's action, asset, location, failure/anomaly types and triggered rules ('<kind>:<key>').
        Context: the seeds plus every node the flag/event sections already name (flag, mission, environment,
        action type, this action's anomalies), used to drop restated FK facts and to rank on-point facts first.
        Extra: the environment and action type, added to the fact node set (not walked from) so their statistics
        (fails_more_in) always show."""
        b = ctx.get("bundle") or {}
        a, loc = b.get("action") or {}, b.get("location") or {}
        seeds: List[str] = []

        def add(kind: str, key: Any, to: List[str] = seeds) -> None:
            nid = "%s:%s" % (kind, key)
            if key and nid not in to:
                to.append(nid)
        add("action", ctx["action_id"])
        add("asset", ctx["asset_id"])
        add("location", ctx["location_id"])
        add("failure_type", a.get("failure_type"))
        for n in (b.get("anomalies") or [])[:2]:
            add("anomaly_type", n.get("anomaly_type"))
        if not ctx["action_id"]:  # mission flag: its failure types and worst anomaly stand in for the action's
            m = got.get("mission") or {}
            add("mission", ctx["mission_id"])
            for ft, _ in sorted((m.get("failure_types") or {}).items(), key=lambda kv: -int(kv[1] or 0))[:2]:
                add("failure_type", ft)
            w = m.get("worst_anomaly") or {}
            add("anomaly_type", w.get("anomaly_type"))
            add("asset", w.get("asset_id"))
        for r in [r for r in rules if r not in VERDICT_RULES][:4]:   # verdict rows are not policy_rule nodes
            add("policy_rule", r)
        extra: List[str] = []
        add("environment", loc.get("environment"), extra)
        add("action_type", a.get("action_type"), extra)
        known: List[str] = []
        add("flag", ctx["flag_id"], known)
        add("mission", ctx["mission_id"], known)
        for n in b.get("anomalies") or []:
            add("anomaly", n.get("anomaly_id"), known)
        return seeds, set(seeds) | set(extra) | set(known), [x for x in extra if x not in seeds]

    async def _graph_section(self, ctx: Dict[str, Any], got: Dict[str, Any], rules: List[str]
                             ) -> Tuple[List[Dict[str, Any]], Optional[str], Optional[str]]:
        """The packet's `graph` section: 2-hop facts around the flag, <= graph_token_budget. Never raises.
        Returns (items, gap, note): a failure is a gap; an empty graph or no linked facts is only a note."""
        seeds, context, extra = self._graph_seeds(ctx, got, rules)
        if not seeds:
            return [], None, None
        try:
            if await self._graph_is_empty():
                return [], None, "%s: %s" % (NO_GRAPH_FACTS, KG_EMPTY)
            items = await self._graph_facts(seeds, PACKET_WALKS, context, extra=extra, drop_fk_within=context,
                                            token_budget=self.s.graph_token_budget, fill=False)
        except GraphUnavailable as exc:
            return [], "graph unavailable (%s)" % exc, None
        except Exception as exc:  # noqa: BLE001 - the graph is extra context; never break the packet
            log.warning("graph section failed for flag %s: %s", ctx["flag_id"], exc)
            return [], "graph lookup failed (%s)" % type(exc).__name__, None
        return items, None, (None if items else NO_GRAPH_FACTS)

    def _s1_compact(self, preds: List[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        latest: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for p in preds:
            latest.setdefault((p["event_kind"], p["question"]), p)
        a = (b or {}).get("action") or {}
        actual = {"will_fail": "A" if a.get("status") == "FAILED" else "B",
                  "anomaly_expected": "A" if (b or {}).get("anomalies") else "B"} if a else {}
        out = []
        for (kind, q), p in sorted(latest.items()):
            if kind not in ("pre_action", "action", "mission"):
                continue
            item = compact({"id": "s1:%s" % p["prediction_id"], "when": kind, "q": q, "answer": p["answer"],
                            "meaning": D.answer_meaning(q, p["answer"]),
                            "p": _num((p.get("probs") or {}).get(p["answer"]))})
            if kind == "pre_action" and q in actual:
                item["correct"] = actual[q] == p["answer"]
            out.append(item)
        return out[:6]

    def _facts(self, ctx: Dict[str, Any], got: Dict[str, Any], event: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        facts: List[str] = []
        b = ctx.get("bundle") or {}
        a = b.get("action") or {}
        ev = event or {}
        v = ev.get("validator")
        if v:
            facts.append("Validator (%s): priority %s, category %s, escalate=%s, reading_trust=%s, suggested action %s."
                         % (D.POLICY_SOURCE, v["priority"], v["category"], "yes" if v["escalate"] == "A" else "no",
                            {"A": "trusted", "B": "untrusted", "C": "no reading"}[v["reading_trust"]],
                            v.get("suggested_action")))
        for r in ev.get("readings") or []:
            lo, hi = r.get("safe_band", [None, None])
            band = "%s..%s" % (_fmt(lo) if lo is not None else "-inf", _fmt(hi) if hi is not None else "+inf")
            facts.append("Reading %s %s %s (quality %s) vs safe band %s: %s."
                         % (r.get("measurement_type"), _fmt(r.get("value")), r.get("unit") or "", r.get("quality"),
                            band, r.get("assessment")))
        batt = _num(a.get("battery_pct"))
        if batt is not None:
            band = D.battery_band(batt)
            stat = next((x for x in got.get("batt") or [] if x["grp"] == band), None)
            line = "Battery %s%% at this action: policy status %s (critical<=%d, low<=%d, watch<=%d)." % (
                _fmt(batt), D.battery_status(batt), D.BATTERY_CRITICAL, D.BATTERY_LOW, D.BATTERY_WATCH)
            if stat:
                rs = D.rate_summary(int(stat["n"]), int(stat["n_success"]))
                line += " Kit action success in band %s: %s%% (%d/%d)%s." % (
                    band, _fmt(rs["success_rate_pct"]), rs["n_success"], rs["n"],
                    ", small sample" if rs.get("warning") else "")
            facts.append(line)
        loc = (got.get("loc") or [None])[0]
        if loc:
            rs = D.rate_summary(int(loc["n"]), int(loc["n_success"]))
            env = (b.get("location") or {}).get("environment")
            facts.append("%s at %s%s: %s%% success (%d/%d)%s." % (
                a.get("action_type"), a.get("location_id"), " (%s)" % env if env else "",
                _fmt(rs["success_rate_pct"]), rs["n_success"], rs["n"], ", small sample" if rs.get("warning") else ""))
        prof = got.get("asset")
        if prof:
            # repo.asset_profile is bounded to robot time before this flag's event, without its own action/anomaly.
            s = prof["asset"]
            by_type = prof.get("anomalies_by_type") or {}
            line = "Asset %s (%s, criticality %s): %d earlier anomalies on record%s." % (
                s.get("asset_id"), s.get("asset_type"), s.get("criticality"), int(prof.get("anomaly_count") or 0),
                " (" + ", ".join("%s x%s" % (t, c) for t, c in sorted(by_type.items(), key=lambda kv: -int(kv[1]))[:3]) + ")"
                if by_type else "")
            rr = prof.get("recent_readings") or []
            mt = ((ev.get("readings") or [{}])[0]).get("measurement_type") or (rr[-1]["measurement_type"] if rr else None)
            vals = [_num(r.get("value")) for r in rr if r.get("measurement_type") == mt and r.get("quality") == "GOOD"]
            vals = [x for x in vals if x is not None]
            if len(vals) >= 2:
                trend = "rising" if vals[-1] > vals[0] else "falling" if vals[-1] < vals[0] else "flat"
                line += " Previous %d GOOD %s readings: %s..%s, %s (latest %s)." % (
                    len(vals), mt, _fmt(min(vals)), _fmt(max(vals)), trend, _fmt(vals[-1]))
            facts.append(line)
            for p in (prof.get("prior_flags") or [])[:2]:
                facts.append("Prior flag flag:%s on this asset: \"%s\" (%s, %s)%s." % (
                    p.get("flag_id"), snippet(p.get("title"), 80), p.get("severity"), p.get("status"),
                    "; operator note: " + snippet(p.get("notes"), 120) if p.get("notes") else ""))
        m = got.get("mission")
        if m and not ctx["action_id"]:  # mission flag: battery and health facts
            mv = self._mission_view(m)
            end = _num(mv.get("end_battery_pct"))
            if end is not None:
                facts.append("Mission %s ended at %s%% battery (started %s%%): policy status %s." % (
                    ctx["mission_id"], _fmt(end), mv.get("start_battery_pct"), D.battery_status(end)))
            th = mv.get("true_health") or {}
            if th.get("true_health_pct") is not None:
                facts.append("Mission %s: reported %s, true health %s%% (%s)%s." % (
                    ctx["mission_id"], mv.get("reported_status"), _fmt(th["true_health_pct"]), th.get("source"),
                    "; status misleading: " + th["why"] if th.get("status_misleading") and th.get("why") else ""))
        if m and ctx["action_id"]:
            mv = self._mission_view(m)
            th = mv.get("true_health") or {}
            facts.append("Mission %s (%s) reported %s; %d/%d actions failed, %d HIGH/CRITICAL anomalies%s." % (
                ctx["mission_id"], mv.get("configuration_version"), mv.get("reported_status"), mv["actions_failed"],
                mv["actions_total"], mv["anomalies_by_severity"]["HIGH"] + mv["anomalies_by_severity"]["CRITICAL"],
                ": reported status hides problems" if th.get("status_misleading") else ""))
        return [{"id": "F%d" % (i + 1), "fact": t} for i, t in enumerate(facts)]

    @staticmethod
    def _question(ctx: Dict[str, Any], packet: Dict[str, Any]) -> str:
        f = ctx["flag"]
        ev = packet.get("event") or {}
        if ctx["action_id"]:
            s = ev.get("asset") or {}
            l = ev.get("location") or {}
            subject = "%s (%s)" % (s.get("name"), ctx["asset_id"]) if s.get("name") else (ctx["asset_id"] or "the asset")
            at = " at %s" % l.get("name") if l.get("name") else ""
            return ("Flag %s \"%s\": what is wrong with %s%s, can the evidence be trusted, and which allowed action "
                    "should the operator take?" % (ctx["flag_id"], f.get("title"), subject, at))
        return ("Flag %s \"%s\": does mission %s's reported status hide problems, and which allowed action should "
                "the operator take?" % (ctx["flag_id"], f.get("title"), ctx["mission_id"]))

    def _fit_budget(self, packet: Dict[str, Any]) -> Dict[str, Any]:
        limit = budget = self.s.packet_token_budget

        def size() -> int:  # citation_ids and bookkeeping are added after trimming, so measure the final shape
            ids = self._collect_ids(packet)
            return est_tokens({**packet, "citation_ids": ids, "result_count": len(ids), "top_similarity": 0.12345})

        if size() > budget:     # trimming: leave room for the note that says what was trimmed
            budget = max(0, limit - TRIM_NOTE_TOKENS)

        def trim(name: str, floor: int) -> None:  # pop from the tail: every section is ordered best first
            while size() > budget and len(packet.get(name, [])) > floor:
                packet[name].pop()
            if name in packet and not packet[name]:
                packet.pop(name)

        before = {n: len(packet.get(n) or []) for n in TRIMMED_SECTIONS}
        # Trim order: least decisive evidence first. flag, event, facts and the question are never trimmed.
        # RAG tails go first, but the last asset_history / manual item outlives shorter snippets and the graph tail
        # down to 4 facts: with a populated graph, dropping them first left 0 of 58 packets with any asset history.
        for name, floor in (("manual", 1), ("asset_history", 1), ("policy", 1), ("similar_incidents", 2),
                            ("graph", 6)):
            trim(name, floor)
        for n in (180, 120):
            if size() <= budget:
                break
            for name in ("similar_incidents", "asset_history", "manual", "policy"):
                for it in packet.get(name, []):
                    it["text"] = snippet(it.get("text"), n)
        trim("graph", 4)
        for name, floor in (("asset_history", 0), ("manual", 0), ("similar_incidents", 1)):
            trim(name, floor)
        if size() > budget and isinstance(packet.get("allowed_actions"), dict):
            packet["allowed_actions"] = list(packet["allowed_actions"])
        trim("graph", 3)
        if size() > budget:  # shorten explanatory prose; the rule names, answers and numbers stay
            f = packet.get("flag") or {}
            for r in f.get("reasons") or []:
                if r.get("detail"):
                    r["detail"] = snippet(r["detail"], 90)
            for r in f.get("verdicts") or []:
                if r.get("detail"):
                    r["detail"] = snippet(r["detail"], 60)
            for k in f.get("operator_acks") or []:
                if k.get("note"):
                    k["note"] = snippet(k["note"], 80)
            for it in packet.get("system1") or []:
                if it.get("meaning"):
                    it["meaning"] = snippet(it["meaning"], 60)
        for name, floor in (("policy", 0), ("system1", 3), ("similar_incidents", 0), ("graph", 2)):
            trim(name, floor)
        if size() > budget:
            for it in packet.get("system1") or []:
                it.pop("meaning", None)
        if size() > budget:
            packet.setdefault("gaps", []).append("packet is over the %d-token budget even after trimming" % limit)
        cut = ["%s %d->%d" % (n, before[n], len(packet.get(n) or [])) for n in TRIMMED_SECTIONS
               if len(packet.get(n) or []) < before[n]]
        if cut:     # informational: the evidence was found, the budget left it out
            packet.setdefault("notes", []).append("trimmed to the %d-token packet budget: %s" % (limit, ", ".join(cut)))
        return packet

    @staticmethod
    def _collect_ids(obj: Any) -> List[str]:
        ids: List[str] = []

        def walk(x: Any) -> None:
            if isinstance(x, dict):
                if isinstance(x.get("id"), str) and x["id"] not in ids:
                    ids.append(x["id"])
                for k, v in x.items():
                    if k != "citation_ids":
                        walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)

        walk(obj)
        return ids
