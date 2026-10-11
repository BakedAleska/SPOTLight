from __future__ import annotations

from typing import Optional

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query

from ..db.pool import DatabaseUnavailable
from ..deps import Services, get_services, require_admin
from ..schemas.misc import JobsOut, ModelCallStats, ResetIn, ResetOut

# Audit H3: every /ops route needs X-Admin-Token (ADMIN_TOKEN).
router = APIRouter(prefix="/ops", tags=["ops"], dependencies=[Depends(require_admin)])


@router.get("/model-calls", response_model=ModelCallStats)
async def model_calls(since_minutes: int = Query(60 * 24, ge=1, le=60 * 24 * 30),
                      system: Optional[str] = Query(None, description="system1 | system2 | embed"),
                      recent: int = Query(0, ge=0, le=200, description="Also return the N most recent calls."),
                      svc: Services = Depends(get_services)) -> dict:
    """Latency and throughput evidence from `app.model_calls`: per system/model/endpoint call counts, errors,
    p50/p95/mean latency (ms) and tokens/s, plus System 1's routing (events triaged vs escalated)."""
    stats = await svc.repo.model_call_stats(since_minutes, system)
    rows = await svc.repo.recent_model_calls(recent) if recent else []
    try:
        routing = await svc.repo.triage_counts()
    except DatabaseUnavailable:
        routing = {}
    if routing.get("triaged"):
        routing["escalation_rate_pct"] = round(100.0 * routing["escalated"] / routing["triaged"], 1)
    routing["since_start"] = dict(svc.pipeline.stats)
    return {"since_minutes": since_minutes, "stats": stats, "recent": rows, "routing": routing}


@router.get("/jobs", response_model=JobsOut)
async def jobs(limit: int = Query(20, ge=1, le=200), svc: Services = Depends(get_services)) -> dict:
    """Recent `app.job_runs` (seed, ingest, replay, ...) and the live replay status."""
    try:
        rows = await svc.repo.jobs(limit)
    except DatabaseUnavailable:
        rows = []
    try:
        replay = await svc.replay.get_status()
    except (DatabaseUnavailable, psycopg.Error):   # stepped mode reads replay_runs
        replay = svc.replay.status()
    return {"replay": replay, "jobs": rows}


@router.post("/reset-demo", response_model=ResetOut)
async def reset_demo(body: ResetIn, svc: Services = Depends(get_services)) -> dict:
    """Reset the live demo: stop the replay, delete events, flags, assessments, acknowledgements and
    demo-generated documents (operator notes, explanations). Kit tables and the append-only raw/app logs are
    kept. Requires `{"confirm": true}`."""
    if not body.confirm:
        raise HTTPException(400, 'send {"confirm": true} to reset the demo')
    status = await svc.replay.stop()
    deleted = await svc.repo.reset_demo(body.clear_predictions)
    svc.ref.reset()
    for c in (svc.system1, svc.embed, svc.mcp, svc.system2):
        c.breaker.reset()
    job = await svc.sink.job_start("reset-demo", {"clear_predictions": body.clear_predictions})
    await svc.sink.job_finish(job, "done", deleted)
    return {"deleted": deleted, "replay": status}
