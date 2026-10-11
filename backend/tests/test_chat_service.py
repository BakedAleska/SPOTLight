"""ChatService and router gaps (docs/chat-contract.md §4): the per-conversation lock (lease expiry, release on every
exit path), Stop during retrieval and during the title wait (the answer is stored exactly once), storage failures,
an empty answer, edit/regenerate edge cases, the title switch, and validation boundaries on every route."""
import asyncio
import time

import httpx
import pytest

from app.config import Settings
from app.services.chat import service as S

from .fakes_chat import (answer_chunks, body_text, chat_harness, chunk, drive, embed_ok, events, gemini_stream,
                         gemini_title)


@pytest.fixture
async def ch(make_harness):
    h, repo = chat_harness(make_harness)
    yield h, repo
    await h.client.aclose()


async def _conv(h, **body) -> str:
    r = await h.client.post("/api/v1/chat/conversations", json=body)
    assert r.status_code == 201, r.text
    return r.json()["conversation_id"]


def _url(cid):
    return "/api/v1/chat/conversations/%s/messages" % cid


async def _ask(h, cid, content="What happened to HX-217's gauge?", **kw):
    return await h.client.post(_url(cid), json={"content": content, **kw})


def _names(evs):
    return [n for n, _ in evs]


# ── the lock ────────────────────────────────────────────────────────────────────────────────────
async def test_the_lock_has_a_lease_so_a_wedged_chat_frees_itself(ch, mock):
    h, repo = ch
    embed_ok(mock)
    gemini_stream(mock)
    svc = h.svc.chat
    assert svc.lock_ttl_s == h.settings.chat_timeout_s + h.settings.chat_retrieval_budget_s + 60.0
    cid = await _conv(h)
    svc._busy[cid] = time.monotonic() - 5                       # a live lock: busy
    assert (await _ask(h, cid)).status_code == 409 and repo.messages[cid] == []
    svc._busy[cid] = time.monotonic() - svc.lock_ttl_s - 1      # a stream that never released, past its lease
    r = await _ask(h, cid)
    assert r.status_code == 200 and _names(events(r.text))[-1] == "done" and not svc.busy(cid)


async def test_one_busy_chat_does_not_block_another(ch, mock):
    h, _ = ch
    embed_ok(mock)
    release, started = asyncio.Event(), asyncio.Event()

    async def slow():
        yield b""
        started.set()
        await release.wait()
        from .fakes_chat import sse_body
        yield sse_body(answer_chunks())
    gemini_stream(mock, stream=slow)
    a, b = await _conv(h), await _conv(h)
    first = asyncio.ensure_future(drive(h.app, _url(a), {"content": "hi"}))
    await asyncio.wait_for(started.wait(), 5)
    assert h.svc.chat.busy(a) and not h.svc.chat.busy(b)
    mock.routes.clear()
    embed_ok(mock)
    gemini_stream(mock)
    assert _names(events((await _ask(h, b)).text))[-1] == "done"
    release.set()
    assert _names(events(body_text(await first)))[-1] == "done"
    assert not h.svc.chat.busy(a) and not h.svc.chat.busy(b)


async def test_a_database_error_before_the_stream_releases_the_lock(ch, mock):
    h, repo = ch
    embed_ok(mock)
    gemini_stream(mock)
    cid = await _conv(h)
    repo.fail.add("list_messages")
    r = await _ask(h, cid)
    assert r.status_code == 503 and r.json()["error"] == "database_unavailable"
    assert not h.svc.chat.busy(cid)
    repo.fail.clear()
    assert _names(events((await _ask(h, cid)).text))[-1] == "done"


async def test_storing_the_answer_fails_is_an_internal_error_event_and_releases_the_lock(ch, mock):
    h, repo = ch
    embed_ok(mock)
    gemini_stream(mock)
    cid = await _conv(h)
    orig = repo.add_message

    async def add_message(conversation_id, m):
        if m.get("role") == "assistant":
            from app.db.pool import DatabaseUnavailable
            raise DatabaseUnavailable("main database is not reachable")
        return await orig(conversation_id, m)
    repo.add_message = add_message
    evs = events((await _ask(h, cid)).text)
    assert _names(evs)[:2] == ["meta", "sources"] and _names(evs)[-1] == "error"
    err = evs[-1][1]
    assert err["code"] == "internal_error" and err["message"] is None and "could not be stored" in err["detail"]
    assert "done" not in _names(evs) and not h.svc.chat.busy(cid)


