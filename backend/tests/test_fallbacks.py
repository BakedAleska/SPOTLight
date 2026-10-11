"""Every fallback on the demo path: MCP, System 2, embedding service, System 1 proxy/forecast, database."""
import asyncio
import json
import time

import httpx
import pytest

from .conftest import EMBED, MCP, S1, S2, laya_answers, replay_event, system1_route


async def _flag(h, mock, event_id="E00022"):
    """Ingest an escalating event (M001 mission) with Laya mocked; return the flag_id."""
    system1_route(mock, lambda it: laya_answers(it["kind"], event=it["event"]))
    res = (await h.client.post("/api/v1/events", json=replay_event(event_id))).json()
    return res["flag"]["flag_id"]


async def _action_flag(h, mock):
    from .test_ingest import _pick
    raw, _ = _pick(True)
    system1_route(mock, lambda it: laya_answers(it["kind"], event=it["event"]))
    res = (await h.client.post("/api/v1/events", json=raw)).json()
    return res["flag"]["flag_id"]


def mcp_server(mock, packet=None, fail=False):
    def handler(request: httpx.Request):
        if fail:
            return httpx.Response(503, text="down")
        msg = json.loads(request.content)
        if msg.get("method") == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "t", "version": "1"}}},
                headers={"mcp-session-id": "s1"})
        if "id" not in msg:
            return httpx.Response(202)
        assert msg["params"]["name"] == "get_flag_context"
        sse = "event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "content": [{"type": "text", "text": json.dumps(packet)}], "structuredContent": packet,
            "isError": False}}) + "\n\n"
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
    mock.post(MCP).mock(side_effect=handler)
    mock.delete(MCP).mock(return_value=httpx.Response(200))


# ── /context ────────────────────────────────────────────────────────────────────────────────────
async def test_context_from_mcp(h, mock):
    fid = await _action_flag(h, mock)
    mcp_server(mock, {"packet_version": "1", "flag": {"id": "flag:%d" % fid}, "question": "q?"})
    r = await h.client.get("/api/v1/flags/%d/context" % fid)
    assert r.status_code == 200 and r.json()["source"] == "mcp" and r.json()["question"] == "q?"


async def test_mcp_client_sends_auth_on_every_request_including_the_session_delete(mock):
    """An MCP server with MCP_AUTH_TOKEN checks the bearer on DELETE too; without it the close got a 401."""
    from app.services.mcp import MCPClient
    mcp_server(mock, {"packet_version": "1"})
    async with httpx.AsyncClient() as http:
        out = await MCPClient(MCP, http, auth_token="tok").call_tool("get_flag_context", {"flag_id": 1}, "r1")
    assert out["packet_version"] == "1"
    sent = [c.request for c in mock.calls]
    assert [r.method for r in sent] == ["POST", "POST", "POST", "DELETE"]
    assert all(r.headers.get("authorization") == "Bearer tok" for r in sent)
    assert sent[-1].headers["mcp-session-id"] == "s1"


@pytest.mark.parametrize("how", ["http503", "connect"])
async def test_context_falls_back_to_sql_packet(h, mock, how):
    fid = await _action_flag(h, mock)
    if how == "connect":
        mock.post(MCP).mock(side_effect=httpx.ConnectError("refused"))
    else:
        mcp_server(mock, fail=True)
    r = await h.client.get("/api/v1/flags/%d/context" % fid)
    body = r.json()
    assert r.status_code == 200 and body["source"] == "sql_fallback"
    assert body["event"]["asset"]["id"].startswith("asset:") and body["facts"] and body["question"]
    assert "MCP unavailable" in body["gaps"][0]


async def test_context_404_for_missing_flag(h):
    r = await h.client.get("/api/v1/flags/999/context")
    assert r.status_code == 404 and r.json()["error"] == "not_found"


# ── /explain ────────────────────────────────────────────────────────────────────────────────────
async def test_explain_template_when_system2_unset(h, mock):
    fid = await _action_flag(h, mock)
    mock.post(MCP).mock(side_effect=httpx.ConnectError("refused"))
    r = await h.client.post("/api/v1/flags/%d/explain" % fid)
    body = r.json()
    assert r.status_code == 200 and body["source"] == "template" and body["model"] == "template"
    for k in ("question", "answer", "reasoning", "recommended_action"):
        assert body[k]
    assert "Laya: escalate" in body["reasoning"]
    assert "SYSTEM2_URL is not set" in body["fallback_reason"]
    assert h.repo.assessments[fid]["model"] == "template"
    assert h.repo.assessments[fid]["state"] == "TEMPLATE"          # audit M10: not stored as a final DONE
    again = (await h.client.get("/api/v1/flags/%d/explain" % fid)).json()
    assert again["cached"] is True and again["source"] == "template"


