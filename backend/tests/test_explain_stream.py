"""Streaming Qwen drafts, backend side (contract "Streaming Qwen drafts"): the SSE parser, System2Client.assess_stream
against a fake /v1/assess/stream, and POST /flags/{id}/explain/stream, whose `done` must equal POST /explain."""
import asyncio
import json

import httpx
import pytest

from app.services.flags import KEEPALIVE
from app.services.http import ServiceUnavailable
from app.services.system2 import SSEParser, StreamNotSupported, System2Client, sanitize_draft_fields

from .conftest import EMBED, MCP, S2
from .fakes import RecordingSink
from .test_fallbacks import _action_flag
from .test_orchestrator import _harness, orch_reply, orch_route

ANSWER = {"question": "Is it safe?", "answer": "No, escalate.", "evidence": [{"claim": "Gauge out of band", "ids":
          ["F1", "reading:R1"]}, {"claim": "Critical asset", "ids": ["asset:HX-217"]}],
          "recommended_action": "DISPATCH_TECHNICIAN", "confidence": "high", "model": "qwen-test", "path": "llm",
          "latency_ms": 900, "tokens_per_sec": 31.2, "insufficient_context": False}
DRAFTS = [{"call": 1, "tokens": 3, "fields": {"evidence": [{"claim": "Gauge out", "ids": []}]}},
          {"call": 1, "tokens": 9, "fields": {"evidence": [{"claim": "Gauge out of band", "ids": ["F1"]}],
                                              "answer": "No, esc"}}]


def frame(event, data):
    return "event: %s\ndata: %s\n\n" % (event, json.dumps(data))


def s2_body(response=ANSWER, drafts=DRAFTS, cache="miss", image=None, final=True, error=None):
    out = [frame("start", {"flag_id": 1, "request_id": "r", "model": "qwen-test", "mode": "live"}), ": keep-alive\n\n"]
    out += [frame("draft", d) for d in drafts]
    if error:
        out.append(frame("error", {"detail": error}))
    elif final:
        out.append(frame("final", {"response": response, "cache": cache, "image": image}))
    return "".join(out)


def sse_response(text, status=200):
    return httpx.Response(status, text=text, headers={"content-type": "text/event-stream"})


def parse(text):
    p = SSEParser()
    return [(e, json.loads(d)) for e, d in p.feed(text) + p.close()]


# ── the SSE parser ──────────────────────────────────────────────────────────────────────────────
def test_sse_parser_handles_any_chunking_and_line_ending():
    text = "event: draft\r\ndata: {\"a\":\r\ndata: 1}\r\n\r\n: keep-alive\r\r" \
           "event:final\ndata:{\"b\": 2}\n\nevent: x\rdata: {}\r\r"
    want = [("draft", "{\"a\":\n1}"), ("final", "{\"b\": 2}"), ("x", "{}")]
    for size in (1, 2, 3, 7, len(text)):
        p = SSEParser()
        got = []
        for i in range(0, len(text), size):
            got += p.feed(text[i:i + size])
        assert got + p.close() == want, size


def test_sse_parser_flushes_a_last_event_without_blank_line_and_ignores_empty():
    p = SSEParser()
    assert p.feed("event: start\n\nevent: final\ndata: {}") == []        # an event with no data is not dispatched
    assert p.close() == [("final", "{}")]
    p = SSEParser()
    assert p.feed("data: x\n\n") == [("message", "x")]


def test_sanitize_draft_fields_keeps_only_strings_and_listed_keys():
    out = sanitize_draft_fields({"evidence": [{"claim": "c", "ids": ["F1", 2, None]}, "junk", {"claim": 5},
                                              {"ids": ["F2"]}], "answer": "a", "confidence": 0.9,
                                 "recommended_action": {"x": 1}, "question": "q", "insufficient_context": False})
    assert out == {"evidence": [{"claim": "c", "ids": ["F1"]}, {"claim": "", "ids": ["F2"]}], "answer": "a"}
    assert sanitize_draft_fields("nope") == {} and sanitize_draft_fields({}) == {}


# ── System2Client.assess_stream ─────────────────────────────────────────────────────────────────
def client(handler, **kw):
    sink = RecordingSink()
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return System2Client(S2, http, sink, api_key="k", **kw), sink


