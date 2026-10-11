"""What the backend writes to the app.* log tables (fix plan PKG-A): no secrets in error strings, env values stripped,
app.request_log without the dashboard's poll noise but with the error for handled 4xx/5xx, and embedding calls that
record the GPU from the server's timing block."""
import json

import httpx
import pytest

from app.config import Settings, log_requests_mode
from app.main import _log_text, should_log_request
from app.services.calllog import LogSink, redact
from app.services.chat.gemini import GeminiClient, GeminiError
from app.services.embed import EmbedClient

from .conftest import EMBED, laya_answers, replay_event, system1_route
from .fakes import RecordingSink

# Fake credentials in the two formats the redaction patterns know (Gemini's AQ. token and the classic AIza key).
AQ_KEY = "AQ.Ab8RN6unit-test-not-a-real-key-0123456789abcdefXYZ"
AIZA_KEY = "AIzaSyUNIT-test-not-a-real-key-012345"
W7900D = "AMD Radeon Pro W7900D"


# ── secrets ─────────────────────────────────────────────────────────────────────────────────────
def test_env_values_are_stripped_so_a_key_never_carries_a_newline(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", AQ_KEY + "\n")
    monkeypatch.setenv("ADMIN_TOKEN", "  tok \n")
    s = Settings.from_env()
    assert s.gemini_api_key == AQ_KEY and s.admin_token == "tok"
    monkeypatch.setenv("GEMINI_API_KEY", " \n")
    assert Settings.from_env().gemini_api_key is None


@pytest.mark.parametrize("key", [AQ_KEY, AIZA_KEY, "plain-unit-key-without-a-known-prefix"])
def test_scrub_removes_a_key_given_with_a_trailing_newline(key):
    c = GeminiClient(key + "\n", "http://gemini.unit/v1beta", None, None)
    assert c._headers()["x-goog-api-key"] == key            # the header itself is now valid
    leaked = "Gemini call failed: LocalProtocolError: Illegal header value b'%s\\n'" % key
    out = c.scrub(leaked)
    assert key not in out and "\\n'" not in out
    assert out == "Gemini call failed: LocalProtocolError: Illegal header value [redacted]"
    assert key not in c.scrub("raw %s\n and escaped %s" % (key, repr((key + "\n").encode())))


async def test_an_illegal_header_error_never_reaches_the_model_call_log():
    """The 21 leaked rows: h11 rejected 'KEY\\n' and printed the header bytes, key included."""
    def boom(request):
        raise httpx.LocalProtocolError("Illegal header value b'%s\\n'" % AQ_KEY)
    sink = RecordingSink()
    http = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    c = GeminiClient(AQ_KEY + "\n", "http://gemini.unit/v1beta", http, sink)
    with pytest.raises(GeminiError) as e:
        await c.stream("gemini-3.8-flash", {}, lambda t, s: None, request_id="r-leak")
    await http.aclose()
    assert e.value.code == "upstream_error" and AQ_KEY not in e.value.detail
    assert sink.model_calls[-1]["error"].endswith("Illegal header value [redacted]")
    assert AQ_KEY not in repr(sink.model_calls)


@pytest.mark.parametrize("text, expected", [
    ("LocalProtocolError: Illegal header value b'%s\\n'" % AQ_KEY,
     "LocalProtocolError: Illegal header value [redacted]"),
    ('Illegal header value b"it\'s %s"' % AQ_KEY, "Illegal header value [redacted]"),
    ("token %s here" % AQ_KEY, "token [redacted] here"),
    ("token %s here" % AIZA_KEY, "token [redacted] here"),
    ("x-goog-api-key: s3cr3t-value done", "x-goog-api-key: [redacted] done"),
    ("Authorization: Bearer abc.def", "Authorization: [redacted]"),
    ("sent Bearer abc.def upstream", "sent Bearer [redacted] upstream"),
    ('{"api_key": "zzz"}', '{"api_key": [redacted]'),
    ("GET /v1/models?key=s3cr3t&alt=sse", "GET /v1/models?key=[redacted]&alt=sse"),
    ("Authorization: Basic dXNlcjpwYXNz", "Authorization: [redacted]"),
    # an error already cut short by short_error's cap: no closing quote, still nothing of the key survives
    ("LocalProtocolError: Illegal header value b'%s" % AQ_KEY[:12], "LocalProtocolError: Illegal header value [redacted]"),
    # ordinary errors pass untouched (the Gemini auth error mapping still finds "API key")
    ("Gemini rejected the API key (HTTP 400): API key not valid", None),
    ("HTTP 503: retry in 41.53s", None),
])
def test_redact_patterns(text, expected):
    assert redact(text) == (text if expected is None else expected)
    assert redact(redact(text)) == redact(text)               # idempotent: scrub() and the sink both apply it
    assert redact(None) is None


class _StubDB:
    """Records what LogSink would insert."""
    configured = True

    def __init__(self):
        self.params = []

    async def fetchone(self, sql, params):
        self.params.append(params)
        return {"run_id": 1}


async def test_log_sink_redacts_every_error_string_before_storing():
    db = _StubDB()
    sink = LogSink(db)          # type: ignore[arg-type]
    leak = "LocalProtocolError: Illegal header value b'%s\\n'" % AQ_KEY
    sink.model_call(system="chat", ok=False, error=leak + " " + "x" * 600)
    sink.request(request_id="r-1", route="/api/v1/x", method="POST", status=500, latency_ms=3, error=leak)
    await sink.drain()
    await sink.job_finish(1, "error", {}, error="upstream said x-goog-api-key: %s" % AIZA_KEY)
    blob = repr(db.params)
    assert AQ_KEY not in blob and AIZA_KEY not in blob and blob.count("[redacted]") == 3
    model_error = db.params[0][12]
    assert model_error.startswith("LocalProtocolError: Illegal header value [redacted]") and len(model_error) == 500


def test_a_whitespace_only_key_is_not_a_secret():
    c = GeminiClient(" \n", "http://gemini.unit/v1beta", None, None)
    assert not c.configured and c._secrets == []
    assert c.scrub("a b\nc") == "a b\nc"


def test_request_log_error_is_redacted_before_the_200_character_cut():
    """Cutting first would keep 'AIzaSyUNIT-test-not' (15 characters after AIza, too short for the AIza pattern)."""
    out = _log_text("upstream_error", "x" * 180 + " " + AIZA_KEY)
    assert "AIzaSy" not in out and out.endswith("[redacted]")
    assert len(out) <= len("upstream_error: ") + 200


# ── app.request_log ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("value, mode", [(None, "writes"), ("", "writes"), ("1", "writes"), ("true", "writes"),
                                         ("writes", "writes"), ("0", "off"), ("false", "off"), ("no", "off"),
                                         ("OFF", "off"), ("all", "all"), (" All\n", "all")])
