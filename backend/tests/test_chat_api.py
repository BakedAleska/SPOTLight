"""/api/v1/chat CRUD, the model catalog and starter prompts (docs/chat-contract.md §4, §7)."""
import pytest

from app.config import Settings
from app.services.chat.catalog import thinking_config

from .fakes_chat import chat_harness


@pytest.fixture
async def ch(make_harness):
    h, repo = chat_harness(make_harness)
    yield h, repo
    await h.client.aclose()


async def test_models_follow_chat_models_order_with_labels(make_harness):
    h, _ = chat_harness(make_harness, chat_models=["gemma-4-31b-it", "gemini-3.5-flash-lite", "gemini-9-ultra"],
                        chat_default_model="gemini-3.8-flash")
    out = (await h.client.get("/api/v1/chat/models")).json()
    assert out["configured"] is True and out["default_mode"] == "fast"
    assert [m["id"] for m in out["models"]] == ["gemma-4-31b-it", "gemini-3.5-flash-lite", "gemini-9-ultra"]
    assert out["default_model"] == "gemma-4-31b-it"            # CHAT_DEFAULT_MODEL not offered -> first offered
    gemma, lite, extra = out["models"]
    assert gemma["family"] == "gemma" and gemma["label"] == "Gemma 4 31B" and gemma["description"]
    assert lite["family"] == "gemini" and lite["thinking_summaries"] is False
    assert extra["label"] and extra["family"] == "gemini"      # an id from the env works without a code change
    assert [m["id"] for m in out["modes"]] == ["fast", "thinking"]
    await h.client.aclose()


async def test_default_models_are_the_contract_list():
    s = Settings()
    assert s.chat_models == ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"]   # chat.2: gemma opt-in
    assert s.chat_default_model == "gemini-3.8-flash" and s.chat_title_model == "gemini-3.1-flash-lite"
    assert (s.chat_timeout_s, s.chat_retrieval_budget_s, s.chat_history_messages) == (120.0, 8.0, 12)
    assert s.gemini_base_url == "https://generativelanguage.googleapis.com/v1beta" and s.gemini_api_key is None


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k-secret-1234")
    monkeypatch.setenv("CHAT_MODELS", " gemini-3.5-flash , gemma-4-26b-a4b-it ,")
    monkeypatch.setenv("CHAT_TITLE_MODEL", "none")
    monkeypatch.setenv("GEMINI_BASE_URL", "http://g.test/v1beta/")
    s = Settings.from_env()
    assert s.chat_models == ["gemini-3.5-flash", "gemma-4-26b-a4b-it"] and s.chat_title_model is None
    assert s.gemini_base_url == "http://g.test/v1beta" and s.gemini_api_key == "k-secret-1234"
    assert "k-secret-1234" not in repr(s)
    monkeypatch.delenv("CHAT_TITLE_MODEL")
    assert Settings.from_env().chat_title_model == "gemini-3.1-flash-lite"


def test_thinking_config_per_family():
    assert thinking_config("gemini-3.8-flash", "fast") == {"thinkingLevel": "low"}
    assert thinking_config("gemini-3.5-flash-lite", "thinking") == {"thinkingLevel": "high", "includeThoughts": True}
    assert thinking_config("gemma-4-31b-it", "fast") is None and thinking_config("gemma-4-31b-it", "thinking") is None


async def test_crud_round_trip(ch):
    h, repo = ch
    r = await h.client.post("/api/v1/chat/conversations", json={})
    assert r.status_code == 201
    c = r.json()
    assert c["title"] == "New chat" and c["title_source"] == "default" and c["model"] == "gemini-3.8-flash"
    assert c["mode"] == "fast" and c["pinned"] is False and c["message_count"] == 0
    cid = c["conversation_id"]
    r2 = await h.client.post("/api/v1/chat/conversations", json={"title": "Battery study", "mode": "thinking",
                                                                 "model": "gemini-3.5-flash"})
    c2 = r2.json()
    assert c2["title_source"] == "user" and c2["mode"] == "thinking" and c2["model"] == "gemini-3.5-flash"
    for conv_id in (cid, c2["conversation_id"]):      # chat.2: only conversations with messages are listed
        await repo.add_message(conv_id, {"role": "user", "content": "first question"})
    # rename + pin
    p = await h.client.patch("/api/v1/chat/conversations/%s" % cid, json={"title": "  HX-217   gauge ", "pinned": True})
    assert p.status_code == 200 and p.json()["title"] == "HX-217 gauge" and p.json()["title_source"] == "user"
    assert p.json()["pinned"] is True
    lst = (await h.client.get("/api/v1/chat/conversations")).json()
    assert lst["total"] == 2 and lst["items"][0]["conversation_id"] == cid            # pinned first
    assert "last_message_preview" in lst["items"][0]
    assert (await h.client.get("/api/v1/chat/conversations", params={"q": "battery"})).json()["total"] == 1
    got = (await h.client.get("/api/v1/chat/conversations/%s" % cid)).json()
    assert got["conversation"]["conversation_id"] == cid and [m["content"] for m in got["messages"]] == [
        "first question"]
    assert got["messages"][0]["error_code"] is None
    # model/mode change, then an empty patch returns the conversation unchanged
    m = await h.client.patch("/api/v1/chat/conversations/%s" % cid, json={"model": "gemini-3.5-flash-lite",
                                                                          "mode": "thinking"})
    assert m.json()["model"] == "gemini-3.5-flash-lite" and m.json()["mode"] == "thinking"
    gemma = await h.client.patch("/api/v1/chat/conversations/%s" % cid, json={"model": "gemma-4-31b-it"})
    assert gemma.status_code == 422                                      # not in the default CHAT_MODELS
    assert (await h.client.patch("/api/v1/chat/conversations/%s" % cid, json={})).json()["title"] == "HX-217 gauge"
    d = await h.client.delete("/api/v1/chat/conversations/%s" % cid)
    assert d.status_code == 204 and d.content == b""
    assert (await h.client.get("/api/v1/chat/conversations/%s" % cid)).status_code == 404
    assert (await h.client.delete("/api/v1/chat/conversations/%s" % cid)).status_code == 404