async def test_assess_stream_relays_drafts_and_returns_final():
    seen = []

    def handler(request):
        seen.append(request)
        return sse_response(s2_body(cache="hit"))
    c, sink = client(handler)
    drafts = []
    out, meta = await c.assess_stream({"flag_id": 1}, "r-1", 1, "A1", drafts.append)
    assert out == ANSWER and drafts == DRAFTS
    assert meta == {"stream": True, "drafts": 2, "cache": "hit", "image": None, "mode": "live"}
    assert seen[0].url.path == "/v1/assess/stream" and seen[0].headers["x-api-key"] == "k"
    assert len(sink.model_calls) == 1
    call = sink.model_calls[0]
    assert call["ok"] and call["endpoint"] == "/v1/assess/stream" and call["model"] == "qwen-test"
    assert call["tokens_per_sec"] == 31.2 and call["flag_id"] == 1 and call["metadata"]["cache"] == "hit"


@pytest.mark.parametrize("case", ["error_event", "no_final", "http500", "connect", "bad_final"])
async def test_assess_stream_failures_are_service_unavailable_logged_once_and_trip_the_breaker(case):
    def handler(request):
        if case == "connect":
            raise httpx.ConnectError("refused")
        if case == "http500":
            return httpx.Response(500, text="boom")
        if case == "bad_final":
            return sse_response(frame("final", {"response": "nope"}))
        return sse_response(s2_body(error="model crashed" if case == "error_event" else None, final=False))
    c, sink = client(handler)
    with pytest.raises(ServiceUnavailable) as exc:
        await c.assess_stream({}, "r", 1, None, lambda d: None)
    assert {"error_event": "model crashed", "no_final": "without a final", "http500": "HTTP 500: boom",
            "connect": "ConnectError", "bad_final": "no response object"}[case] in exc.value.reason
    assert len(sink.model_calls) == 1 and not sink.model_calls[0]["ok"]
    assert c.breaker.state == "open"


@pytest.mark.parametrize("status", [404, 405])
async def test_assess_stream_404_means_old_build_not_down(status):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(status, json={"detail": "Not Found"})
    c, sink = client(handler)
    for _ in range(2):
        with pytest.raises(StreamNotSupported):
            await c.assess_stream({}, "r", 1, None, None)
    assert calls == ["/v1/assess/stream"]          # remembered: the second call does not ask again
    assert not sink.model_calls and c.breaker.state == "closed"


async def test_assess_stream_json_reply_is_accepted_as_the_final_answer():
    c, sink = client(lambda r: httpx.Response(200, json=ANSWER))       # a proxy or build that does not stream
    drafts = []
    out, meta = await c.assess_stream({"flag_id": 1}, "r", 1, None, drafts.append)
    assert out == ANSWER and drafts == [] and meta["stream"] is False and meta["drafts"] == 0
    assert len(sink.model_calls) == 1 and sink.model_calls[0]["ok"] and c.breaker.state == "closed"
    c, sink = client(lambda r: httpx.Response(200, json=["not", "an", "object"]))
    with pytest.raises(ServiceUnavailable, match="not an object"):
        await c.assess_stream({}, "r", 1, None, None)


async def test_assess_stream_cancelled_logs_a_row_without_tripping_the_breaker():
    async def hang(request):
        await asyncio.sleep(30)
    c, sink = client(hang)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(c.assess_stream({}, "r", 7, "A1", None), 0.1)   # deadline or client disconnect
    assert len(sink.model_calls) == 1
    call = sink.model_calls[0]
    assert not call["ok"] and call["error"] == "cancelled" and call["flag_id"] == 7
    assert c.breaker.state == "closed"


async def test_assess_stream_503_busy_is_not_down_and_not_remembered():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(503, json={"detail": "too many concurrent streams; retry shortly or use /v1/assess"})
    c, sink = client(handler)
    for _ in range(2):
        with pytest.raises(StreamNotSupported, match="busy"):
            await c.assess_stream({}, "r", 1, None, None)
    assert calls == ["/v1/assess/stream"] * 2          # asked again: a slot may be free next time
    assert not sink.model_calls and c.breaker.state == "closed"


async def test_assess_stream_unset_and_open_breaker():
    c, _ = client(lambda r: sse_response(s2_body()))
    c.base_url = None
    with pytest.raises(ServiceUnavailable, match="SYSTEM2_URL is not set"):
        await c.assess_stream({}, "r", 1, None, None)
    c, _ = client(lambda r: sse_response(s2_body()))
    c.breaker.failure("earlier")
    with pytest.raises(ServiceUnavailable, match="circuit open"):
        await c.assess_stream({}, "r", 1, None, None)


