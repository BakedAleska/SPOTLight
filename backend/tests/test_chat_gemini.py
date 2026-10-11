"""GeminiClient unit gaps (docs/chat-contract.md §0, §4 error codes, §5): SSE parsing across arbitrary network chunk
boundaries, finishReason / promptFeedback blocks, error-code mapping (401/403/404/429/503, array-wrapped bodies),
`retryDelay` formats, timeouts for the stream and the title call, thought parts in `generate`, and the key staying in
the header. Driven through httpx.MockTransport, so no network and no respx."""
import asyncio
import json
import random

import httpx
import pytest

from app.services.chat.gemini import BLOCKED_FINISH, GeminiClient, GeminiError, parse_retry_delay

from .fakes import RecordingSink
from .fakes_chat import answer_chunks, chunk, sse_body

KEY = "AIzaUNIT-secret-key-9876543210"
BASE = "http://gemini.unit/v1beta"


def client(handler, timeout_s: float = 120.0):
    sink = RecordingSink()
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GeminiClient(KEY, BASE, http, sink, timeout_s), sink


def streaming(pieces, delay: float = 0.0):
    async def gen():
        for p in pieces:
            if delay:
                await asyncio.sleep(delay)
            yield p
    return gen()


async def run(c, body=None, **kw):
    parts = []
    res = await c.stream("gemini-3.8-flash", body or {"contents": []}, lambda th, t: parts.append((th, t)),
                         request_id="rq-1", **kw)
    return res, parts


# ── SSE across chunk boundaries ─────────────────────────────────────────────────────────────────
GOLDEN = answer_chunks(parts=["Grounded ✓ — HX-217 read 412.5 psi ", "[incident:AN012]", " and “62.5%” [stat:fleet]."],
                       thoughts=["Thinking about the gauge… "])


def _split(data: bytes, rng: random.Random):
    cuts = sorted(rng.sample(range(1, len(data)), rng.randint(1, min(40, len(data) - 1))))
    return [data[a:b] for a, b in zip([0] + cuts, cuts + [len(data)])]


@pytest.mark.parametrize("newline", ["\r\n", "\n"])
async def test_sse_parses_identically_for_any_chunk_split(newline):
    raw = sse_body(GOLDEN).replace(b"\r\n", newline.encode())

    async def parse(pieces):
        c, _ = client(lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"},
                                                 content=streaming(pieces)))
        return await run(c)

    whole_res, whole = await parse([raw])
    assert [t for th, t in whole if not th] == ["Grounded ✓ — HX-217 read 412.5 psi ", "[incident:AN012]",
                                                " and “62.5%” [stat:fleet]."]
    assert whole[0] == (True, "Thinking about the gauge… ") and whole_res.finish_reason == "STOP"
    rng = random.Random(1010)
    for _ in range(60):
        res, parts = await parse(_split(raw, rng))
        assert parts == whole and res.usage == whole_res.usage and res.finish_reason == "STOP"
    res, parts = await parse([raw[i:i + 1] for i in range(len(raw))])           # one byte per chunk
    assert parts == whole


async def test_a_final_event_without_its_blank_line_is_still_read():
    raw = sse_body(answer_chunks(parts=["a ", "b"])).rstrip(b"\r\n")
    c, _ = client(lambda req: httpx.Response(200, content=raw))
    res, parts = await run(c)
    assert [t for _, t in parts] == ["a ", "b"] and res.usage["prompt_tokens"] == 3100


async def test_multi_line_data_and_comments_and_other_fields():
    payload = json.dumps(chunk(("x", False), finish="STOP"), indent=1)            # JSON across several data lines
    raw = (": hello\r\nevent: message\r\nid: 1\r\n" + "".join("data: %s\r\n" % ln for ln in payload.splitlines())
           + "\r\n").encode()
    c, _ = client(lambda req: httpx.Response(200, content=raw))
    _, parts = await run(c)
    assert parts == [(False, "x")]


async def test_empty_parts_and_candidate_less_chunks_are_skipped():
    body = [{"candidates": []}, {"usageMetadata": {"promptTokenCount": 5}},
            chunk(("", False)), {"candidates": [{"content": {"parts": [{"thoughtSignature": "abc"}]}}]},
            chunk(("done", False), finish="STOP")]
    c, _ = client(lambda req: httpx.Response(200, content=sse_body(body)))
    res, parts = await run(c)
    assert parts == [(False, "done")] and res.usage["prompt_tokens"] == 5