@pytest.mark.parametrize("method,suffix,body", [
    ("get", "", None), ("patch", "", {"pinned": True}), ("delete", "", None),
    ("post", "/messages", {"content": "hi"})])
@pytest.mark.parametrize("cid", ["not-a-uuid", "123", "00000000-0000-0000-0000-000000000000"])
async def test_bad_or_unknown_ids_are_404(ch, method, suffix, body, cid):
    h, _ = ch
    kw = {"json": body} if body is not None else {}
    r = await getattr(h.client, method)("/api/v1/chat/conversations/%s%s" % (cid, suffix), **kw)
    assert r.status_code == 404 and r.json()["error"] == "not_found" and r.json()["request_id"]


async def test_validation_errors_are_422(ch):
    h, _ = ch
    assert (await h.client.post("/api/v1/chat/conversations", json={"model": "gemini-2.5-pro"})).status_code == 422
    assert (await h.client.post("/api/v1/chat/conversations", json={"mode": "turbo"})).status_code == 422
    cid = (await h.client.post("/api/v1/chat/conversations", json={})).json()["conversation_id"]
    for body in ({"title": ""}, {"title": "   "}, {"title": "x" * 121}, {"model": "nope"}, {"mode": "slow"}):
        r = await h.client.patch("/api/v1/chat/conversations/%s" % cid, json=body)
        assert r.status_code == 422 and r.json()["error"] == "invalid_request", body


async def test_database_down_is_503(ch):
    h, repo = ch
    repo.fail.add("list_conversations")
    r = await h.client.get("/api/v1/chat/conversations")
    assert r.status_code == 503 and r.json()["error"] == "database_unavailable"


async def test_suggestions_use_real_ids(ch):
    h, _ = ch
    s = (await h.client.get("/api/v1/chat/suggestions")).json()["suggestions"]
    assert len(s) == 4 and all(x["title"] and x["prompt"] for x in s)
    text = " ".join(x["prompt"] for x in s)
    assert "HX-217" in text and "WP06" in text and "#14" in text and "6 of the 17" in text
    # chat.2: completed_with_failures counts failed actions only, so the prompt says exactly that
    assert "still had failed actions. Which ones" in text and "serious anomalies" not in s[0]["prompt"]


async def test_empty_conversations_are_not_listed(ch):
    """chat.2: a chat whose first send never started leaves no ghost row in the history panel (items and total)."""
    h, repo = ch
    empty = (await h.client.post("/api/v1/chat/conversations", json={"title": "Empty draft"})).json()
    used = (await h.client.post("/api/v1/chat/conversations", json={"title": "Used chat"})).json()
    await h.client.patch("/api/v1/chat/conversations/%s" % empty["conversation_id"], json={"pinned": True})
    await repo.add_message(used["conversation_id"], {"role": "user", "content": "hello"})
    lst = (await h.client.get("/api/v1/chat/conversations")).json()
    assert lst["total"] == 1 and [i["conversation_id"] for i in lst["items"]] == [used["conversation_id"]]
    assert (await h.client.get("/api/v1/chat/conversations", params={"q": "draft"})).json() == {"items": [],
                                                                                               "total": 0}
    # still reachable by id (the page that created it can send into it)
    assert (await h.client.get("/api/v1/chat/conversations/%s" % empty["conversation_id"])).status_code == 200


async def test_suggestions_fill_gaps_and_survive_a_db_error(ch):
    h, repo = ch
    repo.facts = {"hidden_failures": {"count": 0, "of": 17}, "top_asset": None, "worst_location": None,
                  "latest_serious_flag": None, "low_battery": {"band": "<=25", "success_rate_pct": 55.0}}
    s = (await h.client.get("/api/v1/chat/suggestions")).json()["suggestions"]
    assert len(s) == 4 and "<=25%" in s[0]["prompt"]
    repo.fail.add("suggestion_facts")
    s = (await h.client.get("/api/v1/chat/suggestions")).json()["suggestions"]
    assert len(s) == 4