# ── /flags/{id}/explain/stream ──────────────────────────────────────────────────────────────────
def s2_stream_route(mock, body=None, status=200, calls=None):
    def handler(request):
        if calls is not None:
            calls.append(json.loads(request.content))
        if status != 200:
            return httpx.Response(status, json={"detail": "Not Found"})
        return sse_response(body if body is not None else s2_body())
    return mock.post(S2 + "/v1/assess/stream").mock(side_effect=handler)


def s2_assess_route(mock, response=ANSWER, calls=None):
    def handler(request):
        if calls is not None:
            calls.append(json.loads(request.content))
        return httpx.Response(200, json=response)
    return mock.post(S2 + "/v1/assess").mock(side_effect=handler)


def common_mocks(mock):
    mock.post(MCP).mock(side_effect=httpx.ConnectError("refused"))           # SQL context packet
    mock.post(EMBED + "/v1/embeddings").mock(return_value=httpx.Response(
        200, json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3, 0.4]}], "model": "m"}))


async def stream(h, fid, refresh=False):
    r = await h.client.post("/api/v1/flags/%d/explain/stream" % fid, params={"refresh": str(refresh).lower()})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache, no-transform" and r.headers["x-accel-buffering"] == "no"
    return parse(r.text)


async def twin(make_harness, mock, **kw):
    """Two identical harnesses (fresh fake DBs, same mocks): one streams, one calls POST /explain."""
    a, b = make_harness(**kw), make_harness(**kw)
    fa, fb = await _action_flag(a, mock), await _action_flag(b, mock)
    assert fa == fb
    return a, b, fa


async def plain(h, fid, refresh=False):
    r = await h.client.post("/api/v1/flags/%d/explain" % fid, params={"refresh": str(refresh).lower()})
    assert r.status_code == 200, r.text
    return r.json()


def names(events):
    return [e if e != "stage" else "stage:" + d["stage"] for e, d in events]


async def test_stream_model_answer_with_drafts_equals_explain(make_harness, mock):
    a, b, fid = await twin(make_harness, mock, system2_url=S2)
    common_mocks(mock)
    stream_calls, assess_calls = [], []
    s2_stream_route(mock, calls=stream_calls)
    s2_assess_route(mock, calls=assess_calls)
    events = await stream(a, fid)
    assert names(events) == ["start", "stage:context", "stage:system2", "draft", "draft", "stage:storing", "done"]
    assert events[0][1]["flag_id"] == fid and events[0][1]["request_id"]
    d = events[4][1]
    assert d == {"call": 1, "fields": {"evidence": [{"claim": "Gauge out of band", "ids": ["F1"]}],
                                       "answer": "No, esc"}, "reasoning": "Gauge out of band [F1]"}
    expected = await plain(b, fid)
    assert events[-1][1]["explanation"] == expected
    assert expected["source"] == "system2" and expected["cached"] is False
    assert expected["reasoning"] == "Gauge out of band [F1, reading:R1] Critical asset [asset:HX-217]"
    assert len(stream_calls) == 1 and len(assess_calls) == 1                  # same body as /v1/assess
    assert set(stream_calls[0]) == set(assess_calls[0]) == {"flag_id", "request_id", "flag", "event", "reasons",
                                                             "context"}
    assert stream_calls[0]["context"]["question"] == assess_calls[0]["context"]["question"]
    # only the checked answer is stored and indexed; one model_calls row for the streamed assessment
    assert a.repo.assessments[fid]["answer"] == "No, escalate." and a.repo.assessments[fid]["state"] == "DONE"
    await a.sink.drain()
    assert a.repo.documents["explanation:%d" % fid]["content"].startswith("Q: Is it safe? A: No, escalate.")
    rows = [c for c in a.sink.model_calls if c["system"] == "system2"]
    assert len(rows) == 1 and rows[0]["endpoint"] == "/v1/assess/stream" and rows[0]["ok"]
    again = await stream(a, fid)                                    # now DONE: cached, no drafts
    assert names(again) == ["start", "stage:cached", "done"] and again[-1][1]["explanation"]["cached"] is True


