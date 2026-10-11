"""POST /api/v1/chat/conversations/{id}/messages: SSE order and payloads, thinking per family, Gemini error mapping,
regenerate/edit, follow-up retrieval, full-text fallback, Stop on disconnect, keep-alives, titles, key hygiene."""
import asyncio
import json
import logging

import httpx
import pytest

from .fakes_chat import (GEMINI_KEY, answer_chunks, body_text, chat_harness, chunk, drive, embed_down, embed_ok,
                         events, gemini_stream, gemini_title, parse_sse, sse_body)


ALL_MODELS = ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemma-4-31b-it"]


@pytest.fixture
async def ch(make_harness):
    h, repo = chat_harness(make_harness)
    yield h, repo
    await h.client.aclose()


@pytest.fixture
async def ch_gemma(make_harness):
    """Gemma is opt-in (chat.2): CHAT_MODELS must list it."""
    h, repo = chat_harness(make_harness, chat_models=ALL_MODELS)
    yield h, repo
    await h.client.aclose()


async def _conv(h, **body) -> str:
    r = await h.client.post("/api/v1/chat/conversations", json=body)
    assert r.status_code == 201, r.text
    return r.json()["conversation_id"]


async def _ask(h, cid, content="What happened to HX-217's gauge?", **kw):
    r = await h.client.post("/api/v1/chat/conversations/%s/messages" % cid, json={"content": content, **kw})
    return r


def _names(evs):
    return [n for n, _ in evs]


# ── happy path ──────────────────────────────────────────────────────────────────────────────────
async def test_fast_mode_event_order_payloads_and_request(ch, mock):
    h, repo = ch
    embed_ok(mock)
    cap = []
    gemini_stream(mock, captured=cap)
    cid = await _conv(h)
    r = await _ask(h, cid, model="gemini-3.8-flash", mode="fast")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    evs = events(r.text)
    assert _names(evs) == ["meta", "sources", "delta", "delta", "delta", "done"]
    meta, sources, done = evs[0][1], evs[1][1], evs[-1][1]
    assert meta["conversation_id"] == cid and meta["model"] == "gemini-3.8-flash" and meta["mode"] == "fast"
    assert meta["request_id"] == r.headers["x-request-id"]
    um = meta["user_message"]
    assert um["role"] == "user" and um["seq"] == 1 and um["content"] == "What happened to HX-217's gauge?"
    assert um["sources"] == [] and um["citations"] == [] and um["grounding"] is None and um["thinking"] is None
    ids = [s["id"] for s in sources["sources"]]
    assert "incident:AN012" in ids and "asset:HX-217" in ids and "stat:fleet" in ids
    assert [s["n"] for s in sources["sources"]] == list(range(1, len(ids) + 1))
    assert "topic:weather" not in ids                     # vector-only score 0.2 < MIN_VECTOR_SCORE
    hx = next(s for s in sources["sources"] if s["id"] == "asset:HX-217")
    assert hx["origin"] == "sql" and hx["refs"]["asset_id"] == "HX-217"   # digest + graph node merged
    ret = sources["retrieval"]
    assert ret["mode"] == "hybrid" and ret["graph_seeds"][0] == "asset:HX-217" and ret["gaps"] == []
    assert set(ret["counts"]) == {"rag", "graph", "sql"} and sum(ret["counts"].values()) == len(ids)
    assert "".join(d["delta"] for n, d in evs if n == "delta") == "".join(
        ["HX-217's gauge read 412.5 psi ", "[incident:AN012], above the 380 psi band ",
         "[asset:HX-217, incident:AN012]."])
    msg, conv = done["message"], done["conversation"]
    assert msg["role"] == "assistant" and msg["status"] == "complete" and msg["seq"] == 2 and msg["mode"] == "fast"
    assert msg["citations"] == ["incident:AN012", "asset:HX-217"] and msg["thinking"] is None
    assert msg["thinking_ms"] is None and msg["latency_ms"] >= 0
    assert msg["grounding"] == {"grounded": True, "cited_ids": ["incident:AN012", "asset:HX-217"], "unknown_ids": [],
                                "unverified_numbers": [], "source_count": len(ids), "retrieval_mode": "hybrid",
                                "graph_seeds": ret["graph_seeds"]}
    assert msg["usage"] == {"prompt_tokens": 3100, "output_tokens": 40, "thoughts_tokens": 12}
    assert msg["sources"] == sources["sources"] and msg["error_code"] is None
    assert conv["message_count"] == 2 and conv["title_source"] == "auto"
    assert conv["title"] == "What happened to HX-217's gauge?"
    # the request: key in the header only, system prompt, low thinking, sources block + question last
    (call,) = cap
    assert call["model"] == "gemini-3.8-flash" and GEMINI_KEY not in call["url"]
    assert call["headers"]["x-goog-api-key"] == GEMINI_KEY
    b = call["body"]
    assert b["generationConfig"] == {"maxOutputTokens": 8192, "thinkingConfig": {"thinkingLevel": "low"}}
    assert "temperature" not in b["generationConfig"]
    assert "Answer only from the <sources> block" in b["systemInstruction"]["parts"][0]["text"]
    assert len(b["contents"]) == 1 and b["contents"][0]["role"] == "user"
    text = b["contents"][0]["parts"][0]["text"]
    assert text.startswith("<sources>\n") and '<source id="incident:AN012"' in text
    assert text.endswith("Question: What happened to HX-217's gauge?") and len(text) < 12_000 + 200
    # stored + logged
    stored = await repo.list_messages(cid)
    assert [(m["role"], m["status"]) for m in stored] == [("user", "complete"), ("assistant", "complete")]
    (mc,) = [c for c in h.sink.model_calls if c["system"] == "chat"]
    assert mc["ok"] and mc["endpoint"] == "gemini:streamGenerateContent" and mc["model"] == "gemini-3.8-flash"
    assert mc["input_tokens"] == 3100 and mc["output_tokens"] == 52 and mc["metadata"]["provider"] == "gemini"
    assert mc["metadata"]["conversation_id"] == cid and mc["request_id"] == meta["request_id"]