async def test_explain_template_when_system2_down(make_harness, mock):
    h = make_harness(system2_url=S2)
    fid = await _action_flag(h, mock)
    mock.post(MCP).mock(side_effect=httpx.ConnectError("refused"))
    mock.post(S2 + "/v1/assess").mock(side_effect=httpx.ConnectError("refused"))
    body = (await h.client.post("/api/v1/flags/%d/explain" % fid)).json()
    assert body["source"] == "template" and "ConnectError" in body["fallback_reason"]
    assert any(c["system"] == "system2" and not c["ok"] for c in h.sink.model_calls)


async def test_explain_from_system2(make_harness, mock):
    h = make_harness(system2_url=S2)
    fid = await _action_flag(h, mock)
    mcp_server(mock, {"packet_version": "1", "question": "q?"})
    seen = []

    def s2(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"question": "Is it safe?", "answer": "No, escalate.", "reasoning": "F1, F2",
                                         "recommended_action": "DISPATCH_TECHNICIAN", "confidence": 0.8,
                                         "model": "qwen-test", "latency_ms": 900, "tokens_per_sec": 31.2})
    mock.post(S2 + "/v1/assess").mock(side_effect=s2)
    mock.post(EMBED + "/v1/embeddings").mock(side_effect=httpx.ConnectError("refused"))
    body = (await h.client.post("/api/v1/flags/%d/explain" % fid)).json()
    assert body["source"] == "system2" and body["answer"] == "No, escalate." and body["context_source"] == "mcp"
    assert seen[0]["flag_id"] == fid and seen[0]["context"]["question"] == "q?"
    assert h.repo.assessments[fid]["model"] == "qwen-test"
    call = next(c for c in h.sink.model_calls if c["system"] == "system2")
    assert call["ok"] and call["tokens_per_sec"] == 31.2 and call["flag_id"] == fid


# ── audit M10: templates are not final and are never indexed ───────────────────────────────────
def _s2_ok(mock, model="qwen-test"):
    calls = []

    def s2(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"question": "Is it safe?", "answer": "No, escalate.", "reasoning": "F1",
                                         "recommended_action": "DISPATCH_TECHNICIAN", "model": model})
    return calls, s2


async def test_template_is_retried_once_system2_is_back(make_harness, mock):
    h = make_harness(system2_url=S2)
    fid = await _action_flag(h, mock)
    mock.post(MCP).mock(side_effect=httpx.ConnectError("refused"))
    emb = mock.post(EMBED + "/v1/embeddings").mock(return_value=httpx.Response(
        200, json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3, 0.4]}], "model": "m"}))
    s2_route = mock.post(S2 + "/v1/assess").mock(side_effect=httpx.ConnectError("refused"))
    first = (await h.client.post("/api/v1/flags/%d/explain" % fid)).json()
    assert first["source"] == "template" and h.repo.assessments[fid]["state"] == "TEMPLATE"
    await h.sink.drain()
    assert not any(k.startswith("explanation:") for k in h.repo.documents) and not emb.called   # not indexed

    h.svc.system2.breaker.reset()
    calls, s2 = _s2_ok(mock)
    s2_route.mock(side_effect=s2)
    again = (await h.client.get("/api/v1/flags/%d/explain" % fid)).json()   # no refresh: the template is not final
    assert again["source"] == "system2" and again["cached"] is False and calls
    assert h.repo.assessments[fid]["state"] == "DONE" and h.repo.assessments[fid]["model"] == "qwen-test"
    await h.sink.drain()
    assert "explanation:%d" % again["assessment_id"] in h.repo.documents                   # real ones are indexed
    final = (await h.client.get("/api/v1/flags/%d/explain" % fid)).json()
    assert final["cached"] is True and final["source"] == "system2" and len(calls) == 1      # DONE is final


async def test_legacy_done_template_row_is_retried_when_system2_configured(make_harness, mock):
    """Rows written before M10 (state DONE, model 'template') are treated as templates too."""
    h = make_harness(system2_url=S2)
    fid = await _action_flag(h, mock)
    await h.repo.upsert_assessment(fid, {"answer": "old template", "model": "template"}, state="DONE")
    mock.post(MCP).mock(side_effect=httpx.ConnectError("refused"))
    mock.post(EMBED + "/v1/embeddings").mock(side_effect=httpx.ConnectError("refused"))
    calls, s2 = _s2_ok(mock)
    mock.post(S2 + "/v1/assess").mock(side_effect=s2)
    body = (await h.client.get("/api/v1/flags/%d/explain" % fid)).json()
    assert body["source"] == "system2" and calls


# ── audit M4: one overall deadline on /context and /explain ────────────────────────────────────
async def _hang(request):
    await asyncio.sleep(30)
    return httpx.Response(200)


