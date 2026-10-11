"""Mission chats (docs/chat-contract.md §9, chat.3): create with a mission (case-insensitive, canonical, 422 unknown),
the list filter, PATCH can't change the mission, GET /chat/missions, mission suggestions, and mission retrieval (the
pinned digest, mission-filtered documents ranked first, the mission graph seed, the prompt line, retrieval.mission_id).
A chat without a mission retrieves and prompts exactly as before."""
import pytest

from app.services.chat.prompt import mission_line, user_turn
from app.services.chat.retrieval import MISSION_DOCS_K, Retriever
from app.services.chat.service import mission_suggestions, worst_failure

from .fakes_chat import DIGESTS, FakeChatRepo, chat_harness, embed_ok, events, gemini_stream

LINE_M007 = ("This conversation is about mission M007 (Night rounds). Questions that name no other subject refer "
             "to it.")


@pytest.fixture
async def ch(make_harness):
    h, repo = chat_harness(make_harness)
    yield h, repo
    await h.client.aclose()


async def _create(h, **body):
    return await h.client.post("/api/v1/chat/conversations", json=body)


# ── API ─────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("given", ["M007", "m007", " m007 ", "mission:M007", "MISSION:m007"])
async def test_create_with_a_mission_is_case_insensitive_and_canonical(ch, given):
    h, _ = ch
    r = await _create(h, mission_id=given)
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["mission_id"] == "M007"
    assert c["mission"] == {"mission_id": "M007", "mission_name": "Night rounds", "mission_status": "COMPLETED"}
    got = (await h.client.get("/api/v1/chat/conversations/%s" % c["conversation_id"])).json()["conversation"]
    assert got["mission_id"] == "M007" and got["mission"]["mission_name"] == "Night rounds"


@pytest.mark.parametrize("given", ["M999", "nope", "mission:", "HX-217"])
async def test_create_with_an_unknown_mission_is_422(ch, given):
    h, repo = ch
    r = await _create(h, mission_id=given)
    assert r.status_code == 422 and r.json()["error"] == "unknown_mission" and r.json()["request_id"]
    assert repo.conversations == {}                       # nothing was created


async def test_a_chat_without_a_mission_has_null_mission_fields(ch):
    h, _ = ch
    for body in ({}, {"mission_id": None}, {"mission_id": "  "}):
        c = (await _create(h, **body)).json()
        assert c["mission_id"] is None and c["mission"] is None


async def test_list_filters_by_mission(ch):
    h, repo = ch
    m = (await _create(h, mission_id="M007")).json()["conversation_id"]
    plain = (await _create(h)).json()["conversation_id"]
    for cid in (m, plain):
        await repo.add_message(cid, {"role": "user", "content": "question"})
    allc = (await h.client.get("/api/v1/chat/conversations")).json()
    assert allc["total"] == 2
    by = {i["conversation_id"]: i for i in allc["items"]}
    assert by[m]["mission_id"] == "M007" and by[m]["mission"]["mission_status"] == "COMPLETED"
    assert by[plain]["mission_id"] is None and by[plain]["mission"] is None
    for given in ("M007", "m007"):
        only = (await h.client.get("/api/v1/chat/conversations", params={"mission_id": given})).json()
        assert only["total"] == 1 and [i["conversation_id"] for i in only["items"]] == [m]
    assert (await h.client.get("/api/v1/chat/conversations", params={"mission_id": "M999"})).json() == {
        "items": [], "total": 0}


async def test_patch_cannot_change_the_mission(ch):
    h, _ = ch
    m = (await _create(h, mission_id="M007")).json()["conversation_id"]
    plain = (await _create(h)).json()["conversation_id"]
    r = await h.client.patch("/api/v1/chat/conversations/%s" % m, json={"mission_id": None, "pinned": True})
    assert r.status_code == 200 and r.json()["mission_id"] == "M007" and r.json()["pinned"] is True
    r = await h.client.patch("/api/v1/chat/conversations/%s" % plain, json={"mission_id": "M007"})
    assert r.status_code == 200 and r.json()["mission_id"] is None