async def test_stream_system2_template_path_equals_explain(make_harness, mock):
    a, b, fid = await twin(make_harness, mock, system2_url=S2)
    common_mocks(mock)
    tmpl = {**ANSWER, "path": "fallback", "model": "template"}
    s2_stream_route(mock, s2_body(response=tmpl))
    s2_assess_route(mock, tmpl)
    events = await stream(a, fid)
    assert "draft" in names(events) and names(events)[-1] == "done"     # drafts shown, then the checked template
    expected = await plain(b, fid)
    assert events[-1][1]["explanation"] == expected and expected["source"] == "system2_template"
    assert a.repo.assessments[fid]["state"] == "TEMPLATE"
    await a.sink.drain()
    assert not [k for k in a.repo.documents if k.startswith("explanation:")]


async def test_stream_system2_down_is_template_equal_to_explain(make_harness, mock):
    a, b, fid = await twin(make_harness, mock, system2_url=S2)
    common_mocks(mock)
    mock.post(S2 + "/v1/assess/stream").mock(side_effect=httpx.ConnectError("refused"))
    mock.post(S2 + "/v1/assess").mock(side_effect=httpx.ConnectError("refused"))
    events = await stream(a, fid)
    assert names(events) == ["start", "stage:context", "stage:system2", "stage:storing", "done"]
    expected = await plain(b, fid)
    assert events[-1][1]["explanation"] == expected
    assert expected["source"] == "template" and "ConnectError" in expected["fallback_reason"]


async def test_stream_unset_system2_is_template_equal_to_explain(make_harness, mock):
    a, b, fid = await twin(make_harness, mock)
    common_mocks(mock)
    events = await stream(a, fid)
    expected = await plain(b, fid)
    assert events[-1][1]["explanation"] == expected and "SYSTEM2_URL is not set" in expected["fallback_reason"]


async def test_stream_404_falls_back_to_assess_without_drafts(make_harness, mock):
    a, b, fid = await twin(make_harness, mock, system2_url=S2)
    common_mocks(mock)
    stream_route = s2_stream_route(mock, status=404)
    assess_calls = []
    s2_assess_route(mock, calls=assess_calls)
    events = await stream(a, fid)
    assert names(events) == ["start", "stage:context", "stage:system2", "stage:storing", "done"]
    assert stream_route.call_count == 1 and len(assess_calls) == 1
    expected = await plain(b, fid)
    assert events[-1][1]["explanation"] == expected and expected["source"] == "system2"
    rows = [c for c in a.sink.model_calls if c["system"] == "system2"]
    assert [r["endpoint"] for r in rows] == ["/v1/assess"]


async def test_stream_deadline_gives_template_after_drafts(make_harness, mock):
    a, b, fid = await twin(make_harness, mock, system2_url=S2, request_deadline_s=1.0, system2_timeout_s=60.0)
    common_mocks(mock)

    async def slow_chunks():
        yield s2_body(final=False).encode()
        await asyncio.sleep(30)

    async def hang(request):
        await asyncio.sleep(30)
    mock.post(S2 + "/v1/assess/stream").mock(return_value=httpx.Response(
        200, content=slow_chunks(), headers={"content-type": "text/event-stream"}))
    mock.post(S2 + "/v1/assess").mock(side_effect=hang)
    t0 = asyncio.get_running_loop().time()
    events = await stream(a, fid)
    assert asyncio.get_running_loop().time() - t0 < 2.0
    assert names(events) == ["start", "stage:context", "stage:system2", "draft", "draft", "stage:storing", "done"]
    got = events[-1][1]["explanation"]
    assert got["source"] == "template" and got["fallback_reason"].startswith("deadline: System 2")
    expected = await plain(b, fid)
    assert {k: v for k, v in got.items() if k != "fallback_reason"} == \
        {k: v for k, v in expected.items() if k != "fallback_reason"}      # the budget in the text may differ by ms
    assert a.repo.assessments[fid]["state"] == "TEMPLATE"


async def test_stream_orchestrator_path_sends_no_drafts_and_equals_explain(make_harness, mock):
    a, b = _harness(make_harness, system2_url=S2), _harness(make_harness, system2_url=S2)
    fid = await _action_flag(a, mock)
    assert await _action_flag(b, mock) == fid
    common_mocks(mock)
    orch_route(mock, lambda body: orch_reply(body["flag_id"]))
    s2 = s2_stream_route(mock)
    events = await stream(a, fid)
    assert names(events) == ["start", "stage:system2", "stage:storing", "done"] and not s2.called
    expected = await plain(b, fid)
    assert events[-1][1]["explanation"] == expected and expected["orchestrator"]["route"] == "full"


