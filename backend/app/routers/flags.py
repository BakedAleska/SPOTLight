from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict, Optional, Tuple

import anyio
from fastapi import APIRouter, Body, Depends, Path, Query
from fastapi.responses import StreamingResponse

from ..deps import Services, current_request_id, get_services
from ..schemas.common import ErrorResponse
from ..schemas.flags import AckIn, AckOut, ContextPacket, ExplainIn, Explanation, FlagDetail, FlagList
from ..services.chat.service import sse
from ..services.flags import KEEPALIVE
from ..services.http import short_error

router = APIRouter(prefix="/flags", tags=["flags"])
FlagId = Path(..., ge=1, description="flags.flag_id")
log = logging.getLogger("backend.flags")
LOG_DRAIN_S = 2.0      # = main.SERVERLESS_LOG_DRAIN_S: finish this stream's log/index writes before it ends
EXPLAIN_SSE_EVENTS = """`text/event-stream`, events in this order (`: keep-alive` comments every 15 s):
`start` {flag_id, request_id} · `stage` {stage: cached | context | system2 | storing} (0..n) ·
`draft` {call, fields: {evidence: [{claim, ids}], answer, recommended_action, confidence}, reasoning} (0..n, Qwen's
unchecked partial JSON relayed from System 2; show the newest `call` only; never an answer) ·
`done` {explanation} (exactly the JSON `POST /flags/{id}/explain` returns for the same call) · or
`error` {error, detail}."""


def explanation_json(out: Dict[str, Any]) -> Dict[str, Any]:
    """The explanation as `POST /explain` serializes it (response_model=Explanation, exclude_none)."""
    return Explanation.model_validate(out).model_dump(mode="json", by_alias=True, exclude_none=True)


async def _explain_sse(events: AsyncIterator[Tuple[str, Any]], svc: Services) -> AsyncIterator[str]:
    try:
        async for event, data in events:
            if event == KEEPALIVE[0]:
                yield ": keep-alive\n\n"
                continue
            if event == "done":
                try:
                    data = {"explanation": explanation_json(data["explanation"])}
                except Exception as exc:  # noqa: BLE001 - same as a 500 from /explain, but as the last event
                    log.exception("explanation could not be serialized")
                    yield sse("error", {"error": "internal_error", "detail": short_error(exc)})
                    break
            yield sse(event, data)
    finally:
        with anyio.CancelScope(shield=True):     # a client disconnect must not interrupt the cleanup
            await events.aclose()      # type: ignore[attr-defined]
            if svc.settings.serverless:
                # main.py drains the log sink when the response *starts*; this stream's model-call row and the
                # explanation's RAG indexing are written later, so wait for them before the instance may freeze.
                await svc.sink.drain(timeout_s=LOG_DRAIN_S)


@router.get("", response_model=FlagList)
async def list_flags(status: Optional[str] = Query(None, description="OPEN | ACKNOWLEDGED | RESOLVED"),
                     escalated: Optional[bool] = Query(True, description="Default: escalated flags only."),
                     severity: Optional[str] = Query(None, description="LOW | MEDIUM | HIGH | CRITICAL"),
                     mission_id: Optional[str] = None, asset_id: Optional[str] = None,
                     location_id: Optional[str] = None,
                     limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                     svc: Services = Depends(get_services)) -> dict:
    """The operator queue: open flags first, then by score (priority x confidence), newest first."""
    rows, total = await svc.repo.list_flags(status=status, escalated=escalated, severity=severity,
                                            mission_id=mission_id, asset_id=asset_id, location_id=location_id,
                                            limit=limit, offset=offset)
    return {"items": rows, "page": {"total": total, "limit": limit, "offset": offset}}


@router.get("/{flag_id}", response_model=FlagDetail)
async def get_flag(flag_id: int = FlagId, svc: Services = Depends(get_services)) -> dict:
    """One flag with its event (enriched payload + triage summary), operator acks, System 2 assessment and
    Laya's stored answers."""
    row = await svc.flags.load(flag_id)
    f = row["flag"]
    preds = await svc.repo.predictions(f.get("action_id"), f.get("mission_id"))
    return {"flag": f, "event": row.get("event"), "acks": row.get("acks") or [], "assessment": row.get("assessment"),
            "system1": preds}