async def test_an_unexpected_exception_mid_turn_ends_with_an_error_event_and_a_stored_error(ch, mock, monkeypatch):
    h, repo = ch
    embed_ok(mock)
    route = gemini_stream(mock)
    cid = await _conv(h)

    def broken(*a, **kw):
        raise RuntimeError("prompt builder exploded")
    monkeypatch.setattr(S, "build_body", broken)
    evs = events((await _ask(h, cid)).text)
    assert _names(evs) == ["meta", "sources", "error"] and route.call_count == 0
    err = evs[-1][1]
    assert err["code"] == "internal_error" and "prompt builder exploded" in err["detail"]
    assert err["message"]["status"] == "error" and err["message"]["error_code"] == "internal_error"
    assert [m["role"] for m in await repo.list_messages(cid)] == ["user", "assistant"]
    assert not h.svc.chat.busy(cid)


# ── empty answers ───────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("mode", ["fast", "thinking"])
async def test_an_answer_with_no_text_is_upstream_error(ch, mock, mode):
    """Only thought parts (or nothing) came back: never a `done` with an empty answer."""
    h, repo = ch
    embed_ok(mock)
    gemini_stream(mock, chunks=[chunk(("Let me think.", True)), chunk(("", False), finish="MAX_TOKENS")])
    cid = await _conv(h)
    evs = events((await _ask(h, cid, model="gemini-3.5-flash", mode=mode)).text)
    assert _names(evs)[-1] == "error" and "done" not in _names(evs) and "delta" not in _names(evs)
    err = evs[-1][1]
    assert err["code"] == "upstream_error" and "finishReason MAX_TOKENS" in err["detail"]
    stored = (await repo.list_messages(cid))[-1]
    assert stored["status"] == "error" and stored["error_code"] == "upstream_error" and stored["content"] == ""
    assert stored["grounding"] is None
    assert (stored["thinking"] == "Let me think.") is (mode == "thinking")


# ── Stop ────────────────────────────────────────────────────────────────────────────────────────
async def test_stop_during_retrieval_cancels_the_queries_and_stores_one_stopped_turn(ch, mock):
    h, repo = ch
    embed_ok(mock)
    route = gemini_stream(mock)
    cancelled = asyncio.Event()

    async def stuck(*a, **kw):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
    repo.search_documents = stuck
    cid = await _conv(h)
    sent = await drive(h.app, _url(cid), {"content": "What about HX-217?"}, disconnect_after="meta")
    await h.svc.chat.drain()
    assert _names(events(body_text(sent))) == ["meta"]
    assert cancelled.is_set() and route.call_count == 0
    stored = await repo.list_messages(cid)
    assert [(m["role"], m["status"]) for m in stored] == [("user", "complete"), ("assistant", "stopped")]
    assert stored[1]["content"] == "" and stored[1]["error_code"] is None and stored[1]["sources"] == []
    assert not h.svc.chat.busy(cid)


async def test_stop_while_the_title_is_pending_keeps_one_complete_answer_and_the_title_lands(make_harness, mock):
    """`done` already stored the answer; the client leaves during the title call. No second (stopped) copy is
    stored, and the title task finishes in the background."""
    h, repo = chat_harness(make_harness, chat_title_model="gemini-3.1-flash-lite")
    embed_ok(mock)
    gemini_stream(mock)
    svc = h.svc.chat
    gate = asyncio.Event()

    async def slow_generate(model, body, **kw):
        await gate.wait()
        return "HX-217 gauge history"
    svc.gemini.generate = slow_generate
    cid = await _conv(h)
    sent = await drive(h.app, _url(cid), {"content": "What happened to HX-217?"}, disconnect_after="done")
    assert _names(events(body_text(sent)))[-1] == "done"
    gate.set()
    await svc.drain()
    stored = await repo.list_messages(cid)
    assert [(m["role"], m["status"]) for m in stored] == [("user", "complete"), ("assistant", "complete")]
    conv = await repo.get_conversation(cid)
    assert conv["title"] == "HX-217 gauge history" and conv["title_source"] == "model"
    assert not svc.busy(cid)
    await h.client.aclose()


async def test_a_stopped_partial_is_stored_exactly_once(ch, mock):
    h, repo = ch
    embed_ok(mock)

    async def endless():
        from .fakes_chat import sse_body
        yield sse_body([chunk(("HX-217 read 999 psi [incident:AN012] ", False))])
        await asyncio.Event().wait()
        yield b""
    gemini_stream(mock, stream=endless)
    cid = await _conv(h)
    await drive(h.app, _url(cid), {"content": "What about HX-217?"}, disconnect_after="delta")
    await h.svc.chat.drain()
    await h.svc.chat.drain()
    stored = await repo.list_messages(cid)
    assert [m["status"] for m in stored if m["role"] == "assistant"] == ["stopped"]
    g = stored[-1]["grounding"]
    assert g["cited_ids"] == ["incident:AN012"] and g["unverified_numbers"] == ["999"]   # computed on the partial
    assert stored[-1]["citations"] == ["incident:AN012"]