def test_log_requests_env_values(monkeypatch, value, mode):
    assert log_requests_mode(value) == mode
    if value is None:
        monkeypatch.delenv("LOG_REQUESTS", raising=False)
    else:
        monkeypatch.setenv("LOG_REQUESTS", value)
    monkeypatch.setenv("LOG_SLOW_MS", "250")
    s = Settings.from_env()
    assert s.log_requests_mode == mode and s.log_slow_ms == 250


@pytest.mark.parametrize("value, ms", [("250.0", 250), ("1s", 1000), ("nan", 1000), ("  ", 1000), ("0", 0)])
def test_log_slow_ms_is_lenient(monkeypatch, value, ms):
    monkeypatch.setenv("LOG_SLOW_MS", value)
    assert Settings.from_env().log_slow_ms == ms


def test_legacy_log_requests_bool_still_works():
    assert Settings(log_requests=False).log_requests_mode == "off"
    assert Settings(log_requests=True).log_requests_mode == "writes"
    assert Settings().log_requests_mode == "writes" and Settings().log_slow_ms == 1000
    with pytest.raises(ValueError):
        Settings(log_requests_mode="sometimes")


@pytest.mark.parametrize("args, logged", [
    (("writes", "/api/v1/flags/7", "GET", 200, 12, True), False),          # dashboard poll
    (("writes", "/api/v1/flags", "HEAD", 200, 3, True), False),
    (("writes", "/api/v1/flags", "OPTIONS", 200, 1, True), False),         # CORS preflight
    (("writes", "/api/v1/flags/7/ack", "POST", 200, 30, True), True),
    (("writes", "/api/v1/live/runs/3", "DELETE", 204, 9, True), True),
    (("writes", "/api/v1/flags/7", "GET", 404, 5, True), True),            # a real API 404
    (("writes", "/api/v1/flags/7", "GET", 503, 5, True), True),
    (("writes", "/api/v1/search", "GET", 200, 1000, True), True),          # slow read (LOG_SLOW_MS=1000)
    (("writes", "/favicon.ico", "GET", 404, 1, False), False),             # unmatched probes
    (("writes", "/", "HEAD", 404, 1, False), False),
    (("writes", "/api/v1/nope", "POST", 404, 1, False), False),
    (("writes", "/health", "POST", 500, 1, True), False),
    (("writes", "/api/v1/live/runs/3/stops/2/draft", "POST", 200, 15, True), False),   # draft relay, ~2/s per stop
    (("writes", "/api/v1/live/runs/3/stops/2/draft", "POST", 409, 15, True), True),
    (("writes", "/api/v1/live/runs/3/stops/2/draft", "POST", 200, 1000, True), True),  # slow
    (("all", "/api/v1/live/runs/3/stops/2/draft", "POST", 200, 15, True), True),
    (("all", "/api/v1/flags/7", "GET", 200, 12, True), True),
    (("all", "/favicon.ico", "GET", 404, 1, False), True),
    (("all", "/docs", "GET", 200, 1, True), False),
    (("off", "/api/v1/flags/7/ack", "POST", 500, 30, True), False),
])
def test_should_log_request(args, logged):
    assert should_log_request(*args, slow_ms=1000) is logged


