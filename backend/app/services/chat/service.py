"""One chat turn, end to end (docs/chat-contract.md §4): validate -> lock -> store the user message -> retrieve ->
ONE Gemini call streamed as SSE -> grounding -> store the answer -> optional model title.

Concurrency and stop:
- One generation per conversation (`409 conversation_busy`). The lock is in-process (the backend runs one uvicorn
  worker) and has a lease (CHAT_TIMEOUT_S + budget + 60 s), so a stream that never started can't wedge a chat.
- The turn runs in a producer task that pushes SSE frames into a queue; the response generator relays them and
  sends `: keep-alive` every 15 s of silence. When the client disconnects, Starlette cancels the generator; its
  `finally` cancels the producer (which closes the Gemini stream) and stores the partial answer as `stopped`.
  That storing runs in its own task awaited under a shielded cancel scope, so a cancellation cannot interrupt it.
- Every outcome after the user message is stored leaves an assistant message: complete, error or stopped. An error
  message also stores the §4 code in `error_code` (`chat.2`), so a reloaded thread renders the same error card.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Set

import anyio

from ...db.chat import ConversationNotFound
from ...schemas.chat import conversation_out, message_out
from ..http import short_error
from .catalog import MODE_INFO, MODES, catalog, family, show_thoughts
from .gemini import GeminiClient, GeminiError
from .grounding import check
from .prompt import auto_title, build_body, clean_title, title_body, user_turn
from .retrieval import Prior, Retrieval, RetrievalFailed, Retriever, prior_answer

log = logging.getLogger("backend.chat")

KEEPALIVE_S = 15.0
TITLE_TIMEOUT_S = 6.0


class ChatError(Exception):
    """A pre-stream error: the router answers {"error": code, "detail", "request_id"} with `status`."""

    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


def sse(event: str, data: Any) -> str:
    return "event: %s\ndata: %s\n\n" % (event, json.dumps(data, default=str, ensure_ascii=False,
                                                          separators=(",", ":")))


def norm_uuid(v: Any) -> Optional[str]:
    try:
        return str(uuid.UUID(str(v).strip()))
    except (ValueError, TypeError, AttributeError):
        return None


def _safe_message(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    try:
        return message_out(row)
    except Exception as exc:  # noqa: BLE001 - a malformed row must not hide the error event
        log.warning("could not serialize message: %s", short_error(exc))
        return None


@dataclass
class Turn:
    conversation_id: str
    conversation: Dict[str, Any]
    model: str
    mode: str
    user_message: Dict[str, Any]
    history: List[Dict[str, Any]]          # stored messages before the user message, oldest first
    previous: Optional[str]                # the previous user message (follow-up retrieval)
    request_id: Optional[str]
    wants_title: bool
    prior: Optional[Prior] = None          # the previous answer: its entities and cited sources carry over
    t0: float = field(default_factory=time.perf_counter)

    @property
    def question(self) -> str:
        return self.user_message.get("content") or ""


@dataclass
class TurnState:
    text: List[str] = field(default_factory=list)
    thinking: List[str] = field(default_factory=list)
    retrieval: Optional[Retrieval] = None
    usage: Optional[Dict[str, Any]] = None
    t_call: Optional[float] = None
    t_end: Optional[float] = None
    first_text_at: Optional[float] = None
    final: Optional[asyncio.Future] = None   # the task storing the assistant message (created once)


class ChatService:
    def __init__(self, settings, repo, embed, http, sink):
        self.settings = settings
        self.repo = repo
        self.sink = sink
        self.gemini = GeminiClient(settings.gemini_api_key, settings.gemini_base_url, http, sink,
                                   settings.chat_timeout_s)
        self.retriever = Retriever(repo, embed, settings.search_embed_timeout_s, settings.chat_retrieval_budget_s)
        self.models: List[str] = list(dict.fromkeys(m for m in settings.chat_models if m))
        self.keepalive_s = KEEPALIVE_S
        self.title_timeout_s = TITLE_TIMEOUT_S
        self.lock_ttl_s = settings.chat_timeout_s + settings.chat_retrieval_budget_s + 60.0
        self._busy: Dict[str, float] = {}
        self._tasks: Set[asyncio.Future] = set()

    # ── catalog ─────────────────────────────────────────────────────────────────────────────────
    @property
    def default_model(self) -> str:
        d = self.settings.chat_default_model
        return d if d in self.models or not self.models else self.models[0]

    def models_payload(self) -> Dict[str, Any]:
        return {"configured": self.gemini.configured, "default_model": self.default_model, "default_mode": "fast",
                "models": catalog(self.models), "modes": MODE_INFO}

    def check_model(self, model: Optional[str]) -> None:
        if model is not None and model not in self.models:
            raise ChatError(422, "invalid_request", "unknown model %r (allowed: %s)" % (model, ", ".join(self.models)))

    # ── suggestions (no LLM) ────────────────────────────────────────────────────────────────────
    async def suggestions(self) -> List[Dict[str, str]]:
        """Four starter prompts built from live facts with real ids; generic ones fill any gap (or a DB error)."""
        try:
            f = await self.repo.suggestion_facts() or {}
        except Exception as exc:  # noqa: BLE001 - the empty state must render even with the DB down
            log.warning("suggestion_facts failed: %s", short_error(exc))
            f = {}
        out: List[Dict[str, str]] = []
        hf = f.get("hidden_failures") or {}
        if hf.get("count"):
            out.append({"title": "Missions that hid failures",
                        "prompt": "%s of the %s missions that reported COMPLETED still had failed actions. Which "
                                  "ones, and what went wrong?" % (hf["count"], hf.get("of"))})
        a = f.get("top_asset")
        if a and a.get("asset_id"):
            out.append({"title": "Why does %s keep failing?" % a["asset_id"],
                        "prompt": "Why does %s (%s) keep showing anomalies, and what should we check next?"
                                  % (a.get("asset_name") or a["asset_id"], a["asset_id"])})
        w = f.get("worst_location")
        if w and w.get("location_id"):
            out.append({"title": "Trouble at %s" % w["location_id"],
                        "prompt": "Why do inspections at %s (%s) fail so often? Break down the failure types."
                                  % (w.get("location_name") or w["location_id"], w["location_id"])})
        fl = f.get("latest_serious_flag")
        if fl and fl.get("flag_id") is not None:
            out.append({"title": "Explain flag #%s" % fl["flag_id"],
                        "prompt": "Explain flag #%s%s%s. What evidence supports it, and what should the operator check?"
                                  % (fl["flag_id"], (" (%s)" % fl["title"]) if fl.get("title") else "",
                                     (" on mission %s" % fl["mission_id"]) if fl.get("mission_id") else "")})
        b = f.get("low_battery")
        if b and b.get("band"):
            out.append({"title": "Battery and success rate",
                        "prompt": "How does battery level affect inspection success? Compare the %s%% battery band "
                                  "with the others." % b["band"]})
        for g in ({"title": "Fleet health summary",
                   "prompt": "Give me a short fleet health summary: missions, failed actions, anomalies and open "
                             "flags."},
                  {"title": "Most common failures",
                   "prompt": "What are the most common failure types, and where do they happen?"},
                  {"title": "V1 vs V2 configuration",
                   "prompt": "Does configuration V2 perform better than V1? Compare their success rates."},
                  {"title": "Repeat offenders",
                   "prompt": "Which assets have the most anomalies, and how serious are they?"}):
            if len(out) >= 4:
                break
            out.append(g)
        return out[:4]

    # ── locks and background tasks ──────────────────────────────────────────────────────────────
    def busy(self, conversation_id: str) -> bool:
        at = self._busy.get(conversation_id)
        return at is not None and time.monotonic() - at < self.lock_ttl_s

    def _acquire(self, conversation_id: str) -> bool:
        if self.busy(conversation_id):
            return False
        self._busy[conversation_id] = time.monotonic()
        return True

    def _release(self, conversation_id: str) -> None:
        self._busy.pop(conversation_id, None)

    def _track(self, fut: asyncio.Future) -> asyncio.Future:
        self._tasks.add(fut)
        fut.add_done_callback(self._tasks.discard)
        return fut

    async def drain(self) -> None:
        """Wait for background work (answer storage, titles). Tests and shutdown."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ── turn setup (pre-stream; errors are JSON) ────────────────────────────────────────────────
    async def prepare_turn(self, conversation_id: str, *, content: str, model: Optional[str], mode: Optional[str],
                           edit_message_id: Optional[str], regenerate: bool, request_id: Optional[str]) -> Turn:
        conv = await self.repo.get_conversation(conversation_id) if norm_uuid(conversation_id) else None
        if conv is None:
            raise ChatError(404, "not_found", "no conversation with id %s" % conversation_id)
        cid = conv["conversation_id"]
        if not model:          # the stored model may have left CHAT_MODELS (gemma is opt-in): use the default then
            stored = conv.get("model")
            model = stored if stored in self.models else self.default_model
        mode = mode or conv.get("mode") or "fast"
        self.check_model(model)
        if mode not in MODES:
            raise ChatError(422, "invalid_request", "mode must be one of %s" % ", ".join(MODES))
        if regenerate and edit_message_id:
            raise ChatError(422, "invalid_request", "send either regenerate or edit_message_id, not both")
        content = (content or "").strip()
        if not regenerate and not content:
            raise ChatError(422, "invalid_request", "content must be 1-8000 characters")
        if not self.gemini.configured:
            raise ChatError(503, "gemini_unset", "GEMINI_API_KEY is not set on the backend, so the assistant is "
                                                 "disabled")
        if not self._acquire(cid):
            raise ChatError(409, "conversation_busy", "this conversation is already generating an answer; wait for "
                                                      "it to finish or stop it")
        try:
            msgs = await self.repo.list_messages(cid)
            user_row = {"role": "user", "content": content, "status": "complete", "sources": [], "citations": [],
                        "request_id": request_id}
            if edit_message_id:
                eid = norm_uuid(edit_message_id)
                target = next((m for m in msgs if m.get("message_id") == eid), None)
                if target is None or target.get("role") != "user":
                    raise ChatError(422, "invalid_request", "edit_message_id is not a user message in this "
                                                            "conversation")
                await self.repo.delete_messages_from(cid, target["seq"])
                history = [m for m in msgs if m["seq"] < target["seq"]]
                user = await self.repo.add_message(cid, user_row)
            elif regenerate:
                idx = next((i for i in range(len(msgs) - 1, -1, -1) if msgs[i].get("role") == "user"), None)
                if idx is None:
                    raise ChatError(422, "invalid_request", "nothing to regenerate: the conversation has no user "
                                                            "message")
                if idx + 1 < len(msgs):
                    await self.repo.delete_messages_from(cid, msgs[idx + 1]["seq"])
                user, history = msgs[idx], msgs[:idx]
            else:
                history = msgs
                user = await self.repo.add_message(cid, user_row)
            first = not any(m.get("role") == "user" for m in history)
            upd: Dict[str, Any] = {"model": model, "mode": mode}
            if first and conv.get("title_source") in ("default", "auto"):
                upd.update(title=auto_title(user.get("content") or ""), title_source="auto")
            conv = await self.repo.update_conversation(cid, **upd) or conv
            previous = next((m.get("content") for m in reversed(history) if m.get("role") == "user"), None)
            return Turn(conversation_id=cid, conversation=conv, model=model, mode=mode, user_message=user,
                        history=history, previous=previous, request_id=request_id,
                        wants_title=bool(self.settings.chat_title_model) and first
                        and conv.get("title_source") in ("default", "auto"),
                        prior=prior_answer(history))
        except ConversationNotFound:
            self._release(cid)
            raise ChatError(404, "not_found", "no conversation with id %s" % conversation_id) from None
        except BaseException:
            self._release(cid)
            raise

    # ── the stream ──────────────────────────────────────────────────────────────────────────────
    async def stream(self, turn: Turn) -> AsyncIterator[str]:
        """The SSE body. Relays the producer's frames; on disconnect stores the partial answer as `stopped`."""
        q: asyncio.Queue = asyncio.Queue()
        st = TurnState()
        producer = self._track(asyncio.ensure_future(self._produce(turn, st, q)))
        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), self.keepalive_s)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if item is None:
                    break
                yield item
        finally:
            if not producer.done():
                with anyio.CancelScope(shield=True):
                    await self._stop(turn, st, producer)

    async def _stop(self, turn: Turn, st: TurnState, producer: asyncio.Future) -> None:
        producer.cancel()          # closes the Gemini stream (or abandons retrieval / the title wait)
        try:
            await producer
        except BaseException:  # noqa: BLE001 - CancelledError, or whatever the producer was raising
            pass
        if st.final is None:
            st.final = self._track(asyncio.ensure_future(self._persist(turn, st, "stopped")))
        try:
            await asyncio.shield(st.final)
        except Exception:  # noqa: BLE001
            log.exception("storing the stopped answer failed")

    async def _produce(self, turn: Turn, st: TurnState, q: asyncio.Queue) -> None:
        def emit(event: str, data: Any) -> None:
            q.put_nowait(sse(event, data))

        try:
            emit("meta", {"conversation_id": turn.conversation_id, "request_id": turn.request_id,
                          "model": turn.model, "mode": turn.mode, "user_message": message_out(turn.user_message)})
            try:
                st.retrieval = await self.retriever.retrieve(turn.question, turn.previous, turn.request_id,
                                                             prior=turn.prior)
            except RetrievalFailed as exc:
                await self._fail(turn, st, emit, "retrieval_failed", exc.detail)
                return
            r = st.retrieval
            emit("sources", {"sources": r.sources, "retrieval": r.meta()})
            body = build_body(turn.model, turn.mode, turn.history, user_turn(r.block, turn.question),
                              self.settings.chat_history_messages)
            show = show_thoughts(turn.mode)

            def on_part(is_thought: bool, text: str) -> None:
                if is_thought:
                    if show:
                        st.thinking.append(text)
                        emit("thinking", {"delta": text})
                    return
                if st.first_text_at is None:
                    st.first_text_at = time.perf_counter()
                st.text.append(text)
                emit("delta", {"delta": text})

            st.t_call = time.perf_counter()
            try:
                res = await self.gemini.stream(
                    turn.model, body, on_part, request_id=turn.request_id,
                    metadata={"mode": turn.mode, "family": family(turn.model), "conversation_id": turn.conversation_id,
                              "sources": len(r.sources), "retrieval_mode": r.mode})
            except GeminiError as exc:
                st.t_end = time.perf_counter()
                await self._fail(turn, st, emit, exc.code, exc.detail, exc.retry_after_s)
                return
            st.t_end = time.perf_counter()
            st.usage = res.usage
            if not "".join(st.text).strip():
                await self._fail(turn, st, emit, "upstream_error", "Gemini returned no answer text (finishReason %s)"
                                 % (res.finish_reason or "none"))
                return
            msg = await self._finalize(turn, st, "complete")
            if msg is None:
                emit("error", {"code": "internal_error", "detail": "the answer was generated but could not be stored",
                               "message": None})
                return
            emit("done", {"message": message_out(msg), "conversation": await self._conversation(turn)})
            if turn.wants_title:
                title = await self._title(turn, msg.get("content") or "")
                if title:
                    emit("title", {"conversation_id": turn.conversation_id, "title": title})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the stream must end with an `error` event, never a broken body
            log.exception("chat turn %s failed", turn.request_id)
            if st.final is None:
                try:
                    await self._fail(turn, st, emit, "internal_error", short_error(exc))
                except Exception:  # noqa: BLE001
                    emit("error", {"code": "internal_error", "detail": self.gemini.scrub(short_error(exc)),
                                   "message": None})
            else:
                emit("error", {"code": "internal_error", "detail": self.gemini.scrub(short_error(exc)),
                               "message": None})
        finally:
            q.put_nowait(None)

    # ── storing ─────────────────────────────────────────────────────────────────────────────────
    async def _finalize(self, turn: Turn, st: TurnState, status: str, error: Optional[str] = None,
                        error_code: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if st.final is None:
            st.final = self._track(asyncio.ensure_future(self._persist(turn, st, status, error, error_code)))
        return await asyncio.shield(st.final)

    async def _persist(self, turn: Turn, st: TurnState, status: str, error: Optional[str] = None,
                       error_code: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Store the assistant message (complete / error / stopped) and release the conversation lock."""
        try:
            r = st.retrieval
            content = "".join(st.text)
            grounding = None
            if r is not None and (content.strip() or status == "stopped"):
                grounding = check(content, r.sources, r.texts, turn.question, r.mode, r.graph_seeds)
            thinking_ms = None
            if turn.mode == "thinking" and st.t_call is not None:
                end = st.first_text_at or st.t_end or time.perf_counter()
                thinking_ms = int((end - st.t_call) * 1000)
            usage = st.usage if st.usage and any(v is not None for v in st.usage.values()) else None
            row = {"role": "assistant", "content": content, "thinking": "".join(st.thinking) or None,
                   "thinking_ms": thinking_ms, "model": turn.model, "mode": turn.mode, "status": status,
                   "sources": r.sources if r else [], "citations": grounding["cited_ids"] if grounding else [],
                   "grounding": grounding, "usage": usage,
                   "latency_ms": int((time.perf_counter() - turn.t0) * 1000),
                   "error": self.gemini.scrub(error) if error else None,
                   "error_code": error_code if status == "error" else None, "request_id": turn.request_id}
            return await self.repo.add_message(turn.conversation_id, row)
        except Exception as exc:  # noqa: BLE001
            log.warning("storing the %s answer for %s failed: %s", status, turn.conversation_id, short_error(exc))
            return None
        finally:
            self._release(turn.conversation_id)

    async def _fail(self, turn: Turn, st: TurnState, emit, code: str, detail: str,
                    retry_after_s: Optional[int] = None) -> None:
        detail = self.gemini.scrub(detail)
        msg = await self._finalize(turn, st, "error", detail, code)
        data: Dict[str, Any] = {"code": code, "detail": detail}
        if retry_after_s is not None:
            data["retry_after_s"] = int(retry_after_s)
        data["message"] = _safe_message(msg)
        emit("error", data)

    async def _conversation(self, turn: Turn) -> Optional[Dict[str, Any]]:
        try:
            conv = await self.repo.get_conversation(turn.conversation_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("re-reading conversation %s failed: %s", turn.conversation_id, short_error(exc))
            conv = None
        return conversation_out(conv or turn.conversation)

    # ── titles ──────────────────────────────────────────────────────────────────────────────────
    async def _title(self, turn: Turn, answer: str) -> Optional[str]:
        """Waits up to the title timeout for a model title; the title task finishes (and is stored) even if the
        stream is stopped meanwhile."""
        task = self._track(asyncio.ensure_future(self._make_title(turn, answer)))
        try:
            return await asyncio.shield(task)
        except Exception:  # noqa: BLE001
            return None

    async def _make_title(self, turn: Turn, answer: str) -> Optional[str]:
        model = self.settings.chat_title_model
        try:
            text = await self.gemini.generate(model, title_body(model, turn.question, answer),
                                              request_id=turn.request_id, timeout_s=self.title_timeout_s,
                                              metadata={"purpose": "title", "conversation_id": turn.conversation_id})
        except GeminiError:
            return None
        title = clean_title(text)
        if not title:
            return None
        try:
            conv = await self.repo.get_conversation(turn.conversation_id)
            if conv is None or conv.get("title_source") == "user":
                return None
            if await self.repo.update_conversation(turn.conversation_id, title=title, title_source="model",
                                                   keep_user_title=True) is None:
                return None
            return title
        except Exception as exc:  # noqa: BLE001 - keep the auto title silently
            log.warning("storing the model title failed: %s", short_error(exc))
            return None
