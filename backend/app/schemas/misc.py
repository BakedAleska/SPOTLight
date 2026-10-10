"""Schemas for missions, System 1, catalog (assets/locations), insights, search, replay and ops."""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from .common import JSON, Loose, TriageMode


# ── missions ────────────────────────────────────────────────────────────────────────────────────
class MissionSummary(Loose):
    mission_id: str
    mission_status: Optional[str] = Field(None, description="Reported status (COMPLETED | FAILED).")
    true_status: Optional[str] = Field(None, description="NEEDS_REVIEW when the reported status hides problems.")
    true_health_pct: Optional[float] = None
    status_misleading: bool = False
    why: Optional[str] = None
    actions_total: int = 0
    actions_failed: int = 0
    serious_anomalies: int = 0


class MissionList(BaseModel):
    items: List[MissionSummary]
    totals: JSON


class MissionDetail(BaseModel):
    mission: MissionSummary
    actions: List[JSON]
    flags: List[JSON]


class Timeline(BaseModel):
    mission_id: str
    items: List[JSON] = Field(description="One entry per action (kit) merged with its ingested events and flags, "
                                          "in robot order.")
    events_ingested: int


class Debrief(Loose):
    mission_id: str
    summary: str
    recommendations: List[str]
    facts: JSON
    source: Literal["system2", "template"]
    model: Optional[str] = None
    fallback_reason: Optional[str] = None


# ── System 1 ────────────────────────────────────────────────────────────────────────────────────
class PredictItemIn(BaseModel):
    id: Optional[str] = Field(None, description="Defaults to the canonical event's event_id.")
    kind: Optional[Literal["pre_action", "action", "mission"]] = None
    event: Optional[Dict[str, Any]] = Field(None, description="A canonical event dict (triage/events.py).")
    action_id: Optional[str] = Field(None, description="Instead of `event`: build it from the kit action.")
    questions: Optional[List[str]] = None


class PredictIn(BaseModel):
    items: List[PredictItemIn] = Field(min_length=1, max_length=64)


class PredictOut(Loose):
    source: Literal["system1", "rules_fallback"]
    model: Optional[str] = None
    items: List[JSON]
    error: Optional[str] = None


class ForecastItem(Loose):
    action_id: str
    sequence: Optional[int] = None
    action_type: Optional[str] = None
    location_id: Optional[str] = None
    battery_pct: Optional[int] = None
    p_fail: Optional[float] = None
    failure_mode: Optional[str] = None
    p_anomaly: Optional[float] = None
    source: Literal["system1_stored", "system1_live", "baseline"]
    baseline_fail_pct: Optional[float] = Field(None, description="Empirical failure rate for this action type at "
                                                                 "this waypoint (kit history).")
    actual: Optional[JSON] = Field(None, description="The real outcome, when known (for the 'was Laya right' view).")


class Forecast(BaseModel):
    scope: JSON
    items: List[ForecastItem]
    system1: str = Field(description="ok | laya_unavailable")


# ── catalog ─────────────────────────────────────────────────────────────────────────────────────
class AssetOut(Loose):
    asset_id: str
    asset_name: str
    criticality: Optional[str] = None


class LocationOut(Loose):
    location_id: str
    location_name: str
    environment: Optional[str] = None
    success_rate_pct: Optional[float] = None


# ── insights ────────────────────────────────────────────────────────────────────────────────────
class InsightCard(Loose):
    key: str
    label: str
    value: Optional[float]
    unit: str
    pitch: str = Field(description="One sentence for the dashboard / pitch, built from the live numbers.")
    detail: JSON


class Overview(BaseModel):
    insights: List[InsightCard]
    stored_insights: List[JSON] = Field([], description="Rows of the `insights` table (filled by other jobs).")
    generated_at: dt.datetime


class ReliabilityRow(Loose):
    group: Optional[str]
    n: int
    n_success: int
    success_rate_pct: Optional[float]
    ci95_pct: List[Optional[float]]
    warning: Optional[str] = None


class Reliability(BaseModel):
    group_by: Optional[str]
    filters: JSON
    rows: List[ReliabilityRow]


# ── search ──────────────────────────────────────────────────────────────────────────────────────
class SearchHit(Loose):
    doc_id: str
    kind: str
    title: Optional[str]
    snippet: str
    score: Optional[float] = None
    similarity: Optional[float] = None
    asset_id: Optional[str] = None
    location_id: Optional[str] = None
    mission_id: Optional[str] = None
    action_id: Optional[str] = None


class SearchOut(BaseModel):
    query: str
    mode: Literal["hybrid", "fulltext"] = Field(description="hybrid = embedding + full text (RRF); fulltext = "
                                                            "embedding service down, Postgres full-text only.")
    hits: List[SearchHit]
    fallback_reason: Optional[str] = None
    latency_ms: int


# ── replay ──────────────────────────────────────────────────────────────────────────────────────
class ReplayStart(BaseModel):
    speed: Optional[float] = Field(None, ge=0, description="Robot seconds per wall second (default REPLAY_SPEED, "
                                                           "60). 0 = as fast as possible.")
    max_gap_s: Optional[float] = Field(None, ge=0, description="Cap on any single wait (default REPLAY_MAX_GAP_S).")
    missions: Optional[List[str]] = Field(None, description="Only these missions, e.g. ['M001','M014'].")
    start_at: Optional[str] = Field(None, description="Start at this event_id.")
    limit: Optional[int] = Field(None, ge=1, le=10000)
    triage_mode: Optional[TriageMode] = None
    reset: bool = Field(False, description="Run reset-demo first (clears live events/flags).")


class ReplayStep(BaseModel):
    run_id: Optional[int] = Field(None, description="Only step this run (default: the running one).")
    budget_s: Optional[float] = Field(None, ge=0.5, le=60, description="Time budget of this step "
                                                                       "(default REPLAY_STEP_BUDGET_S, 10).")
    max_events: int = Field(100, ge=1, le=500, description="At most this many events in this step.")


class ReplayStatus(Loose):
    state: Literal["idle", "running", "stopping", "stopped", "finished", "error"]
    mode: Optional[Literal["background", "stepped"]] = None


# ── ops ─────────────────────────────────────────────────────────────────────────────────────────
class ModelCallStats(BaseModel):
    since_minutes: int
    stats: List[JSON] = Field(description="Per system/model/endpoint: calls, errors, p50/p95/mean latency (ms), "
                                          "tokens/s, token totals.")
    recent: List[JSON] = []
    routing: JSON = Field({}, description="How many events System 1 triaged vs escalated (System 2's workload).")


class JobsOut(BaseModel):
    replay: JSON
    jobs: List[JSON]


class ResetIn(BaseModel):
    confirm: bool = Field(False, description="Must be true.")
    clear_predictions: bool = Field(False, description="Also delete system1_predictions.")


class ResetOut(BaseModel):
    deleted: Dict[str, int]
    replay: JSON