async def _flag_id(h, mock) -> int:
    system1_route(mock, lambda it: laya_answers(it["kind"], event=it["event"]))
    res = (await h.client.post("/api/v1/events", json=replay_event("E00022"))).json()
    return res["flag"]["flag_id"]


async def test_writes_mode_skips_polls_and_logs_writes_and_api_errors(make_harness, mock):
    h = make_harness(log_requests_mode="writes")
    fid = await _flag_id(h, mock)
    h.sink.requests.clear()

    for path in ("/api/v1/flags/%d" % fid, "/api/v1/flags", "/favicon.ico", "/"):
        await h.client.get(path)
    await h.client.head("/")
    assert h.sink.requests == []                        # GET 200 polls and unmatched 404 probes

    r = await h.client.post("/api/v1/events/batch", json={"events": [replay_event("E00001")]},
                            headers={"X-Request-ID": "r-batch"})
    assert r.status_code == 200
    req = h.sink.requests[-1]
    assert (req["route"], req["method"], req["status"], req["error"]) == ("/api/v1/events/batch", "POST", 200, None)
    assert req["request_id"] == "r-batch"

    r = await h.client.get("/api/v1/flags/999999")
    assert r.status_code == 404
    req = h.sink.requests[-1]
    assert (req["route"], req["status"]) == ("/api/v1/flags/{flag_id}", 404)
    assert req["error"] == "not_found: no flag with flag_id=999999"


async def test_handled_errors_fill_request_log_error(make_harness, mock):
    h = make_harness(log_requests_mode="writes")
    # A router's HTTPException: the response is FastAPI's default, the log row gets code + detail.
    r = await h.client.get("/api/v1/missions/NOPE-1")
    assert r.status_code == 404 and r.json() == {"detail": "no mission NOPE-1"}
    assert h.sink.requests[-1]["error"] == "not_found: no mission NOPE-1"
    # Request validation (422)
    r = await h.client.post("/api/v1/events/batch", json={"events": []})
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"
    err = h.sink.requests[-1]["error"]
    assert err.startswith("invalid_request: [") and len(err) <= len("invalid_request: ") + 200
    # An admin-token failure (AdminAuthError handler)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url="http://backend") as anon:
        r = await anon.post("/api/v1/events", json=replay_event("E00001"))
    assert r.status_code in (401, 403)
    assert h.sink.requests[-1]["status"] == r.status_code
    assert h.sink.requests[-1]["error"] == "%s: %s" % (r.json()["error"], r.json()["detail"])


