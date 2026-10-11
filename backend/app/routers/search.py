from __future__ import annotations

import time
from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from ..deps import Services, current_request_id, get_services
from ..schemas.misc import SearchOut
from ..services.embed import vec_literal
from ..services.http import ServiceUnavailable

router = APIRouter(prefix="/search", tags=["search"])
KINDS = ("event", "incident", "operator_note", "explanation", "manual", "policy")


def _snippet(text: Optional[str], n: int = 280) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1].rsplit(" ", 1)[0] + "…"


@router.get("", response_model=SearchOut, response_model_exclude_none=True)
async def search(q: str = Query(..., min_length=1, max_length=500, description="Natural language or an exact id "
                                                                                "like HX-204."),
                 kinds: Optional[List[str]] = Query(None, description="Any of: " + ", ".join(KINDS)),
                 asset_id: Optional[str] = None, location_id: Optional[str] = None,
                 mission_id: Optional[str] = None, k: int = Query(8, ge=1, le=50),
                 svc: Services = Depends(get_services)) -> dict:
    """RAG search over `documents`: the query is embedded with EMBED_QUERY_PREFIX and passed to
    `match_documents_hybrid` (vector + full text, reciprocal rank fusion), with optional kind/asset/location/
    mission filters. If the embedding service is down or slower than SEARCH_EMBED_TIMEOUT_S (default 4 s; the CPU
    embedder needs ~2.3 s): Postgres full-text on `content_tsv` (`mode: fulltext`)."""
    t0 = time.perf_counter()
    # '?asset_id=' arrives as '' (or blanks): no filter (match_documents_hybrid would compare against '' and match
    # nothing)
    asset_id, location_id, mission_id = ((v or "").strip() or None for v in (asset_id, location_id, mission_id))
    kinds = [x for k_ in (kinds or []) for x in k_.split(",") if x] or None
    if kinds:
        bad = [x for x in kinds if x not in KINDS]
        if bad:
            raise ValueError("unknown kinds %s (allowed: %s)" % (bad, ", ".join(KINDS)))
    mode, reason = "hybrid", None
    try:
        vec = await svc.embed.embed_query(q, request_id=current_request_id(),
                                          timeout_s=svc.settings.search_embed_timeout_s)
        rows = await svc.repo.search_hybrid(vec_literal(vec), q, kinds, asset_id, location_id, mission_id, k)
    except ServiceUnavailable as exc:
        mode, reason = "fulltext", "embedding service unavailable: %s" % exc.reason
        rows = await svc.repo.search_fulltext(q, kinds, asset_id, location_id, mission_id, k)
    hits = [{"doc_id": r["doc_id"], "kind": r["kind"], "title": r.get("title"), "snippet": _snippet(r.get("content")),
             "score": r.get("score"), "similarity": r.get("similarity"), "text_match": r.get("text_match"),
             "asset_id": r.get("asset_id"), "location_id": r.get("location_id"), "mission_id": r.get("mission_id"),
             "action_id": r.get("action_id"), "ts": r.get("ts"), "metadata": r.get("metadata")} for r in rows]
    return {"query": q, "mode": mode, "hits": hits, "fallback_reason": reason,
            "latency_ms": int((time.perf_counter() - t0) * 1000)}