async def test_missions_list_for_the_picker(ch):
    h, repo = ch
    m = (await _create(h, mission_id="M007")).json()["conversation_id"]
    await repo.add_message(m, {"role": "user", "content": "q"})
    items = (await h.client.get("/api/v1/chat/missions")).json()["items"]
    row = next(i for i in items if i["mission_id"] == "M007")
    assert set(row) == {"mission_id", "mission_name", "scenario", "configuration_version", "start_time",
                        "duration_min", "start_battery_pct", "end_battery_pct", "mission_status", "actions",
                        "failed", "anomalies", "serious_anomalies", "open_flags", "hidden_failure", "chats"}
    assert row["hidden_failure"] is True and row["chats"] == 1 and row["failed"] == 4 and row["open_flags"] == 1
    assert (await h.client.get("/api/v1/chat/missions", params={"q": "night"})).json()["items"][0]["mission_id"] == \
        "M007"
    assert (await h.client.get("/api/v1/chat/missions", params={"q": "zzz"})).json() == {"items": []}
    assert repo.calls[-1] == ("list_missions", {"q": "zzz", "limit": 100})


async def test_mission_suggestions_come_from_the_digest(ch):
    h, repo = ch
    for given in ("M007", "m007"):
        s = (await h.client.get("/api/v1/chat/suggestions", params={"mission_id": given})).json()["suggestions"]
        assert len(s) == 4 and all(x["title"] and "M007" in x["prompt"] for x in s)
        text = " ".join(x["prompt"] for x in s)
        assert "reported COMPLETED, but 4 of its 15 actions failed" in text      # hidden failures
        assert "A0061" in text and "LOCALIZATION_FAILURE" in text               # the worst failed action
        assert "88% to 31%" in text                                              # battery start -> end
    r = await h.client.get("/api/v1/chat/suggestions", params={"mission_id": "M999"})
    assert r.status_code == 422 and r.json()["error"] == "unknown_mission"
    assert not any(n == "suggestion_facts" for n, _ in repo.calls)


def test_mission_suggestions_cover_failed_and_clean_missions():
    d = dict(DIGESTS["missions"]["M007"])
    d.update(mission_status="FAILED", anomalies=[
        {"anomaly_id": "AN020", "asset_id": "PUMP-205", "anomaly_type": "LEAK", "severity": "CRITICAL"},
        {"anomaly_id": "AN021", "asset_id": "HX-217", "anomaly_type": "GAUGE_HIGH", "severity": "HIGH"}],
        failures=[{"action_id": "A0061", "sequence": 7, "asset_id": None, "failure_type": "LOCALIZATION_FAILURE"},
                  {"action_id": "A0070", "sequence": 9, "asset_id": "PUMP-205", "failure_type": "CAMERA"}])
    s = mission_suggestions(d)
    assert s[0]["title"] == "Why did M007 fail?"
    assert worst_failure(d)["action_id"] == "A0070"       # on the asset with the CRITICAL anomaly
    assert "A0070" in s[1]["prompt"] and "2 anomalies" in s[2]["prompt"] and "CRITICAL-severity LEAK" in s[2]["prompt"]
    clean = dict(DIGESTS["missions"]["M007"], failed=0, failures=[], anomalies=[], start_battery_pct=None)
    s = mission_suggestions(clean)
    assert len(s) == 4 and s[0]["title"] == "Was M007 clean?" and "configuration V1" in s[3]["prompt"]


# ── retrieval ───────────────────────────────────────────────────────────────────────────────────────
async def test_the_pinned_digest_survives_a_tiny_budget():
    repo = FakeChatRepo()
    out = await Retriever(repo, None, max_chars=50).retrieve("What went wrong on this mission?", mission_id="M007")
    ids = [s["id"] for s in out.sources]
    assert ids[0] == "mission:M007"
    assert all(i == "mission:M007" or i.startswith("stat:") for i in ids)    # everything else was dropped
    assert out.block.startswith('<sources>\n<source id="mission:M007"')
    assert out.mission_id == "M007" and out.meta()["mission_id"] == "M007"
    assert out.mission["mission_name"] == "Night rounds"


async def test_mission_documents_rank_first_and_are_deduped():
    repo = FakeChatRepo()
    repo.mission_docs = [dict(d, mission_id="M007") for d in repo.mission_docs]   # AN012 is also a global hit
    out = await Retriever(repo, None).retrieve("What went wrong on this mission?", mission_id="m007")
    ids = [s["id"] for s in out.sources]
    assert ids[:3] == ["mission:M007", "event:act-A0061", "incident:AN012"]
    assert ids.count("incident:AN012") == 1 and "policy:battery_low" in ids[3:]
    searches = [kw for n, kw in repo.calls if n == "search_documents"]
    assert {"q": "What went wrong on this mission?", "vec": None, "k": MISSION_DOCS_K, "mission_id": "M007"} in searches
    assert any(kw["mission_id"] is None and kw["k"] == 8 for kw in searches)     # the global search still runs


