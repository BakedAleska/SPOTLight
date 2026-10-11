from __future__ import annotations

import psycopg
from fastapi import APIRouter, Depends, HTTPException

from ..db.pool import DatabaseUnavailable
from ..deps import Services, get_services, require_admin
from ..schemas.misc import ReplayStart, ReplayStatus, ReplayStep
from ..services.replay import ReplayConflict

# Audit H3: every /replay route needs X-Admin-Token (ADMIN_TOKEN).
router = APIRouter(prefix="/replay", tags=["replay"], dependencies=[Depends(require_admin)])

CONFLICT = "a replay is already running; POST /api/v1/replay/stop first"


@router.post("/start", response_model=ReplayStatus)
async def start(body: ReplayStart = ReplayStart(), svc: Services = Depends(get_services)) -> dict:
    """Start replaying the kit's replay.ndjson (KIT_DIR) through the ingest pipeline. Gaps follow the robot's
    timestamps divided by `speed`, capped at `max_gap_s`. 409 if one is running. REPLAY_MODE=background: an asyncio
    task streams it. REPLAY_MODE=stepped (serverless): the run is stored in `replay_runs` and advances only when a
    client calls `POST /replay/step`."""
    if await svc.replay.is_running():
        raise HTTPException(409, CONFLICT)
    reset_error = None
    if body.reset:
        try:
            await svc.repo.reset_demo(clear_predictions=False)
        except (DatabaseUnavailable, psycopg.Error) as exc:  # the replay still runs (verdicts without storage)
            reset_error = str(exc)
        svc.ref.reset()
    try:
        out = await svc.replay.start(speed=body.speed, max_gap_s=body.max_gap_s, missions=body.missions,
                                     start_at=body.start_at, limit=body.limit, triage_mode=body.triage_mode)
    except ReplayConflict:
        raise HTTPException(409, CONFLICT)
    if reset_error:
        out["reset_error"] = reset_error
    return out


@router.post("/step", response_model=ReplayStatus)
async def step(body: ReplayStep = ReplayStep(), svc: Services = Depends(get_services)) -> dict:
    """REPLAY_MODE=stepped only: ingest the running run's due events within `budget_s`, then return its status with
    `step: {ingested, elapsed_ms, busy, done}` and `next_due_in_s`. Call it in a loop until `state != "running"`.
    `busy: true` = another step holds the run (call again). Safe to repeat: progress is saved after every event."""
    if svc.settings.replay_mode != "stepped":
        raise HTTPException(409, "replay_mode is 'background': the replay runs by itself (set REPLAY_MODE=stepped "
                                 "to drive it with /replay/step)")
    return await svc.replay.step(run_id=body.run_id, budget_s=body.budget_s,  # type: ignore[union-attr]
                                 max_events=body.max_events)


@router.post("/stop", response_model=ReplayStatus)
async def stop(svc: Services = Depends(get_services)) -> dict:
    """Stop the running replay (no-op if none). Events already ingested stay."""
    return await svc.replay.stop()


@router.get("/status", response_model=ReplayStatus)
async def status(svc: Services = Depends(get_services)) -> dict:
    """Progress of the current or last replay: sent/total, escalations, flags created, Laya fallbacks, last event
    (stepped mode adds `cursor` and `next_due_in_s`)."""
    return await svc.replay.get_status()