async def test_context_deadline_returns_sql_packet(make_harness, mock):
    h = make_harness(request_deadline_s=1.0, mcp_timeout_s=60.0)    # per-call timeout far past the deadline
    fid = await _action_flag(h, mock)
    mock.post(MCP).mock(side_effect=_hang)
    t0 = time.perf_counter()
    r = await h.client.get("/api/v1/flags/%d/context" % fid)
    elapsed = time.perf_counter() - t0
    body = r.json()
    assert r.status_code == 200 and elapsed < 2.0, elapsed
    assert body["source"] == "sql_fallback" and body["fallback_reason"].startswith("deadline:")
    assert body["gaps"][0].startswith("deadline:") and body["event"]["asset"]["id"].startswith("asset:")


async def test_explain_deadline_returns_template(make_harness, mock):
    h = make_harness(request_deadline_s=1.0, mcp_timeout_s=60.0, system2_url=S2, system2_timeout_s=60.0)
    fid = await _action_flag(h, mock)
    mock.post(MCP).mock(side_effect=_hang)
    mock.post(S2 + "/v1/assess").mock(side_effect=_hang)
    t0 = time.perf_counter()
    r = await h.client.post("/api/v1/flags/%d/explain" % fid)
    elapsed = time.perf_counter() - t0
    body = r.json()
    assert r.status_code == 200 and elapsed < 2.0, elapsed
    assert body["source"] == "template" and body["fallback_reason"].startswith("deadline: System 2")
    assert body["context_source"] == "sql_fallback"
    assert h.repo.assessments[fid]["state"] == "TEMPLATE"


async def test_deadline_on_the_flag_read_is_a_504_not_a_hang(make_harness, mock):
    h = make_harness(request_deadline_s=0.5)
    fid = await _action_flag(h, mock)

    async def slow_get_flag(flag_id):
        await asyncio.sleep(30)
    h.repo.get_flag = slow_get_flag
    t0 = time.perf_counter()
    for url in ("/api/v1/flags/%d/context" % fid, "/api/v1/flags/%d/explain" % fid):
        r = await h.client.request("GET", url)
        assert r.status_code == 504 and r.json()["error"] == "deadline_exceeded", r.text
    assert time.perf_counter() - t0 < 2.0


def test_request_deadline_from_env(monkeypatch):
    from app.config import Settings
    monkeypatch.delenv("REQUEST_DEADLINE_S", raising=False)
    assert Settings.from_env().request_deadline_s == 45.0
    monkeypatch.setenv("REQUEST_DEADLINE_S", "12.5")
    assert Settings.from_env().request_deadline_s == 12.5


# ── /ack ────────────────────────────────────────────────────────────────────────────────────────
async def test_ack_indexes_operator_note_when_embed_up(h, mock):
    fid = await _action_flag(h, mock)
    sent = []

    def emb(request):
        body = json.loads(request.content)
        sent.append(body)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3, 0.4]}],
                                         "model": "m", "usage": {"prompt_tokens": 12}})
    mock.post(EMBED + "/v1/embeddings").mock(side_effect=emb)
    r = await h.client.post("/api/v1/flags/%d/ack" % fid, json={"action": "RESOLVE", "note": "Gauge recalibrated",
                                                                 "operator": "tj"})
    body = r.json()
    assert r.status_code == 200 and body["rag_indexed"] is True and body["flag_status"] == "RESOLVED"
    doc = h.repo.documents[body["doc_id"]]
    assert body["doc_id"] == "note:%d" % body["ack"]["ack_id"] and doc["kind"] == "operator_note"
    assert "Gauge recalibrated" in doc["content"] and doc["embedding"].startswith("[0.1")
    assert sent[0]["input"][0] == doc["content"]                     # documents get no query prefix
    assert h.repo.flags[fid]["status"] == "RESOLVED"


async def test_ack_survives_embed_down(h, mock):
    fid = await _action_flag(h, mock)
    mock.post(EMBED + "/v1/embeddings").mock(side_effect=httpx.ConnectError("refused"))
    body = (await h.client.post("/api/v1/flags/%d/ack" % fid, json={"note": "looked at it"})).json()
    assert body["rag_indexed"] is False and "embedding service unavailable" in body["rag_error"]
    assert h.repo.acks and not h.repo.documents and body["flag_status"] == "ACKNOWLEDGED"


# ── /search ─────────────────────────────────────────────────────────────────────────────────────
async def test_search_hybrid_uses_query_prefix(h, mock):
    sent = []

    def emb(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 0, 0, 0]}]})
    mock.post(EMBED + "/v1/embeddings").mock(side_effect=emb)
    body = (await h.client.get("/api/v1/search", params={"q": "HX-204 gauge", "kinds": "incident,policy"})).json()
    assert body["mode"] == "hybrid" and body["hits"][0]["doc_id"] == "incident:AN002"
    assert sent[0]["input"][0].startswith("Instruct:") and sent[0]["input"][0].endswith("HX-204 gauge")
    assert h.repo.last_search[0] == "hybrid" and h.repo.last_search[2] == "HX-204 gauge"