async def test_the_mission_node_is_always_a_graph_seed():
    repo = FakeChatRepo()
    out = await Retriever(repo, None).retrieve("What went wrong on this mission?", mission_id="M007")
    assert out.graph_seeds[0] == "mission:M007" and len(out.graph_seeds) <= 5
    seeds = [kw["seed_ids"] for n, kw in repo.calls if n == "graph_neighborhood"]
    assert any("mission:M007" in s for s in seeds)
    # the question also naming the mission: one seed, one digest
    repo2 = FakeChatRepo()
    out2 = await Retriever(repo2, None).retrieve("And m007's battery?", mission_id="M007")
    assert out2.graph_seeds.count("mission:M007") == 1
    assert [s["id"] for s in out2.sources].count("mission:M007") == 1
    digests = [kw for n, kw in repo2.calls if n == "entity_digests"]
    assert sum(kw["missions"].count("M007") for kw in digests) == 1           # fetched once, as the pinned one


async def test_other_missions_can_still_be_asked_about():
    repo = FakeChatRepo()
    out = await Retriever(repo, None).retrieve("Compare with HX-217", mission_id="M007")
    ids = [s["id"] for s in out.sources]
    assert ids[0] == "mission:M007" and "asset:HX-217" in ids


async def test_a_chat_without_a_mission_retrieves_as_before():
    repo = FakeChatRepo()
    out = await Retriever(repo, None).retrieve("What happened to HX-217's gauge?")
    assert out.mission_id is None and out.meta()["mission_id"] is None and out.mission is None
    assert not any(kw.get("mission_id") for n, kw in repo.calls if n == "search_documents")
    assert not any(s.startswith("mission:") for s in out.graph_seeds)
    assert out.graph_seeds[0] == "asset:HX-217"


def test_prompt_line():
    assert mission_line("M007", "Night rounds") == LINE_M007
    assert mission_line("M007", None) == ("This conversation is about mission M007. Questions that name no other "
                                          "subject refer to it.")
    assert mission_line(None, "x") is None
    assert user_turn("<sources/>", "Q?") == "<sources/>\n\nQuestion: Q?"         # unchanged without a mission
    assert user_turn("<sources/>", "Q?", LINE_M007) == LINE_M007 + "\n<sources/>\n\nQuestion: Q?"


# ── end to end ──────────────────────────────────────────────────────────────────────────────────────
async def test_a_mission_turn_pins_the_mission_and_opens_with_the_line(ch, mock):
    h, repo = ch
    embed_ok(mock)
    cap = []
    gemini_stream(mock, captured=cap)
    cid = (await _create(h, mission_id="m007")).json()["conversation_id"]
    r = await h.client.post("/api/v1/chat/conversations/%s/messages" % cid,
                            json={"content": "What went wrong on this mission?"})
    evs = dict(events(r.text))
    assert evs["sources"]["retrieval"]["mission_id"] == "M007"
    assert evs["sources"]["sources"][0]["id"] == "mission:M007"
    assert evs["sources"]["retrieval"]["graph_seeds"][0] == "mission:M007"
    final = cap[0]["body"]["contents"][-1]
    assert final["role"] == "user" and final["parts"][0]["text"].startswith(LINE_M007 + "\n<sources>")
    assert final["parts"][0]["text"].endswith("Question: What went wrong on this mission?")
    assert evs["done"]["conversation"]["mission_id"] == "M007"
    assert evs["done"]["conversation"]["mission"]["mission_name"] == "Night rounds"


async def test_a_plain_turn_has_no_mission_line(ch, mock):
    h, repo = ch
    embed_ok(mock)
    cap = []
    gemini_stream(mock, captured=cap)
    cid = (await _create(h)).json()["conversation_id"]
    r = await h.client.post("/api/v1/chat/conversations/%s/messages" % cid,
                            json={"content": "What happened to HX-217's gauge?"})
    evs = dict(events(r.text))
    assert evs["sources"]["retrieval"]["mission_id"] is None
    assert cap[0]["body"]["contents"][-1]["parts"][0]["text"].startswith("<sources>")
    assert evs["done"]["conversation"]["mission_id"] is None and evs["done"]["conversation"]["mission"] is None