# ── edit / regenerate edges ─────────────────────────────────────────────────────────────────────
async def test_an_edit_id_from_another_conversation_is_422_and_touches_neither(ch, mock):
    h, repo = ch
    embed_ok(mock)
    gemini_stream(mock)
    a, b = await _conv(h), await _conv(h)
    await _ask(h, a, "question in A")
    await _ask(h, b, "question in B")
    foreign = (await repo.list_messages(a))[0]["message_id"]
    before_a, before_b = await repo.list_messages(a), await repo.list_messages(b)
    r = await _ask(h, b, "edited", edit_message_id=foreign)
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"
    assert await repo.list_messages(a) == before_a and await repo.list_messages(b) == before_b
    assert not h.svc.chat.busy(b)


async def test_regenerate_when_the_last_message_is_a_question_answers_it_without_deleting(ch, mock):
    """The answer to the last question was never stored (e.g. the backend restarted mid-turn)."""
    h, repo = ch
    embed_ok(mock)
    cap = []
    gemini_stream(mock, captured=cap)
    cid = await _conv(h)
    await repo.add_message(cid, {"role": "user", "content": "orphan question about M007"})
    evs = dict(events((await _ask(h, cid, "", regenerate=True)).text))
    assert evs["meta"]["user_message"]["content"] == "orphan question about M007"
    stored = await repo.list_messages(cid)
    assert [(m["seq"], m["role"]) for m in stored] == [(1, "user"), (2, "assistant")]
    assert cap[0]["body"]["contents"][-1]["parts"][0]["text"].endswith("Question: orphan question about M007")


async def test_regenerate_ignores_content_and_editing_the_first_question_re_titles_an_auto_chat(ch, mock):
    h, repo = ch
    embed_ok(mock)
    gemini_stream(mock)
    cid = await _conv(h)
    await _ask(h, cid, "first question")
    evs = dict(events((await _ask(h, cid, "IGNORED TEXT", regenerate=True)).text))
    assert evs["meta"]["user_message"]["content"] == "first question"
    first = (await repo.list_messages(cid))[0]
    evs = dict(events((await _ask(h, cid, "a better first question", edit_message_id=first["message_id"])).text))
    assert evs["done"]["conversation"]["title"] == "a better first question"
    assert evs["done"]["conversation"]["title_source"] == "auto"


# ── titles ──────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("value", ["none", "OFF", "off", "", "false", "0"])
def test_title_model_can_be_switched_off(monkeypatch, value):
    monkeypatch.setenv("CHAT_TITLE_MODEL", value)
    assert Settings.from_env().chat_title_model is None


async def test_with_titles_off_no_title_call_is_made(make_harness, mock):
    h, repo = chat_harness(make_harness, chat_title_model=None)
    embed_ok(mock)
    gemini_stream(mock)
    route = gemini_title(mock)
    cid = await _conv(h)
    evs = events((await _ask(h, cid)).text)
    assert "title" not in _names(evs) and route.call_count == 0
    conv = await repo.get_conversation(cid)
    assert conv["title_source"] == "auto" and conv["title"] == "What happened to HX-217's gauge?"
    await h.client.aclose()


async def test_a_gemma_title_model_gets_no_thinking_config(make_harness, mock):
    h, _ = chat_harness(make_harness, chat_title_model="gemma-4-26b-a4b-it")
    embed_ok(mock)
    gemini_stream(mock)
    cap = []
    gemini_title(mock, captured=cap)
    cid = await _conv(h)
    assert _names(events((await _ask(h, cid)).text))[-1] == "title"
    assert cap[0]["model"] == "gemma-4-26b-a4b-it" and cap[0]["body"]["generationConfig"] == {"maxOutputTokens": 40}
    await h.client.aclose()


async def test_a_rename_between_the_title_check_and_write_is_never_overwritten(make_harness, mock):
    h, repo = chat_harness(make_harness, chat_title_model="gemini-3.1-flash-lite")
    embed_ok(mock)
    gemini_stream(mock)
    gemini_title(mock, text="Model title")
    cid = await _conv(h)
    orig = repo.update_conversation

    async def update_conversation(conversation_id, **kw):
        if kw.get("title_source") == "model":                 # the operator's PATCH lands first
            await orig(conversation_id, title="Operator title", title_source="user")
        return await orig(conversation_id, **kw)
    repo.update_conversation = update_conversation
    await _ask(h, cid)
    conv = await repo.get_conversation(cid)
    await h.client.aclose()
    assert (conv["title"], conv["title_source"]) == ("Operator title", "user")


# ── validation boundaries ───────────────────────────────────────────────────────────────────────
async def test_content_length_boundary(ch, mock):
    h, _ = ch
    embed_ok(mock)
    gemini_stream(mock)
    cid = await _conv(h)
    ok = await _ask(h, cid, "x" * 8000)
    assert ok.status_code == 200 and _names(events(ok.text))[-1] == "done"
    too_long = await _ask(h, cid, "x" * 8001)
    assert too_long.status_code == 422 and too_long.json()["error"] == "invalid_request"