# ── blocks ──────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("reason", sorted(BLOCKED_FINISH))
async def test_blocked_finish_reasons(reason):
    c, sink = client(lambda req: httpx.Response(200, content=sse_body([chunk(("Part", False), finish=reason)])))
    parts = []
    with pytest.raises(GeminiError) as e:
        await c.stream("m", {}, lambda th, t: parts.append(t), request_id=None)
    assert e.value.code == "blocked" and reason in e.value.detail and parts == ["Part"]
    assert sink.model_calls[-1]["ok"] is False and sink.model_calls[-1]["metadata"]["code"] == "blocked"


@pytest.mark.parametrize("reason", ["MAX_TOKENS", "STOP", "OTHER"])
async def test_non_block_finish_reasons_end_normally(reason):
    c, _ = client(lambda req: httpx.Response(200, content=sse_body([chunk(("ok", False), finish=reason)])))
    res, _ = await run(c)
    assert res.finish_reason == reason


async def test_prompt_feedback_block_is_blocked():
    c, _ = client(lambda req: httpx.Response(200, content=sse_body([{"promptFeedback": {"blockReason": "SAFETY"}}])))
    with pytest.raises(GeminiError) as e:
        await run(c)
    assert e.value.code == "blocked" and "SAFETY" in e.value.detail


# ── error mapping ───────────────────────────────────────────────────────────────────────────────
def err(code, message, status=None, details=None):
    e = {"code": code, "message": message}
    if status:
        e["status"] = status
    if details:
        e["details"] = details
    return {"error": e}


@pytest.mark.parametrize("status,body,code", [
    (401, err(401, "Request had invalid authentication credentials.", "UNAUTHENTICATED"), "gemini_auth"),
    (403, err(403, "Method doesn't allow unregistered callers.", "PERMISSION_DENIED"), "gemini_auth"),
    (400, err(400, "API key expired. Please renew the API key."), "gemini_auth"),
    (404, err(404, "models/gemini-9 is not found for API version v1beta"), "model_unavailable"),
    (400, err(400, "Developer instruction is not enabled for models/gemma-3"), "model_unavailable"),
    (400, err(400, "Request contains an invalid argument."), "upstream_error"),
    (429, err(429, "Resource has been exhausted", "RESOURCE_EXHAUSTED"), "rate_limited"),
    (503, err(503, "The model is overloaded. Please try again later.", "UNAVAILABLE"), "model_unavailable"),
    (500, err(500, "An internal error has occurred.", "INTERNAL"), "upstream_error"),
    (502, "<html>Bad Gateway</html>", "upstream_error"),
])
async def test_http_status_codes_map_to_contract_codes(status, body, code):
    content = body.encode() if isinstance(body, str) else json.dumps(body).encode()
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(status, content=content)
    c, sink = client(handler)
    with pytest.raises(GeminiError) as e:
        await run(c)
    assert e.value.code == code and e.value.http_status == status
    assert len(calls) == 1                                               # one attempt, no retry
    assert sink.model_calls[-1]["metadata"]["http_status"] == status


async def test_the_streaming_endpoints_array_wrapped_error_body_is_unwrapped():
    body = [err(429, "Quota exceeded.", "RESOURCE_EXHAUSTED",
                [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "17s"}])]
    c, _ = client(lambda req: httpx.Response(429, json=body))
    with pytest.raises(GeminiError) as e:
        await run(c)
    assert e.value.code == "rate_limited" and e.value.retry_after_s == 17


@pytest.mark.parametrize("delay,expected", [("41s", 41), ("1.5s", 2), ("0.2s", 1), ("41.53s", 42), ("0s", 1),
                                            ("41", None), ("", None), (None, None), ("soon", None)])
def test_retry_delay_formats(delay, expected):
    assert parse_retry_delay(delay) == expected


@pytest.mark.parametrize("details,message,expected", [
    ([{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "1.5s"}], "Quota exceeded.", 2),
    ([{"@type": "type.googleapis.com/google.rpc.QuotaFailure"}], "Please retry in 12.1s.", 13),
    (None, "Quota exceeded.", 60),                                      # nothing to go on: a minute
    ([{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "bogus"}], "Please retry in 3s.", 3),
])
def test_rate_limit_retry_after_sources(details, message, expected):
    c = GeminiClient(KEY, BASE, None, None)
    e = c.map_http_error(429, json.dumps(err(429, message, "RESOURCE_EXHAUSTED", details)).encode(), "m")
    assert e.code == "rate_limited" and e.retry_after_s == expected


async def test_an_in_stream_error_after_text_maps_and_keeps_the_partial():
    body = [chunk(("half ", False)), err(500, "Internal error", "INTERNAL")]
    c, _ = client(lambda req: httpx.Response(200, content=sse_body(body)))
    parts = []
    with pytest.raises(GeminiError) as e:
        await c.stream("m", {}, lambda th, t: parts.append(t), request_id=None)
    assert e.value.code == "upstream_error" and parts == ["half "]


