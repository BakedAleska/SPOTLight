from __future__ import annotations

import base64
import binascii
import datetime as dt
import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .common import JSON, Loose, Page


class Flag(Loose):
    flag_id: int
    event_id: Optional[str]
    mission_id: Optional[str]
    action_id: Optional[str]
    location_id: Optional[str]
    asset_id: Optional[str]
    score: float
    severity: str
    title: str
    reasons: List[JSON] = Field(description="Both verdicts first (laya_verdict | laya_unavailable, rules_verdict), "
                                            "then the fired policy rules [{rule, detail, weight}].")
    escalated: bool
    status: str
    created_at: dt.datetime
    priority: Optional[str] = None
    category: Optional[str] = None


class FlagList(BaseModel):
    items: List[Flag]
    page: Page


class FlagDetail(BaseModel):
    flag: Flag
    event: Optional[JSON] = None
    acks: List[JSON] = []
    assessment: Optional[JSON] = None
    system1: List[JSON] = Field([], description="Laya's stored answers for this event (system1_predictions).")


class ContextPacket(Loose):
    """The context packet System 2 reads. From the MCP server (`source: mcp`) or SQL-only (`sql_fallback`)."""
    source: Literal["mcp", "sql_fallback"]
    question: Optional[str] = None
    gaps: Optional[List[str]] = Field(None, description="Lookups that failed. System 2 treats any gap as "
                                                        "insufficient context.")
    notes: Optional[List[str]] = Field(None, description="Non-blocking notices, e.g. 'MCP unavailable ...: SQL-only "
                                                         "packet' (System 2 ignores this key).")


class Explanation(Loose):
    """Orbit-style explanation: question / answer / reasoning / recommended action."""
    question: Optional[str]
    answer: Optional[str]
    reasoning: Optional[str]
    recommended_action: Optional[str]
    severity_assessment: Optional[str] = None
    confidence: Optional[float] = None
    model: Optional[str] = None
    source: Literal["system2", "system2_template", "template", "system1"]
    cached: bool = False
    fallback_reason: Optional[str] = None
    context_source: Optional[str] = None
    assessment_id: Optional[int] = None
    orchestrator: Optional[Dict[str, Any]] = Field(
        None, description="ORCHESTRATOR_URL set: {route, route_reasons, trace, system1, vision, elapsed_ms} when the "
                          "orchestrator answered, else {error} when the existing path ran instead.")


ImageKind = Literal["thermal", "gauge", "spectrogram", "acoustic_imager", "orbit_screenshot", "visual"]


class ExplainIn(BaseModel):
    """Optional body for POST /flags/{id}/explain: an operator inspection image, forwarded to the orchestrator."""
    image_b64: Optional[str] = Field(None, max_length=12_000_000, description="JPEG/PNG base64, or a data: URL.")
    image_kind: Optional[ImageKind] = None

    @field_validator("image_b64")
    @classmethod
    def _b64(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        text = re.sub(r"^data:image/[A-Za-z0-9.+-]+;base64,", "", v.strip(), count=1, flags=re.IGNORECASE)
        try:
            if not base64.b64decode("".join(text.split()), validate=True):
                raise ValueError
        except (binascii.Error, ValueError):
            raise ValueError("image_b64 is not valid base64") from None
        return v.strip()


class AskIn(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    flag_id: Optional[int] = Field(None, ge=1)
    max_steps: Optional[int] = Field(None, ge=1, le=4)


class AckIn(BaseModel):
    action: Literal["ACKNOWLEDGE", "RESOLVE", "ESCALATE", "NOTE"] = "ACKNOWLEDGE"
    note: Optional[str] = Field(None, max_length=4000, description="Free text. Indexed for RAG as an operator_note. "
                                                                    "Blank or whitespace-only means no note (null).")
    operator: str = Field("operator", min_length=1, max_length=100)

    @field_validator("note")
    @classmethod
    def _blank_note_is_none(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if v and v.strip() else None


class AckOut(BaseModel):
    ack: Dict[str, Any]
    flag_status: str
    rag_indexed: bool = Field(description="True if the note was embedded and upserted as documents.note:<ack_id>.")
    doc_id: Optional[str] = Field(None, description="documents.note:<ack_id> when the note document was stored, "
                                                    "also without a vector while the embedder is down "
                                                    "(rag_indexed false; full-text search finds it).")
    rag_error: Optional[str] = None