@pytest.mark.parametrize("how", ["connect", "bad_dim", "timeout"])
async def test_search_falls_back_to_fulltext(h, mock, how):
    route = mock.post(EMBED + "/v1/embeddings")
    seen = []
    if how == "connect":
        route.mock(side_effect=httpx.ConnectError("refused"))
    elif how == "timeout":
        def slow(request):
            seen.append(request.extensions["timeout"]["read"])
            raise httpx.ReadTimeout("slow CPU embedder")
        route.mock(side_effect=slow)
    else:
        route.mock(return_value=httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 0]}]}))
    body = (await h.client.get("/api/v1/search", params={"q": "battery"})).json()
    assert body["mode"] == "fulltext" and body["hits"] and body["fallback_reason"]
    if how == "timeout":
        assert seen == [h.settings.search_embed_timeout_s] and "ReadTimeout" in body["fallback_reason"]


async def test_search_rejects_unknown_kind(h):
    r = await h.client.get("/api/v1/search", params={"q": "x", "kinds": "nope"})
    assert r.status_code == 422


# ── System 1 proxy / forecast ───────────────────────────────────────────────────────────────────
async def test_predict_proxy_and_rules_fallback(h, mock):
    system1_route(mock, lambda it: laya_answers(it["kind"], event=it["event"]))
    ok = (await h.client.post("/api/v1/system1/predict", json={"items": [{"action_id": "A0009", "kind": "action"}]}))
    assert ok.json()["source"] == "system1" and ok.json()["items"][0]["answers"]["escalate"]
    mock.post(S1 + "/v1/predict").mock(side_effect=httpx.ConnectError("refused"))
    fb = (await h.client.post("/api/v1/system1/predict", json={"items": [{"action_id": "A0009", "kind": "action"}]}))
    body = fb.json()
    assert fb.status_code == 200 and body["source"] == "rules_fallback"
    assert set(body["items"][0]["answers"]) == {"escalate", "priority", "category", "reading_trust"}


async def test_forecast_live_then_baseline_when_laya_down(h, mock):
    system1_route(mock, lambda it: laya_answers(it["kind"]))
    body = (await h.client.get("/api/v1/system1/forecast", params={"mission_id": "M001"})).json()
    assert body["system1"] == "ok" and len(body["items"]) == 10
    # audit H2 (integration log 2026-10-10): at most FORECAST_LIVE_MAX (5) live Laya items per request
    assert all(i["source"] == "system1_live" and i["p_fail"] == 0.64 for i in body["items"][:5])
    assert all(i["source"] == "baseline" for i in body["items"][5:])
    assert body["items"][0]["actual"]["status"] == "FAILED"
    # second call reads the stored predictions, even with Laya down
    mock.post(S1 + "/v1/predict").mock(side_effect=httpx.ConnectError("refused"))
    body = (await h.client.get("/api/v1/system1/forecast", params={"mission_id": "M001"})).json()
    assert all(i["source"] == "system1_stored" for i in body["items"][:5])
    body = (await h.client.get("/api/v1/system1/forecast", params={"mission_id": "M002"})).json()
    assert body["system1"] == "laya_unavailable" and all(i["source"] == "baseline" for i in body["items"])


# ── debrief / database down ─────────────────────────────────────────────────────────────────────
async def test_debrief_template(h):
    body = (await h.client.get("/api/v1/missions/M017/debrief")).json()
    assert body["source"] == "template" and "M017" in body["summary"] and body["recommendations"]


async def test_read_endpoints_503_not_500_when_db_down(h):
    h.repo.down = True
    for url in ("/api/v1/flags/1", "/api/v1/missions", "/api/v1/search?q=x"):
        r = await h.client.get(url)
        assert r.status_code == 503 and r.json()["error"] == "database_unavailable", url


async def test_health_shallow_and_deep(h, mock):
    mock.get(S1 + "/health").mock(return_value=httpx.Response(200, json={"ok": True, "model_loaded": True}))
    mock.get(EMBED + "/health").mock(side_effect=httpx.ConnectError("refused"))
    mock.get("http://mcp.test/healthz").mock(return_value=httpx.Response(200, json={"ok": True}))
    r = await h.client.get("/health", params={"deep": "false"})
    assert r.status_code == 200 and r.json()["ok"] and "system1" not in r.json()
    body = (await h.client.get("/health")).json()
    assert body["system1"]["status"] == "up" and body["embed"]["status"] == "down"
    assert body["mcp"]["status"] == "up" and body["system2"]["status"] == "unset"
    assert body["database"]["status"] == "unset"