@pytest.mark.parametrize("title,status,expected", [
    ("t" * 120, 201, "t" * 120), ("t" * 121, 422, None), ("   ", 201, "New chat"), ("  Battery   study ", 201,
                                                                                    "Battery study")])
async def test_create_title_boundaries(ch, title, status, expected):
    h, _ = ch
    r = await h.client.post("/api/v1/chat/conversations", json={"title": title})
    assert r.status_code == status, r.text
    if status == 201:
        assert r.json()["title"] == expected
        assert r.json()["title_source"] == ("default" if expected == "New chat" else "user")
    else:
        assert r.json()["error"] == "invalid_request"


async def test_patch_title_boundary_and_unknown_fields(ch):
    h, _ = ch
    cid = await _conv(h)
    r = await h.client.patch("/api/v1/chat/conversations/%s" % cid, json={"title": "t" * 120})
    assert r.status_code == 200 and r.json()["title"] == "t" * 120 and r.json()["title_source"] == "user"
    r = await h.client.patch("/api/v1/chat/conversations/%s" % cid, json={"title_source": "model", "pinned": False})
    assert r.status_code == 200 and r.json()["title_source"] == "user"     # title_source can't be set directly


@pytest.mark.parametrize("params,status", [
    ({"limit": 0}, 422), ({"limit": 201}, 422), ({"limit": 200}, 200), ({"limit": 1}, 200),
    ({"offset": -1}, 422), ({"offset": 0}, 200), ({"offset": 10_000}, 200), ({"limit": "ten"}, 422),
    ({"q": "x" * 200}, 200), ({"q": "x" * 201}, 422),
])
async def test_list_query_bounds(ch, params, status):
    h, _ = ch
    r = await h.client.get("/api/v1/chat/conversations", params=params)
    assert r.status_code == status, r.text
    if status == 422:
        assert r.json()["error"] == "invalid_request"


@pytest.mark.parametrize("q,passed", [(None, None), ("", None), ("   ", None), ("  Battery M007 ", "Battery M007")])
async def test_q_is_trimmed_and_passed_through(ch, q, passed):
    h, repo = ch
    seen = []
    orig = repo.list_conversations

    async def spy(q=None, limit=50, offset=0):
        seen.append((q, limit, offset))
        return await orig(q=q, limit=limit, offset=offset)
    repo.list_conversations = spy
    params = {"limit": 7, "offset": 3}
    if q is not None:
        params["q"] = q
    assert (await h.client.get("/api/v1/chat/conversations", params=params)).status_code == 200
    assert seen == [(passed, 7, 3)]


@pytest.mark.parametrize("cid", ["{3F2B8C1E-9A4D-4E2F-8B7A-1C2D3E4F5A6B}", "3F2B8C1E-9A4D-4E2F-8B7A-1C2D3E4F5A6B",
                                 "urn:uuid:3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b"])
async def test_equivalent_uuid_spellings_reach_the_same_chat(ch, cid):
    h, repo = ch
    real = "3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b"
    repo.conversations[real] = {**(await repo.create_conversation(title="x", model="gemini-3.8-flash",
                                                                  mode="fast")), "conversation_id": real}
    repo.messages[real] = []
    r = await h.client.get("/api/v1/chat/conversations/%s" % cid)
    assert r.status_code == 200 and r.json()["conversation"]["conversation_id"] == real


@pytest.mark.parametrize("body", [{"content": 5}, {"content": None}, {"regenerate": "maybe"}, {"mode": "Fast"},
                                  {"edit_message_id": 12}])
async def test_message_body_types_are_validated(ch, body):
    h, repo = ch
    cid = await _conv(h)
    r = await h.client.post(_url(cid), json=body)
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"
    assert repo.messages[cid] == [] and not h.svc.chat.busy(cid)


async def test_a_non_json_message_body_is_422_not_500(ch):
    h, _ = ch
    cid = await _conv(h)
    r = await h.client.post(_url(cid), content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"


async def test_suggestions_and_models_need_no_conversation(ch):
    h, _ = ch
    assert (await h.client.get("/api/v1/chat/models")).status_code == 200
    r = await h.client.get("/api/v1/chat/suggestions")
    assert r.status_code == 200 and len(r.json()["suggestions"]) == 4


def test_sse_frames_are_compact_utf8_json():
    frame = S.sse("delta", {"delta": "≤ 25% — “ok”"})
    assert frame == 'event: delta\ndata: {"delta":"≤ 25% — “ok”"}\n\n'
    assert S.sse("x", {"t": httpx.URL("http://a")}).startswith("event: x\ndata: {\"t\":\"http://a\"}")