async def test_malformed_sse_json_is_upstream_error():
    c, _ = client(lambda req: httpx.Response(200, content=b"data: {not json\r\n\r\n"))
    with pytest.raises(GeminiError) as e:
        await run(c)
    assert e.value.code == "upstream_error"


# ── timeouts and transport ──────────────────────────────────────────────────────────────────────
async def test_a_stream_that_outlives_the_timeout_is_upstream_error():
    raw = sse_body(answer_chunks())
    c, sink = client(lambda req: httpx.Response(200, content=streaming([raw[:40], raw[40:]], delay=0.3)),
                     timeout_s=0.1)
    with pytest.raises(GeminiError) as e:
        await run(c)
    assert e.value.code == "upstream_error" and "CHAT_TIMEOUT_S" in e.value.detail
    assert sink.model_calls[-1]["ok"] is False and sink.model_calls[-1]["metadata"]["code"] == "upstream_error"


async def test_the_title_call_times_out_as_upstream_error():
    async def slow(req):
        await asyncio.sleep(0.5)
        return httpx.Response(200, json={})
    c, _ = client(slow)
    with pytest.raises(GeminiError) as e:
        await c.generate("gemini-3.1-flash-lite", {}, request_id=None, timeout_s=0.05)
    assert e.value.code == "upstream_error" and "0.05" in e.value.detail


async def test_a_transport_error_never_leaks_the_key():
    def boom(req):
        raise httpx.ConnectError("could not connect with key=%s" % KEY)
    c, sink = client(boom)
    with pytest.raises(GeminiError) as e:
        await run(c)
    assert e.value.code == "upstream_error" and KEY not in e.value.detail
    assert KEY not in repr(sink.model_calls)


async def test_cancellation_is_logged_as_stopped_and_re_raised():
    started = asyncio.Event()

    async def endless():
        yield sse_body([chunk(("so far ", False))])
        started.set()
        await asyncio.sleep(30)
        yield b""
    c, sink = client(lambda req: httpx.Response(200, content=endless()))
    task = asyncio.ensure_future(run(c))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sink.model_calls[-1]["metadata"]["code"] == "stopped" and sink.model_calls[-1]["ok"] is False


# ── generate (titles) ───────────────────────────────────────────────────────────────────────────
async def test_generate_drops_thought_parts_and_uses_the_header_key():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"text": "I should keep it short. ", "thought": True}, {"text": "HX-217 "}, {"text": "gauge"}]},
            "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 3}})
    c, sink = client(handler)
    out = await c.generate("gemini-3.1-flash-lite", {"contents": []}, request_id="r", timeout_s=2,
                           metadata={"purpose": "title"})
    assert out == "HX-217 gauge"
    (req,) = seen
    assert str(req.url) == BASE + "/models/gemini-3.1-flash-lite:generateContent"
    assert KEY not in str(req.url) and req.headers["x-goog-api-key"] == KEY
    assert sink.model_calls[-1]["endpoint"] == "gemini:generateContent" and sink.model_calls[-1]["ok"]


@pytest.mark.parametrize("payload", [{}, {"candidates": []}, {"candidates": [{}]},
                                     {"candidates": [{"content": {"parts": []}}]}])
async def test_generate_with_no_text_returns_empty(payload):
    c, _ = client(lambda req: httpx.Response(200, json=payload))
    assert await c.generate("m", {}, request_id=None, timeout_s=2) == ""


async def test_stream_url_and_headers():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, content=sse_body(answer_chunks()))
    c, _ = client(handler)
    await run(c, body={"contents": [{"role": "user", "parts": [{"text": "q"}]}]})
    (req,) = seen
    assert str(req.url) == BASE + "/models/gemini-3.8-flash:streamGenerateContent?alt=sse"
    assert req.headers["x-goog-api-key"] == KEY and KEY not in str(req.url)
    assert json.loads(req.content) == {"contents": [{"role": "user", "parts": [{"text": "q"}]}]}


async def test_no_key_raises_gemini_unset_without_calling():
    calls = []
    c = GeminiClient(None, BASE, httpx.AsyncClient(transport=httpx.MockTransport(lambda r: calls.append(r))),
                     RecordingSink())
    with pytest.raises(GeminiError) as e:
        await run(c)
    with pytest.raises(GeminiError) as e2:
        await c.generate("m", {}, request_id=None, timeout_s=1)
    assert e.value.code == e2.value.code == "gemini_unset" and calls == [] and not c.configured