async def test_an_error_response_that_bypasses_the_handlers_still_gets_an_error(make_harness):
    """routers/chat.py builds its 404 JSONResponse itself (no handler, no request.state.log_error)."""
    h = make_harness(log_requests_mode="writes")
    r = await h.client.get("/api/v1/chat/conversations/not-a-uuid")
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    req = h.sink.requests[-1]
    assert (req["route"], req["status"], req["error"]) == ("/api/v1/chat/conversations/{conversation_id}", 404,
                                                           "not_found: HTTP 404")


async def test_an_unhandled_error_is_logged_as_internal_error_without_secrets(make_harness):
    h = make_harness(log_requests_mode="writes")

    async def boom(**kw):
        raise RuntimeError("upstream x-goog-api-key: %s" % AIZA_KEY)
    h.repo.list_flags = boom
    transport = httpx.ASGITransport(app=h.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://backend") as c:
        r = await c.get("/api/v1/flags")
    assert r.status_code == 500 and r.json()["error"] == "internal_error"
    req = h.sink.requests[-1]
    assert (req["route"], req["status"]) == ("/api/v1/flags", 500)
    assert req["error"] == "internal_error: RuntimeError: upstream x-goog-api-key: [redacted]"
    assert AIZA_KEY not in repr(h.sink.requests)


async def test_slow_reads_are_logged(make_harness):
    h = make_harness(log_requests_mode="writes", log_slow_ms=0)
    await h.client.get("/api/v1/flags")
    assert [(q["route"], q["method"], q["error"]) for q in h.sink.requests] == [("/api/v1/flags", "GET", None)]


async def test_all_and_off_modes(make_harness):
    h = make_harness(log_requests_mode="all")
    await h.client.get("/api/v1/flags")
    await h.client.get("/favicon.ico")
    await h.client.get("/health")
    assert [(q["route"], q["status"]) for q in h.sink.requests] == [("/api/v1/flags", 200), ("/favicon.ico", 404)]
    h = make_harness(log_requests_mode="off")
    await h.client.post("/api/v1/events/batch", json={"events": []})
    assert h.sink.requests == []


# ── embedding telemetry ─────────────────────────────────────────────────────────────────────────
def _embed_client(body, sent):
    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=body)
    sink = RecordingSink()
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return EmbedClient(EMBED, "Qwen/Qwen3-Embedding-0.6B", 4, "Instruct: find things\nQuery:", "", http, sink), sink


async def test_embed_logs_the_gpu_and_server_timing():
    sent = []
    body = {"model": "Qwen/Qwen3-Embedding-0.6B", "data": [{"index": 0, "embedding": [1, 0, 0, 0]}],
            "usage": {"prompt_tokens": 40, "total_tokens": 40},
            "timing": {"ms": 21.5, "tokens_per_sec": 1860.5, "device": "cuda", "dtype": "bfloat16", "gpu": W7900D}}
    c, sink = _embed_client(body, sent)
    await c.embed_query("HX-204 gauge", request_id="r-emb")
    assert sent[0]["input"] == ["Instruct: find things\nQuery:HX-204 gauge"]      # no space after "Query:"
    call = sink.model_calls[-1]
    assert (call["system"], call["ok"], call["endpoint"]) == ("embed", True, "/v1/embeddings")
    assert call["device"] == W7900D and call["tokens_per_sec"] == 1860.5 and call["input_tokens"] == 40
    assert call["metadata"] == {"purpose": "query", "inputs": 1, "server_ms": 21.5}
    await c.http.aclose()


async def test_embed_without_a_timing_block_falls_back():
    sent = []
    body = {"data": [{"index": 0, "embedding": [1, 0, 0, 0]}], "usage": {"prompt_tokens": 40},
            "timing": {"device": "cpu"}}
    c, sink = _embed_client(body, sent)
    await c.embed_documents(["note"])
    call = sink.model_calls[-1]
    assert call["device"] == "cpu" and "server_ms" not in call["metadata"]
    assert call["tokens_per_sec"] is None or call["tokens_per_sec"] > 0          # wall clock (None if 0 ms)
    body.pop("timing")
    await c.embed_documents(["note"])
    assert sink.model_calls[-1]["device"] is None
    await c.http.aclose()