async def test_thinking_mode_streams_thoughts_before_answer(ch, mock):
    h, repo = ch
    embed_ok(mock)
    cap = []
    gemini_stream(mock, chunks=answer_chunks(thoughts=["Looking at the gauge source. ", "Comparing to the band."]),
                  captured=cap)
    cid = await _conv(h)
    evs = events((await _ask(h, cid, model="gemini-3.5-flash", mode="thinking")).text)
    assert _names(evs) == ["meta", "sources", "thinking", "thinking", "delta", "delta", "delta", "done"]
    assert evs[2][1] == {"delta": "Looking at the gauge source. "}
    msg = evs[-1][1]["message"]
    assert msg["thinking"] == "Looking at the gauge source. Comparing to the band." and msg["mode"] == "thinking"
    assert isinstance(msg["thinking_ms"], int) and "Looking" not in msg["content"]
    assert cap[0]["body"]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "high", "includeThoughts": True}


async def test_thinking_mode_without_summaries_still_completes(ch, mock):
    h, _ = ch
    embed_ok(mock)
    gemini_stream(mock)
    cid = await _conv(h)
    evs = events((await _ask(h, cid, model="gemini-3.5-flash-lite", mode="thinking")).text)
    assert "thinking" not in _names(evs) and _names(evs)[-1] == "done"
    msg = evs[-1][1]["message"]
    assert msg["thinking"] is None and isinstance(msg["thinking_ms"], int)


async def test_gemma_fast_sends_no_thinking_config_and_hides_thoughts(ch_gemma, mock):
    h, _ = ch_gemma
    embed_ok(mock)
    cap = []
    gemini_stream(mock, chunks=answer_chunks(thoughts=["secret reasoning"]), captured=cap)
    cid = await _conv(h)
    evs = events((await _ask(h, cid, model="gemma-4-31b-it", mode="fast")).text)
    assert "thinking" not in _names(evs)
    msg = evs[-1][1]["message"]
    assert "secret reasoning" not in msg["content"] and msg["thinking"] is None
    b = cap[0]["body"]
    assert b["generationConfig"] == {"maxOutputTokens": 8192}            # no thinkingConfig at all (§0)
    # chat.2: systemInstruction for every family, Gemma 4 included; the user turn is just sources + question
    assert b["systemInstruction"]["parts"][0]["text"].startswith("You are SPOTbot, SPOTlight's AI assistant")
    assert b["contents"][0]["parts"][0]["text"].startswith("<sources>\n")


