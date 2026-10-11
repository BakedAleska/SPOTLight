"""/api/v1/chat: the AI Assistance tab (docs/chat-contract.md §4). Conversations CRUD, the model catalog, starter
prompts, and `POST /conversations/{id}/messages`, which answers as a `text/event-stream`.

No admin token: these are operator reads like /flags. Ids are uuids; a malformed id is a 404, never a 500.
Errors before a stream starts are JSON `{"error", "detail", "request_id"}`; after it starts they are an `error` event.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import JSONResponse, StreamingResponse

from ..deps import Services, current_request_id, get_services
from ..schemas.chat import (Conversation, ConversationCreateIn, ConversationDetail, ConversationPage,
                            ConversationPatchIn, MessageIn, ModelsOut, SuggestionsOut)
from ..schemas.common import ErrorResponse
from ..services.chat import ChatError
from ..services.chat.service import norm_uuid

router = APIRouter(prefix="/chat", tags=["chat"])
ERRORS = {404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 503: {"model": ErrorResponse}}
SSE_EVENTS = """`text/event-stream`, events in this order (`: keep-alive` comments every 15 s):
`meta` {conversation_id, request_id, model, mode, user_message} · `sources` {sources, retrieval} ·
`thinking` {delta} (thinking mode, 0..n) · `delta` {delta} (1..n) · `done` {message, conversation} ·
`title` {conversation_id, title} (optional, first exchange) · or `error` {code, detail, retry_after_s?, message}."""


def _err(status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "detail": detail,
                                                     "request_id": current_request_id()})


def _not_found(conversation_id: str) -> JSONResponse:
    return _err(404, "not_found", "no conversation with id %s" % conversation_id)


@router.get("/models", response_model=ModelsOut)
async def models(svc: Services = Depends(get_services)) -> dict:
    """The model picker: CHAT_MODELS in order, with labels and descriptions, and the two modes. `configured` is
    false when GEMINI_API_KEY is unset (sending a message then answers 503 `gemini_unset`)."""
    return svc.chat.models_payload()


@router.get("/suggestions", response_model=SuggestionsOut)
async def suggestions(svc: Services = Depends(get_services)) -> dict:
    """Four starter prompts built from live facts (real mission, asset, waypoint and flag ids). No LLM call."""
    return {"suggestions": await svc.chat.suggestions()}


@router.get("/conversations", response_model=ConversationPage)
async def list_conversations(q: Optional[str] = Query(None, max_length=200, description="Matches the title or any "
                                                                                       "message, case-insensitive."),
                             limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                             svc: Services = Depends(get_services)) -> dict:
    """The history panel: pinned first, then most recently updated."""
    items, total = await svc.chat.repo.list_conversations(q=(q or "").strip() or None, limit=limit, offset=offset)
    return {"items": items, "total": total}


@router.post("/conversations", status_code=201, response_model=Conversation, responses=ERRORS)
async def create_conversation(body: Optional[ConversationCreateIn] = None, svc: Services = Depends(get_services)):
    """A new, empty chat. Defaults: title "New chat", CHAT_DEFAULT_MODEL, fast mode."""
    body = body or ConversationCreateIn()
    model = body.model or svc.chat.default_model
    try:
        svc.chat.check_model(model)
    except ChatError as exc:
        return _err(exc.status, exc.code, exc.detail)
    title = " ".join((body.title or "").split())
    conv = await svc.chat.repo.create_conversation(title=title or "New chat", model=model, mode=body.mode or "fast")
    if title:   # an operator-given title is never replaced by an auto or model title
        conv = await svc.chat.repo.update_conversation(conv["conversation_id"], title_source="user") or conv
    return conv


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail, responses=ERRORS)
async def get_conversation(conversation_id: str, svc: Services = Depends(get_services)):
    """The conversation and every message, oldest first."""
    conv = await svc.chat.repo.get_conversation(conversation_id) if norm_uuid(conversation_id) else None
    if conv is None:
        return _not_found(conversation_id)
    return {"conversation": conv, "messages": await svc.chat.repo.list_messages(conv["conversation_id"])}


@router.patch("/conversations/{conversation_id}", response_model=Conversation, responses=ERRORS)
async def patch_conversation(conversation_id: str, body: ConversationPatchIn, svc: Services = Depends(get_services)):
    """Rename (sets title_source=user, so a model title never replaces it), pin/unpin, or change model/mode."""
    if not norm_uuid(conversation_id):
        return _not_found(conversation_id)
    try:
        svc.chat.check_model(body.model)
    except ChatError as exc:
        return _err(exc.status, exc.code, exc.detail)
    fields = body.model_dump(exclude_none=True)
    if "title" in fields:
        fields["title_source"] = "user"
    repo = svc.chat.repo
    if fields:
        conv = await repo.update_conversation(conversation_id, **fields)
    else:
        conv = await repo.get_conversation(conversation_id)
    return conv if conv is not None else _not_found(conversation_id)


@router.delete("/conversations/{conversation_id}", status_code=204, responses=ERRORS)
async def delete_conversation(conversation_id: str, svc: Services = Depends(get_services)):
    """Deletes the conversation and its messages."""
    if not norm_uuid(conversation_id) or not await svc.chat.repo.delete_conversation(conversation_id):
        return _not_found(conversation_id)
    return Response(status_code=204)


@router.post("/conversations/{conversation_id}/messages", responses={
    200: {"content": {"text/event-stream": {}}, "description": SSE_EVENTS},
    404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse},
    503: {"model": ErrorResponse}}, description=(
    "Ask (or `regenerate`, or `edit_message_id`): the backend retrieves from documents, the knowledge graph and SQL "
    "digests, makes ONE Gemini call, and streams the answer with `[id]` citations, then a deterministic grounding "
    "check. Disconnecting stops the generation; the partial answer is stored as `stopped`.\n\n" + SSE_EVENTS))
async def post_message(conversation_id: str, body: MessageIn, svc: Services = Depends(get_services)):
    try:
        turn = await svc.chat.prepare_turn(conversation_id, content=body.content, model=body.model, mode=body.mode,
                                           edit_message_id=body.edit_message_id, regenerate=body.regenerate,
                                           request_id=current_request_id())
    except ChatError as exc:
        return _err(exc.status, exc.code, exc.detail)
    return StreamingResponse(svc.chat.stream(turn), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})
