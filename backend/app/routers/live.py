"""Live simulation API (docs/backend-services-contract.md "Live simulation").

Public reads (the web console polls them every 2 s): routes, runs, captures, the run's live KG rows.
Admin (X-Admin-Token): queue/cancel a run (the console's Start button, via the web server) and the live runner's
per-stop calls. The runner never holds database credentials; this API is its only way into Neon.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel, Field

from ..deps import Services, current_request_id, get_services, require_admin

router = APIRouter(prefix="/live", tags=["live"])
admin = [Depends(require_admin)]


class RunCreate(BaseModel):
    route_id: str = Field(..., pattern=r"^R[1-9]$")
    requested_by: Optional[str] = Field(None, max_length=100)


class Claim(BaseModel):
    runner: Optional[str] = Field(None, max_length=100, description="A label for the runner host; never an address.")


class StopResult(BaseModel):
    timestamp: Optional[str] = Field(None, description="Result time (ISO). The runner sent Laya the start response's "
                                                       "action_event with this timestamp.")
    laya: Optional[Dict[str, Any]] = Field(None, description="{status: ok, answers, model, latency_ms, device, "
                                                             "fine_tuned} or {status: unavailable, error}")


class StopAnalysis(BaseModel):
    photo: Optional[Dict[str, Any]] = None
    vision: Optional[Dict[str, Any]] = None
    vision_error: Optional[str] = None
    system2_request: Optional[Dict[str, Any]] = None
    system2: Optional[Dict[str, Any]] = None
    system2_error: Optional[str] = None
    system2_ms: Optional[int] = None
    image_status: Optional[str] = None
    timings: Optional[Dict[str, Any]] = None


class EmbedItem(BaseModel):
    key: str = Field(..., max_length=200)
    text: str
    vector: List[float]


class Embeddings(BaseModel):
    model: Optional[str] = None
    items: List[EmbedItem] = Field(default_factory=list, max_length=20)


class Finish(BaseModel):
    status: Literal["done", "error"] = "done"
    error: Optional[str] = Field(None, max_length=500)


def _rid() -> str:
    return current_request_id() or "r-live"


# ── public ───────────────────────────────────────────────────────────────────────────────────────
@router.get("/routes")
async def routes(svc: Services = Depends(get_services)) -> dict:
    """The five routes (8 stops each) with each stop's kit row, photo and the rules' planned verdict."""
    return svc.live.route_list()


@router.get("/runs")
async def runs(limit: int = Query(20, ge=1, le=100), svc: Services = Depends(get_services)) -> dict:
    """Live runs, newest first, with red/green counts."""
    return {"items": await svc.live.runs(limit)}


@router.get("/runs/{run_id}")
async def run(run_id: int, svc: Services = Depends(get_services)) -> dict:
    """One run with its route and every capture (one per stop reached)."""
    return await svc.live.run_detail(run_id)


@router.get("/captures")
async def captures(run_id: int, after: Optional[str] = Query(None, description="Cursor from the last call."),
                   svc: Services = Depends(get_services)) -> dict:
    """Captures of a run updated after `after` (all of them without it), plus the run's status and a new cursor."""
    return await svc.live.captures(run_id, after)


@router.get("/preview")
async def preview(route_id: Optional[str] = Query(None, pattern=r"^R[1-9]$"),
                  svc: Services = Depends(get_services)) -> dict:
    """Every stop's canonical action event and rules verdict, without writing anything (the runner's Laya dry run)."""
    return svc.live.preview(route_id)


@router.get("/kg")
async def kg(run_id: int, svc: Services = Depends(get_services)) -> dict:
    """Knowledge-graph nodes and edges this run added (props.source = 'live'), plus graph totals."""
    return await svc.live.kg(run_id)


# ── admin: operator ──────────────────────────────────────────────────────────────────────────────
@router.post("/runs", dependencies=admin)
async def create_run(body: RunCreate, svc: Services = Depends(get_services)) -> dict:
    """Queue a run of `route_id` (status pending). 409 while another run is pending or running."""
    return {"run": await svc.live.create_run(body.route_id, body.requested_by)}


@router.post("/runs/{run_id}/cancel", dependencies=admin)
async def cancel_run(run_id: int, svc: Services = Depends(get_services)) -> dict:
    return {"run": await svc.live.cancel_run(run_id)}


# ── admin: the live runner ───────────────────────────────────────────────────────────────────────
@router.post("/claim", dependencies=admin)
async def claim(body: Claim = Body(Claim()), svc: Services = Depends(get_services)) -> dict:
    """Claim the oldest pending run (-> running) and log MISSION_STARTED. `run: null` when nothing is queued."""
    return await svc.live.claim(body.runner, _rid())


@router.post("/runs/{run_id}/stops/{stop_index}/start", dependencies=admin)
async def stop_start(run_id: int, stop_index: int, svc: Services = Depends(get_services)) -> dict:
    """SPOT reached the stop: ACTION_STARTED is logged and the capture appears on the map. Returns the canonical
    action event to send to Laya (set its `timestamp` to the result time first)."""
    return await svc.live.stop_start(run_id, stop_index, _rid())


@router.post("/runs/{run_id}/stops/{stop_index}/result", dependencies=admin)
async def stop_result(run_id: int, stop_index: int, body: StopResult, svc: Services = Depends(get_services)) -> dict:
    """ACTION_COMPLETED/FAILED through the 7-step ingest with the runner's Laya answers (rules run here, parallel
    mode) -> red/green, flag, event document, live KG rows. Returns the System 2 request and texts to embed."""
    return await svc.live.stop_result(run_id, stop_index, body.timestamp, body.laya, _rid())


@router.post("/runs/{run_id}/stops/{stop_index}/analysis", dependencies=admin)
async def stop_analysis(run_id: int, stop_index: int, body: StopAnalysis,
                        svc: Services = Depends(get_services)) -> dict:
    """The stop's photo, vision readings and Qwen (System 2) output; a template, labelled as such, when Qwen did
    not answer. Writes the assessment (red), the explanation document and its KG node."""
    return await svc.live.stop_analysis(run_id, stop_index, body.model_dump(), _rid())


@router.post("/runs/{run_id}/embeddings", dependencies=admin)
async def embeddings(run_id: int, body: Embeddings, svc: Services = Depends(get_services)) -> dict:
    """Vectors for texts this API asked the runner to embed (`doc:<doc_id>` or `node:<node_id>`); each is stored
    only if the row's stored text is exactly `text`."""
    return await svc.live.embeddings(run_id, body.model_dump())


@router.post("/runs/{run_id}/finish", dependencies=admin)
async def finish(run_id: int, body: Finish = Body(Finish()), svc: Services = Depends(get_services)) -> dict:
    """End the run: MISSION_COMPLETED is logged (stored, not triaged) and the run gets its final counts."""
    return {"run": await svc.live.finish(run_id, body.status, body.error, _rid())}