async def test_gemma_thinking_streams_thought_parts(ch_gemma, mock):
    h, _ = ch_gemma
    embed_ok(mock)
    cap = []
    gemini_stream(mock, chunks=answer_chunks(thoughts=["gemma thinks"]), captured=cap)
    cid = await _conv(h)
    evs = events((await _ask(h, cid, model="gemma-4-31b-it", mode="thinking")).text)
    assert ("thinking", {"delta": "gemma thinks"}) in evs
    assert "thinkingConfig" not in cap[0]["body"]["generationConfig"]


# ── Gemini errors ───────────────────────────────────────────────────────────────────────────────
RATE_LIMIT = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                        "message": "You exceeded your current quota. Please retry in 41.53s. key=" + GEMINI_KEY,
                        "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
                            {"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
                                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "41s"}]}}


async def test_429_is_rate_limited_with_retry_after_and_a_stored_error(ch, mock, caplog):
    h, repo = ch
    caplog.set_level(logging.DEBUG)
    embed_ok(mock)
    gemini_stream(mock, status=429, error=RATE_LIMIT)
    cid = await _conv(h)
    r = await _ask(h, cid)
    evs = events(r.text)
    assert _names(evs) == ["meta", "sources", "error"]
    err = evs[-1][1]
    assert err["code"] == "rate_limited" and err["retry_after_s"] == 41
    assert err["message"]["status"] == "error" and err["message"]["role"] == "assistant"
    assert err["message"]["error"] == err["detail"] and "rate limited" in err["detail"]
    assert err["message"]["error_code"] == "rate_limited"                     # chat.2: stored for reloads
    stored = await repo.list_messages(cid)
    assert [(m["role"], m["status"]) for m in stored] == [("user", "complete"), ("assistant", "error")]
    assert stored[1]["error_code"] == "rate_limited" and stored[0]["error_code"] is None
    mc = [c for c in h.sink.model_calls if c["system"] == "chat"]
    assert len(mc) == 1 and not mc[0]["ok"] and mc[0]["metadata"]["code"] == "rate_limited"
    # the key never leaves the backend, even when the upstream message echoes it
    for blob in (r.text, json.dumps(stored, default=str), repr(h.sink.model_calls), caplog.text):
        assert GEMINI_KEY not in blob
    assert not h.svc.chat.busy(cid)


