from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Priority = Literal["P1", "P2", "P3", "P4"]
Severity = Literal["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"]
TriageMode = Literal["serial", "parallel"]


class Loose(BaseModel):
    """A response model that keeps any extra fields the database or a service adds."""
    model_config = ConfigDict(extra="allow")


class ErrorResponse(BaseModel):
    error: str = Field(description="Machine-readable error code, e.g. `database_unavailable`.")
    detail: str = Field(description="Human-readable explanation.")
    request_id: Optional[str] = None


class ServiceHealth(Loose):
    status: Literal["up", "down", "degraded", "unset"]
    url: Optional[str] = None
    error: Optional[str] = None
    breaker: Optional[str] = Field(None, description="`open` while the backend is skipping a failing service.")
    model_status: Optional[Literal["up", "down", "unknown", "stub"]] = Field(
        None, description="System 2 only: does the model behind it answer? `down` makes `status` `degraded`.")


class Health(BaseModel):
    ok: bool = Field(description="True when the API process is serving. Dependencies may still be down.")
    service: str = "backend"
    version: str
    triage_mode: TriageMode
    database: Optional[ServiceHealth] = None
    logs_database: Optional[ServiceHealth] = None
    system1: Optional[ServiceHealth] = None
    embed: Optional[ServiceHealth] = None
    mcp: Optional[ServiceHealth] = None
    system2: Optional[ServiceHealth] = None
    orchestrator: Optional[ServiceHealth] = None
    replay: Dict[str, Any] = {}
    kit: Dict[str, Any] = {}


class Page(BaseModel):
    total: int
    limit: int
    offset: int


JSON = Dict[str, Any]
JSONList = List[Dict[str, Any]]
