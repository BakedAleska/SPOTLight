"""Schemas for the AI Assistance chat (docs/chat-contract.md §3, §4). The SSE events reuse Message/Conversation,
so a streamed `done` and a reloaded thread serialize identically."""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

Mode = Literal["fast", "thinking"]


class Message(BaseModel):
    message_id: str
    conversation_id: str
    seq: int
    role: Literal["user", "assistant"]
    content: str = ""
    thinking: Optional[str] = None
    thinking_ms: Optional[int] = None
    model: Optional[str] = None
    mode: Optional[Mode] = None
    status: Literal["complete", "error", "stopped"] = "complete"
    sources: List[Dict[str, Any]] = Field(default_factory=list, description="Source[] shown to the model (§3).")
    citations: List[str] = Field(default_factory=list, description="Source ids the answer cited, first use first.")
    grounding: Optional[Dict[str, Any]] = Field(None, description="Grounding (§3); assistant messages only.")
    usage: Optional[Dict[str, Any]] = Field(None, description="{prompt_tokens, output_tokens, thoughts_tokens}")
    latency_ms: Optional[int] = None
    error: Optional[str] = None
    error_code: Optional[str] = Field(None, description="chat.2: the §4 error code ('rate_limited', "
                                                        "'model_unavailable', ...) when status is 'error', else null.")
    created_at: Optional[dt.datetime] = None


class MissionRef(BaseModel):
    """chat.3: the mission a conversation is about."""
    mission_id: str
    mission_name: Optional[str] = None
    mission_status: Optional[str] = Field(None, description="The mission's reported status (COMPLETED | FAILED).")


class Conversation(BaseModel):
    conversation_id: str
    title: str
    title_source: Literal["default", "auto", "model", "user"] = "default"
    model: str
    mode: Mode = "fast"
    pinned: bool = False
    created_at: dt.datetime
    updated_at: dt.datetime
    message_count: int = 0
    mission_id: Optional[str] = Field(None, description="chat.3: the mission this chat is about (set at creation, "
                                                        "never changed), or null.")
    mission: Optional[MissionRef] = None


class ConversationSummary(Conversation):
    last_message_preview: Optional[str] = None


class ConversationPage(BaseModel):
    items: List[ConversationSummary]
    total: int


class ConversationDetail(BaseModel):
    conversation: Conversation
    messages: List[Message]


class ConversationCreateIn(BaseModel):
    title: Optional[str] = Field(None, max_length=120, description="Default 'New chat'. A given title counts as "
                                                                   "the operator's (never replaced by a model title).")
    model: Optional[str] = Field(None, description="A /chat/models id. Default CHAT_DEFAULT_MODEL.")
    mode: Optional[Mode] = None
    mission_id: Optional[str] = Field(None, max_length=40, description="chat.3: make this a chat about one mission. "
                                                                       "Case-insensitive ('m007' is stored as "
                                                                       "'M007'); an unknown id is 422 "
                                                                       "unknown_mission.")


class ConversationPatchIn(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=120, description="Sets title_source=user.")
    pinned: Optional[bool] = None
    model: Optional[str] = None
    mode: Optional[Mode] = None

    @field_validator("title")
    @classmethod
    def _title(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = " ".join(v.split())
        if not v:
            raise ValueError("title must not be blank")
        return v


class MessageIn(BaseModel):
    content: str = Field("", max_length=8000, description="1-8000 chars; ignored when regenerate is true.")
    model: Optional[str] = Field(None, description="A /chat/models id. Default: the conversation's model.")
    mode: Optional[Mode] = None
    edit_message_id: Optional[str] = Field(None, description="A user message of this conversation: it and every "
                                                             "later message are replaced by `content`.")
    regenerate: bool = Field(False, description="Drop a trailing assistant message and answer the last user "
                                                "message again.")


class ModelInfo(BaseModel):
    id: str
    label: str
    description: str
    family: Literal["gemini", "gemma"]
    thinking_summaries: bool


class ModeInfo(BaseModel):
    id: Mode
    label: str
    description: str


class ModelsOut(BaseModel):
    configured: bool = Field(description="GEMINI_API_KEY is set on the backend.")
    default_model: str
    default_mode: Mode = "fast"
    models: List[ModelInfo]
    modes: List[ModeInfo]


class Suggestion(BaseModel):
    title: str
    prompt: str


class SuggestionsOut(BaseModel):
    suggestions: List[Suggestion]


class MissionSummary(BaseModel):
    """chat.3 (§9): one row of the mission picker (ChatRepo.list_missions)."""
    mission_id: str
    mission_name: Optional[str] = None
    scenario: Optional[str] = None
    configuration_version: Optional[str] = None
    start_time: Optional[dt.datetime] = None
    duration_min: Optional[int] = None
    start_battery_pct: Optional[int] = None
    end_battery_pct: Optional[int] = None
    mission_status: Optional[str] = None
    actions: int = 0
    failed: int = 0
    anomalies: int = 0
    serious_anomalies: int = 0
    open_flags: int = 0
    hidden_failure: bool = Field(False, description="Reported COMPLETED with at least one failed action.")
    chats: int = Field(0, description="Conversations about this mission with at least one message.")


class MissionsOut(BaseModel):
    items: List[MissionSummary]


def message_out(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return None if row is None else Message.model_validate(row).model_dump(mode="json")


def conversation_out(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return None if row is None else Conversation.model_validate(row).model_dump(mode="json")