@router.get("/{flag_id}/context", response_model=ContextPacket, response_model_exclude_none=True)
async def context(flag_id: int = FlagId, svc: Services = Depends(get_services)) -> dict:
    """The context packet System 2 reads, from the MCP server's `get_flag_context` (RAG over pgvector + SQL).
    If the MCP server is down, a SQL-built minimal packet (`source: sql_fallback`) with the flag, the event,
    Laya's answers, waypoint/battery reliability and the asset's history."""
    return await svc.flags.context(flag_id, current_request_id())


@router.post("/{flag_id}/explain", response_model=Explanation, response_model_exclude_none=True)
async def explain(flag_id: int = FlagId,
                  refresh: bool = Query(False, description="Regenerate even if an assessment is stored."),
                  body: Optional[ExplainIn] = Body(None, description="Optional operator image (orchestrator only)."),
                  svc: Services = Depends(get_services)) -> dict:
    """Orbit-style explanation (question / answer / reasoning / recommended action) from System 2
    (`SYSTEM2_URL` /v1/assess, with the context packet). Stored in `assessments`; later calls return it unless
    `refresh=true`. If System 2 is unset or down: a templated explanation from the flag's reasons
    (`source: template`). With `ORCHESTRATOR_URL` set, the LangGraph orchestrator is asked first (graph trace in
    `orchestrator`); an optional `image_b64` is forwarded to it and bypasses the cache."""
    image = body.model_dump(exclude_none=True) if body is not None and body.image_b64 else None
    return await svc.flags.explain(flag_id, current_request_id(), refresh=refresh, image=image)


@router.post("/{flag_id}/explain/stream", responses={
    200: {"content": {"text/event-stream": {}}, "description": EXPLAIN_SSE_EVENTS},
    404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}, 504: {"model": ErrorResponse}}, description=(
    "`POST /flags/{id}/explain` as server-sent events (contract \"Streaming Qwen drafts\"): the same cache rules, "
    "REQUEST_DEADLINE_S, fallbacks, storage and indexing, plus Qwen's partial answer while System 2 generates it "
    "(`/v1/assess/stream`; an older System 2 without it is asked `/v1/assess`, without drafts). Cached rows and the "
    "orchestrator path send no drafts. A missing flag (404) or the deadline passing while the flag is read (504) "
    "are JSON errors before the stream starts.\n\n" + EXPLAIN_SSE_EVENTS))
async def explain_stream(flag_id: int = FlagId,
                         refresh: bool = Query(False, description="Regenerate even if an assessment is stored."),
                         svc: Services = Depends(get_services)):
    events = await svc.flags.explain_stream(flag_id, current_request_id(), refresh=refresh)
    return StreamingResponse(_explain_sse(events, svc), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


@router.get("/{flag_id}/explain", response_model=Explanation, response_model_exclude_none=True)
async def get_explanation(flag_id: int = FlagId, svc: Services = Depends(get_services)) -> dict:
    """Same as POST without `refresh`: the stored explanation, generated on first request."""
    return await svc.flags.explain(flag_id, current_request_id(), refresh=False)


@router.post("/{flag_id}/ack", response_model=AckOut)
async def ack(body: AckIn, flag_id: int = FlagId, svc: Services = Depends(get_services)) -> dict:
    """Record an operator action in `acknowledgements` and update the flag status (ACKNOWLEDGE -> ACKNOWLEDGED,
    RESOLVE -> RESOLVED, ESCALATE -> OPEN). A note is embedded and upserted as RAG document `note:<ack_id>`
    (kind `operator_note`) when the embedding service is up, so System 2 learns from operators."""
    return await svc.flags.ack(flag_id, body.action, body.note, body.operator, current_request_id())