@pytest.mark.parametrize("status,error,code", [
    (404, {"error": {"code": 404, "message": "models/gemini-3.8-flash is not found for API version v1beta"}},
     "model_unavailable"),
    (400, {"error": {"code": 400, "message": "Thinking level is not supported for this model."}}, "model_unavailable"),
    (400, {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                     "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}]}},
     "gemini_auth"),
    (403, {"error": {"code": 403, "message": "Permission denied"}}, "gemini_auth"),
    (500, {"error": {"code": 500, "message": "Internal error"}}, "upstream_error"),
    (503, {"error": {"code": 503, "status": "UNAVAILABLE",
                     "message": "This model is currently experiencing high demand. Spikes in demand are usually "
                                "temporary. Please try again later."}}, "model_unavailable"),           # chat.2
    (503, {"error": {"code": 503, "message": "The model is overloaded."}}, "model_unavailable"),
])
async def test_http_errors_map_to_contract_codes(ch, mock, status, error, code):
    h, repo = ch
    embed_ok(mock)
    route = gemini_stream(mock, status=status, error=error)
    cid = await _conv(h)
    evs = events((await _ask(h, cid)).text)
    assert evs[-1][0] == "error" and evs[-1][1]["code"] == code and "retry_after_s" not in evs[-1][1]
    assert evs[-1][1]["message"]["status"] == "error" and evs[-1][1]["message"]["error_code"] == code
    assert (await repo.list_messages(cid))[-1]["error_code"] == code
    assert route.call_count == 1                      # one attempt, no retries, no model fallback


async def test_in_stream_unavailable_is_model_unavailable(ch_gemma, mock):
    """chat.2: Gemma answered 200, then an in-stream 503 UNAVAILABLE ("high demand"); it was upstream_error."""
    h, repo = ch_gemma
    embed_ok(mock)
    err = {"error": {"code": 503, "status": "UNAVAILABLE",
                     "message": "This model is currently experiencing high demand. Please try again later."}}
    gemini_stream(mock, chunks=[chunk(("Partial answer ", False)), err])
    cid = await _conv(h)
    evs = events((await _ask(h, cid, model="gemma-4-31b-it")).text)
    assert _names(evs) == ["meta", "sources", "delta", "error"]
    e = evs[-1][1]
    assert e["code"] == "model_unavailable" and "gemma-4-31b-it" in e["detail"] and "retry_after_s" not in e
    assert e["message"]["error_code"] == "model_unavailable" and e["message"]["content"] == "Partial answer "
    # the stored partial error answer is not history for the next turn
    gemini_stream(mock, captured=(cap := []))
    await _ask(h, cid, "and now?", model="gemini-3.5-flash")
    assert [c["role"] for c in cap[0]["body"]["contents"]] == ["user"]


async def test_blocked_finish_reason_is_blocked_and_keeps_partial_text(ch, mock):
    h, _ = ch
    embed_ok(mock)
    gemini_stream(mock, chunks=[chunk(("Partial ", False)), chunk(("", False), finish="SAFETY")])
    cid = await _conv(h)
    evs = events((await _ask(h, cid)).text)
    assert _names(evs) == ["meta", "sources", "delta", "error"]
    assert evs[-1][1]["code"] == "blocked" and evs[-1][1]["message"]["content"] == "Partial "


async def test_transport_error_is_upstream_error(ch, mock):
    h, _ = ch
    embed_ok(mock)
    from .fakes_chat import STREAM_RE
    mock.post(url__regex=STREAM_RE).mock(side_effect=httpx.ConnectError("connection refused"))
    cid = await _conv(h)
    evs = events((await _ask(h, cid)).text)
    assert evs[-1][1]["code"] == "upstream_error" and evs[-1][1]["message"]["status"] == "error"


async def test_retrieval_failed_when_the_database_is_down(ch, mock):
    h, repo = ch
    embed_ok(mock)
    route = gemini_stream(mock)
    cid = await _conv(h)
    repo.fail |= {"search_documents", "match_graph_nodes", "fleet_snapshot"}
    evs = events((await _ask(h, cid)).text)
    assert _names(evs) == ["meta", "error"] and evs[-1][1]["code"] == "retrieval_failed"
    assert evs[-1][1]["message"]["status"] == "error" and route.call_count == 0
    assert evs[-1][1]["message"]["error_code"] == "retrieval_failed"


# ── retrieval behaviour ─────────────────────────────────────────────────────────────────────────
async def test_embed_down_falls_back_to_fulltext(ch, mock):
    h, repo = ch
    embed_down(mock)
    gemini_stream(mock)
    cid = await _conv(h)
    evs = events((await _ask(h, cid)).text)
    src = dict(evs)["sources"]
    assert src["retrieval"]["mode"] == "fulltext"
    assert src["retrieval"]["gaps"] == ["embedding unavailable (HTTP 503: loading): full-text search only"]
    docs = [a for n, a in repo.calls if n == "search_documents"]
    assert docs and all(a["vec"] is None for a in docs)
    assert all(a["vec"] is None for n, a in repo.calls if n == "match_graph_nodes")
    assert dict(evs)["done"]["message"]["grounding"]["retrieval_mode"] == "fulltext"


async def test_follow_up_sends_history_and_runs_a_second_retrieval(ch, mock):
    h, repo = ch
    embed_ok(mock)
    cap = []
    gemini_stream(mock, captured=cap)
    cid = await _conv(h)
    await _ask(h, cid, "Tell me about mission M007")
    repo.calls.clear()
    evs = events((await _ask(h, cid, "what about the second one?")).text)
    assert _names(evs)[-1] == "done"
    contents = cap[1]["body"]["contents"]
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    assert contents[0]["parts"][0]["text"] == "Tell me about mission M007"     # history is plain text, no sources
    assert "<sources>" not in contents[1]["parts"][0]["text"]
    qs = [a["q"] for n, a in repo.calls if n == "search_documents"]
    assert "Tell me about mission M007\nwhat about the second one?" in qs
    digest_calls = [a for n, a in repo.calls if n == "entity_digests"]
    assert any("M007" in a["missions"] for a in digest_calls)              # entities from the previous turn
    assert "mission:M007" in [s["id"] for s in dict(evs)["sources"]["sources"]]


# ── regenerate / edit ───────────────────────────────────────────────────────────────────────────
async def test_regenerate_replaces_the_trailing_answer(ch, mock):
    h, repo = ch
    embed_ok(mock)
    cap = []
    gemini_stream(mock, captured=cap)
    cid = await _conv(h)
    first = dict(events((await _ask(h, cid)).text))
    old = first["done"]["message"]["message_id"]
    evs = dict(events((await _ask(h, cid, "", regenerate=True)).text))
    assert evs["meta"]["user_message"]["message_id"] == first["meta"]["user_message"]["message_id"]
    stored = await repo.list_messages(cid)
    assert [(m["seq"], m["role"]) for m in stored] == [(1, "user"), (2, "assistant")]
    assert stored[1]["message_id"] != old and stored[1]["message_id"] == evs["done"]["message"]["message_id"]
    assert len(cap[1]["body"]["contents"]) == 1                               # the old answer is not history


async def test_regenerate_after_an_error_answers_the_same_question(ch, mock):
    h, repo = ch
    embed_ok(mock)
    gemini_stream(mock, status=429, error=RATE_LIMIT)
    cid = await _conv(h)
    await _ask(h, cid)
    mock.routes.clear()
    embed_ok(mock)
    gemini_stream(mock)
    evs = dict(events((await _ask(h, cid, regenerate=True, model="gemini-3.5-flash")).text))
    assert evs["done"]["message"]["model"] == "gemini-3.5-flash"
    assert evs["done"]["conversation"]["model"] == "gemini-3.5-flash"
    assert [(m["role"], m["status"]) for m in await repo.list_messages(cid)] == [("user", "complete"),
                                                                                 ("assistant", "complete")]


async def test_edit_replaces_the_message_and_everything_after(ch, mock):
    h, repo = ch
    embed_ok(mock)
    cap = []
    gemini_stream(mock, captured=cap)
    cid = await _conv(h)
    await _ask(h, cid, "first question")
    await _ask(h, cid, "second question")
    msgs = await repo.list_messages(cid)
    second_user = msgs[2]
    evs = dict(events((await _ask(h, cid, "second question, edited",
                                  edit_message_id=second_user["message_id"])).text))
    stored = await repo.list_messages(cid)
    assert [(m["seq"], m["role"], m["content"]) for m in stored][:3] == [
        (1, "user", "first question"), (2, "assistant", msgs[1]["content"]), (3, "user", "second question, edited")]
    assert stored[1]["message_id"] == msgs[1]["message_id"]                  # earlier messages are untouched
    assert len(stored) == 4 and evs["meta"]["user_message"]["content"] == "second question, edited"
    assert [c["role"] for c in cap[-1]["body"]["contents"]] == ["user", "model", "user"]


@pytest.mark.parametrize("body,needs_history", [
    ({"content": "x", "edit_message_id": "not-a-uuid"}, False),
    ({"content": "x", "edit_message_id": "ASSISTANT"}, True),
    ({"content": "", "regenerate": True}, False),               # nothing to regenerate
    ({"content": "   "}, False),
    ({"content": "x", "model": "gemini-2.5-pro"}, False),
    ({"content": "x", "mode": "turbo"}, False),
    ({"content": "x" * 8001}, False),
    ({"content": "x", "regenerate": True, "edit_message_id": "ASSISTANT"}, True),
])
async def test_invalid_requests_are_422(ch, mock, body, needs_history):
    h, repo = ch
    embed_ok(mock)
    gemini_stream(mock)
    cid = await _conv(h)
    if needs_history:
        await _ask(h, cid)
        asst = (await repo.list_messages(cid))[1]["message_id"]
        body = {**body, "edit_message_id": asst}
    n = len(await repo.list_messages(cid))
    r = await h.client.post("/api/v1/chat/conversations/%s/messages" % cid, json=body)
    assert r.status_code == 422 and r.json()["error"] == "invalid_request", r.text
    assert len(await repo.list_messages(cid)) == n and not h.svc.chat.busy(cid)


async def test_unknown_or_malformed_conversation_is_404(ch, mock):
    h, _ = ch
    for cid in ("nope", "00000000-0000-0000-0000-000000000000"):
        r = await _ask(h, cid)
        assert r.status_code == 404 and r.json()["error"] == "not_found"


async def test_gemini_unset_is_503_and_stores_nothing(make_harness, mock):
    h, repo = chat_harness(make_harness, gemini_api_key=None)
    cid = await _conv(h)
    r = await _ask(h, cid)
    assert r.status_code == 503 and r.json()["error"] == "gemini_unset"
    assert await repo.list_messages(cid) == []
    assert (await h.client.get("/api/v1/chat/models")).json()["configured"] is False
    await h.client.aclose()


# ── concurrency, stop, keep-alive ───────────────────────────────────────────────────────────────
async def test_second_message_while_generating_is_409(ch, mock):
    h, repo = ch
    embed_ok(mock)
    release = asyncio.Event()
    started = asyncio.Event()

    async def slow():
        yield sse_body([chunk(("Working on it ", False))])
        started.set()
        await release.wait()
        yield sse_body([chunk(("[incident:AN012].", False), finish="STOP", usage={"promptTokenCount": 10})])
    gemini_stream(mock, stream=slow)
    cid = await _conv(h)
    first = asyncio.create_task(drive(h.app, "/api/v1/chat/conversations/%s/messages" % cid, {"content": "hi"}))
    await asyncio.wait_for(started.wait(), 5)
    r = await _ask(h, cid, "again")
    assert r.status_code == 409 and r.json()["error"] == "conversation_busy"
    release.set()
    sent = await first
    assert [n for n, _ in events(body_text(sent))][-1] == "done"
    assert not h.svc.chat.busy(cid)


async def test_disconnect_stops_generation_and_stores_partial_answer(ch, mock):
    h, repo = ch
    embed_ok(mock)
    closed = asyncio.Event()

    async def endless():
        try:
            yield sse_body([chunk(("HX-217 read 412.5 psi [incident:AN012] and ", False))])
            await asyncio.Event().wait()            # Gemini still "thinking" when the operator presses Stop
            yield b""
        finally:
            closed.set()
    gemini_stream(mock, stream=endless)
    cid = await _conv(h)
    sent = await drive(h.app, "/api/v1/chat/conversations/%s/messages" % cid, {"content": "What about HX-217?"},
                       disconnect_after="delta")
    await h.svc.chat.drain()
    evs = events(body_text(sent))
    assert _names(evs) == ["meta", "sources", "delta"]                     # streamed live, then nothing more
    assert closed.is_set()                                                  # the Gemini stream was closed
    stored = await repo.list_messages(cid)
    a = stored[-1]
    assert a["role"] == "assistant" and a["status"] == "stopped"
    assert a["content"] == "HX-217 read 412.5 psi [incident:AN012] and "
    assert a["grounding"]["cited_ids"] == ["incident:AN012"] and a["grounding"]["unverified_numbers"] == []
    mc = [c for c in h.sink.model_calls if c["system"] == "chat"]
    assert len(mc) == 1 and not mc[0]["ok"] and mc[0]["metadata"]["code"] == "stopped"
    assert not h.svc.chat.busy(cid)
    # the next message works right away
    gemini_stream(mock)
    r = await _ask(h, cid, "and now?")
    assert events(r.text)[-1][0] == "done"


async def test_keep_alive_comments_while_waiting(ch, mock):
    h, _ = ch
    embed_ok(mock)
    h.svc.chat.keepalive_s = 0.05

    async def slow():
        await asyncio.sleep(0.3)
        yield sse_body(answer_chunks())
    gemini_stream(mock, stream=slow)
    cid = await _conv(h)
    r = await _ask(h, cid)
    raw = parse_sse(r.text)
    assert (":", "keep-alive") in raw and raw[-1][0] == "done"


# ── titles ──────────────────────────────────────────────────────────────────────────────────────
async def test_title_event_after_done_on_first_exchange_only(make_harness, mock):
    h, repo = chat_harness(make_harness, chat_title_model="gemini-3.1-flash-lite")
    embed_ok(mock)
    gemini_stream(mock)
    cap = []
    route = gemini_title(mock, text='"HX-217 Gauge Reading."', captured=cap)
    cid = await _conv(h)
    evs = events((await _ask(h, cid)).text)
    assert _names(evs)[-2:] == ["done", "title"]
    assert evs[-1][1] == {"conversation_id": cid, "title": "HX-217 Gauge Reading"}
    assert evs[-2][1]["conversation"]["title_source"] == "auto"            # auto title set right away
    conv = (await h.client.get("/api/v1/chat/conversations/%s" % cid)).json()["conversation"]
    assert conv["title"] == "HX-217 Gauge Reading" and conv["title_source"] == "model"
    assert cap[0]["model"] == "gemini-3.1-flash-lite" and cap[0]["headers"]["x-goog-api-key"] == GEMINI_KEY
    # chat.2: minimal thinking and 40 output tokens (0.7 s instead of 18.9 s at "low")
    assert cap[0]["body"]["generationConfig"] == {"maxOutputTokens": 40,
                                                  "thinkingConfig": {"thinkingLevel": "minimal"}}
    titles = [c for c in h.sink.model_calls if c["endpoint"] == "gemini:generateContent"]
    assert len(titles) == 1 and titles[0]["ok"] and titles[0]["metadata"]["purpose"] == "title"
    evs2 = events((await _ask(h, cid, "and the second one?")).text)
    assert "title" not in _names(evs2) and route.call_count == 1
    await h.client.aclose()


async def test_user_title_is_never_overwritten(make_harness, mock):
    h, repo = chat_harness(make_harness, chat_title_model="gemini-3.1-flash-lite")
    embed_ok(mock)
    gemini_stream(mock)
    route = gemini_title(mock)
    cid = await _conv(h)
    await h.client.patch("/api/v1/chat/conversations/%s" % cid, json={"title": "My HX-217 notes"})
    evs = events((await _ask(h, cid)).text)
    assert "title" not in _names(evs) and route.call_count == 0
    conv = (await repo.get_conversation(cid))
    assert conv["title"] == "My HX-217 notes" and conv["title_source"] == "user"
    await h.client.aclose()


async def test_rename_during_the_title_call_wins(make_harness, mock):
    h, repo = chat_harness(make_harness, chat_title_model="gemini-3.1-flash-lite")
    embed_ok(mock)
    gemini_stream(mock)
    cid = await _conv(h)
    gemini_title(mock, side=lambda: repo.conversations[cid].update(title="Renamed", title_source="user"))
    evs = events((await _ask(h, cid)).text)
    assert "title" not in _names(evs)
    assert repo.conversations[cid]["title"] == "Renamed"
    await h.client.aclose()


async def test_title_failure_keeps_the_auto_title_silently(make_harness, mock):
    h, repo = chat_harness(make_harness, chat_title_model="gemini-3.1-flash-lite")
    embed_ok(mock)
    gemini_stream(mock)
    from .fakes_chat import TITLE_RE
    mock.post(url__regex=TITLE_RE).mock(return_value=httpx.Response(429, json=RATE_LIMIT))
    cid = await _conv(h)
    evs = events((await _ask(h, cid, "Which waypoints fail the most often during night rounds at the plant?")).text)
    assert _names(evs)[-1] == "done"
    conv = await repo.get_conversation(cid)
    assert conv["title_source"] == "auto" and conv["title"].endswith("…") and len(conv["title"]) <= 61
    await h.client.aclose()


async def test_key_never_appears_in_responses_logs_or_storage(make_harness, mock, caplog):
    caplog.set_level(logging.DEBUG)
    h, repo = chat_harness(make_harness, chat_title_model="gemini-3.1-flash-lite")
    embed_ok(mock)
    gemini_stream(mock)
    gemini_title(mock)
    cid = await _conv(h)
    texts = [(await _ask(h, cid)).text, (await h.client.get("/api/v1/chat/models")).text,
             (await h.client.get("/api/v1/chat/conversations/%s" % cid)).text]
    for call in mock.calls:
        assert GEMINI_KEY not in str(call.request.url)
    blobs = texts + [repr(h.sink.model_calls), repr(h.sink.requests), caplog.text, repr(h.settings),
                     repr(h.svc.chat.gemini), json.dumps(repo.messages, default=str)]
    for b in blobs:
        assert GEMINI_KEY not in b
    await h.client.aclose()


# ── chat.2: follow-ups ──────────────────────────────────────────────────────────────────────────
SECOND_ONE = ["Two missions hid failures: M014 had the gauge issue [incident:AN012] ",
              "and M007 lost localization [asset:HX-217]."]


async def test_what_about_the_second_one_retrieves_the_previous_answers_subject(ch, mock):
    h, repo = ch
    embed_ok(mock)
    cap = []
    gemini_stream(mock, chunks=answer_chunks(parts=SECOND_ONE), captured=cap)
    cid = await _conv(h)
    first = dict(events((await _ask(h, cid, "Which missions hid failures?")).text))
    assert first["done"]["message"]["citations"] == ["incident:AN012", "asset:HX-217"]
    repo.calls.clear()
    repo.docs = [d for d in repo.docs if d["doc_id"] != "incident:AN012"]     # not retrieved fresh this time
    evs = dict(events((await _ask(h, cid, "What about the second one?")).text))
    digest = [a for n, a in repo.calls if n == "entity_digests"][0]
    assert digest["missions"] == ["M014", "M007"]                       # from the previous answer, in its order
    ids = [s["id"] for s in evs["sources"]["sources"]]
    assert "mission:M007" in ids                                        # the subject of "the second one"
    carried = next(s for s in evs["sources"]["sources"] if s["id"] == "incident:AN012")
    assert carried["title"] == "Gauge out of range on HX-217 (M014)"     # carried over with its stored title
    contents = cap[1]["body"]["contents"]
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    assert "[" not in contents[1]["parts"][0]["text"]                   # old citation markers stripped
    assert '<source id="incident:AN012"' in contents[2]["parts"][0]["text"]


async def test_follow_up_after_an_error_turn_still_alternates(ch, mock):
    h, repo = ch
    embed_ok(mock)
    gemini_stream(mock, status=429, error=RATE_LIMIT)
    cid = await _conv(h)
    await _ask(h, cid, "first question")
    mock.routes.clear()
    embed_ok(mock)
    cap = []
    gemini_stream(mock, captured=cap)
    evs = events((await _ask(h, cid, "second question", model="gemini-3.5-flash")).text)
    assert _names(evs)[-1] == "done"
    (call,) = cap
    contents = call["body"]["contents"]
    assert [c["role"] for c in contents] == ["user"]                    # was ['user', 'user']
    text = contents[0]["parts"][0]["text"]
    assert text.startswith("first question\n<sources>\n") and text.endswith("Question: second question")


async def test_a_stored_model_that_left_chat_models_falls_back_to_the_default(ch, mock):
    h, repo = ch
    embed_ok(mock)
    cap = []
    gemini_stream(mock, captured=cap)
    cid = await _conv(h)
    repo.conversations[cid]["model"] = "gemma-4-31b-it"                 # chosen back when gemma was offered
    r = await h.client.post("/api/v1/chat/conversations/%s/messages" % cid, json={"content": "hi"})
    assert r.status_code == 200 and cap[0]["model"] == "gemini-3.8-flash"
    r = await _ask(h, cid, "again", model="gemma-4-31b-it")             # asking for it explicitly is still a 422
    assert r.status_code == 422