async def test_stream_refresh_bypasses_cache(make_harness, mock):
    h = make_harness(system2_url=S2)
    fid = await _action_flag(h, mock)
    common_mocks(mock)
    calls = []
    s2_stream_route(mock, calls=calls)
    await stream(h, fid)
    assert names(await stream(h, fid))[1] == "stage:cached"
    assert names(await stream(h, fid, refresh=True)).count("draft") == 2 and len(calls) == 2


async def test_stream_errors_before_start_are_json(make_harness, mock):
    h = make_harness(request_deadline_s=0.5)
    r = await h.client.post("/api/v1/flags/999/explain/stream")
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    fid = await _action_flag(h, mock)

    async def slow_get_flag(flag_id):
        await asyncio.sleep(30)
    h.repo.get_flag = slow_get_flag
    r = await h.client.post("/api/v1/flags/%d/explain/stream" % fid)
    assert r.status_code == 504 and r.json()["error"] == "deadline_exceeded"


async def test_stream_error_after_start_is_an_error_event(make_harness, mock):
    h = make_harness()
    fid = await _action_flag(h, mock)
    common_mocks(mock)

    async def broken_store(*a, **kw):
        raise RuntimeError("disk on fire")
    h.svc.flags._store = broken_store
    events = await stream(h, fid)
    assert names(events)[0] == "start" and events[-1] == ("error", {"error": "internal_error",
                                                                   "detail": "RuntimeError: disk on fire"})


async def test_drafts_arrive_while_system2_is_still_generating(make_harness, mock):
    """The service relays each draft as it comes (not after the final): the fake System 2 only sends `final` after
    the test has seen the first draft. Also: keep-alives while nothing happens."""
    h = make_harness(system2_url=S2)
    fid = await _action_flag(h, mock)
    common_mocks(mock)
    gate = asyncio.Event()

    async def chunks():
        yield s2_body(drafts=DRAFTS[:1], final=False).encode()
        await gate.wait()
        yield frame("final", {"response": ANSWER, "cache": "miss", "image": None}).encode()
    mock.post(S2 + "/v1/assess/stream").mock(return_value=httpx.Response(
        200, content=chunks(), headers={"content-type": "text/event-stream"}))
    h.svc.flags.keepalive_s = 0.05
    events = []

    async def consume():
        it = await h.svc.flags.explain_stream(fid, "r-test")
        async for ev in it:
            events.append(ev)
            if ev[0] == "draft":                   # only now may System 2 finish; meanwhile: keep-alives
                asyncio.get_running_loop().call_later(0.2, gate.set)
    await asyncio.wait_for(consume(), 5.0)
    kinds = [e for e, _ in events]
    assert kinds[0] == "start" and kinds[-1] == "done" and KEEPALIVE[0] in kinds
    assert kinds.index("draft") < kinds.index("done")
    assert events[-1][1]["explanation"]["answer"] == "No, escalate."


async def test_router_sends_keep_alive_comments(make_harness, mock):
    h = make_harness(system2_url=S2)
    fid = await _action_flag(h, mock)
    common_mocks(mock)

    async def chunks():
        yield s2_body(final=False).encode()
        await asyncio.sleep(0.2)
        yield frame("final", {"response": ANSWER, "cache": "miss", "image": None}).encode()
    mock.post(S2 + "/v1/assess/stream").mock(return_value=httpx.Response(
        200, content=chunks(), headers={"content-type": "text/event-stream"}))
    h.svc.flags.keepalive_s = 0.05
    r = await h.client.post("/api/v1/flags/%d/explain/stream" % fid)
    assert ": keep-alive\n\n" in r.text and parse(r.text)[-1][0] == "done"


async def test_serverless_stream_waits_for_indexing_before_it_ends(make_harness, mock):
    h = make_harness(system2_url=S2, serverless=True)
    fid = await _action_flag(h, mock)
    mock.post(MCP).mock(side_effect=httpx.ConnectError("refused"))

    async def slow_embed(request):
        await asyncio.sleep(0.3)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3, 0.4]}], "model": "m"})
    mock.post(EMBED + "/v1/embeddings").mock(side_effect=slow_embed)
    s2_stream_route(mock)
    events = await stream(h, fid)
    assert events[-1][1]["explanation"]["source"] == "system2"
    assert "explanation:%d" % fid in h.repo.documents          # no sink.drain() here: the stream waited for it
