"""Answer modes (docs/chat-contract.md §10, chat.4): only questions about the plant are grounded; greetings and
general questions get a general answer with no retrieval, no <sources> block and no citation rule."""
import pytest

from app.services.chat.prompt import GENERAL_PROMPT, SYSTEM_PROMPT
from app.services.chat.retrieval import Prior
from app.services.chat.routing import GENERAL, GROUNDED, PROBE, choose, fleet_ids, has_evidence

from .fakes_chat import answer_chunks, chat_harness, embed_ok, events, gemini_stream

CITED = Prior(text="HX-217 read 412.5 psi [incident:AN012].", cited=[{"id": "incident:AN012"}])
UNCITED = Prior(text="Hello! I'm SPOTbot.", cited=[])


# ── the rules ───────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("q", ["hi", "Hello!", "hey there", "good morning", "thanks!", "Thank you so much",
                               "ok", "what can you do?", "Who are you?", "help", "how can you help me?", "bye"])
def test_small_talk_is_general(q):
    assert choose(q, None, None, None) == (GENERAL, "small_talk")


@pytest.mark.parametrize("q,reason", [
    ("Why did M007 fail?", "names_id"), ("what's up with hx-217", "names_id"), ("show flag #14", "names_id"),
    ("thanks, and what about WP05?", "names_id"),
    ("What does a P1 flag mean?", "plant_terms"), ("Which missions hid failures?", "plant_terms"),
    ("Give me a fleet health summary", "plant_terms"), ("How many anomalies were critical?", "plant_terms"),
    ("Is V2 better than V1?", "plant_terms"), ("what's our worst waypoint", "plant_terms"),
    ("How does Spot climb stairs?", "plant_terms"),
])
def test_plant_questions_are_grounded(q, reason):
    assert choose(q, None, None, None) == (GROUNDED, reason)


def test_a_mission_chat_is_always_grounded_even_for_small_talk():
    assert choose("hi", None, None, "M007") == (GROUNDED, "mission_chat")
    assert choose("what can you do?", None, None, "M007").mode == GROUNDED


def test_follow_ups_to_a_cited_answer_are_grounded_but_thanks_is_not():
    assert choose("why?", "What happened to HX-217?", CITED, None) == (GROUNDED, "follow_up")
    assert choose("and the second one?", None, CITED, None) == (GROUNDED, "follow_up")
    assert choose("thanks!", "What happened to HX-217?", CITED, None) == (GENERAL, "small_talk")
    assert choose("why?", "hi", UNCITED, None) == (PROBE, "unsure")       # nothing to follow up on


@pytest.mark.parametrize("q", ["How does lidar work?", "Explain Kalman filters", "write a haiku about robots?"])
def test_anything_else_is_probed_not_guessed(q):
    route = choose(q, None, None, None)
    assert route.mode in (PROBE, GROUNDED) and route.mode != GENERAL     # never answered generally on a guess


def test_evidence_is_plant_context_not_the_snapshot_carry_over_or_the_manual():
    assert not has_evidence([{"id": "stat:fleet", "kind": "stat"}, {"id": "stat:config", "kind": "stat"}])
    assert not has_evidence([{"id": "incident:AN012", "kind": "incident"}], carried=["incident:AN012"])
    # measured: "How does lidar work?" retrieved only Spot User Guide pages; that is not plant context
    assert not has_evidence([{"id": "manual:p19-1", "kind": "manual"}, {"id": "policy:battery_low", "kind": "policy"},
                             {"id": "topic:localization", "kind": "topic"}])
    assert has_evidence([{"id": "stat:fleet", "kind": "stat"}, {"id": "incident:AN012", "kind": "incident"}])
    assert has_evidence([{"id": "mission:M007", "kind": "mission"}])
    assert has_evidence([{"id": "event:act-A0061", "kind": "event"}])


def test_fleet_ids_lists_plant_ids_a_general_answer_names():
    assert fleet_ids("Try asking about M007, HX-217 at WP05 or flag 14.") == ["M007", "HX-217", "WP05", "flag 14"]
    assert fleet_ids("Hi! I'm SPOTbot.") == []


# ── the stream ──────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
async def ch(make_harness):
    h, repo = chat_harness(make_harness)
    yield h, repo
    await h.client.aclose()


async def _turn(h, content, mission_id=None, cid=None):
    if cid is None:
        body = {"mission_id": mission_id} if mission_id else {}
        cid = (await h.client.post("/api/v1/chat/conversations", json=body)).json()["conversation_id"]
    r = await h.client.post("/api/v1/chat/conversations/%s/messages" % cid, json={"content": content})
    assert r.status_code == 200, r.text
    return cid, dict(events(r.text))


def _retrieval_calls(repo):
    return [n for n, _ in repo.calls if n in ("search_documents", "match_graph_nodes", "fleet_snapshot",
                                              "entity_digests")]


async def test_small_talk_skips_retrieval_and_sends_the_general_prompt(ch, mock):
    h, repo = ch
    embed = embed_ok(mock)
    sent = []
    gemini_stream(mock, answer_chunks(["Hi! I'm SPOTbot. Ask me about a mission, ", "for example M007."]),
                  captured=sent)
    _, ev = await _turn(h, "hi")
    assert ev["sources"]["sources"] == []
    assert ev["sources"]["retrieval"]["answer_mode"] == "general" and ev["sources"]["retrieval"]["route"] == "small_talk"
    assert ev["sources"]["retrieval"]["mode"] is None
    assert _retrieval_calls(repo) == [] and embed.call_count == 0
    body = sent[0]["body"]
    assert body["systemInstruction"]["parts"][0]["text"] == GENERAL_PROMPT
    assert body["contents"][-1]["parts"][0]["text"] == "hi" and "<sources>" not in str(body["contents"])
    msg = ev["done"]["message"]
    assert msg["sources"] == [] and msg["citations"] == []
    assert msg["grounding"]["mode"] == "general" and msg["grounding"]["grounded"] is False
    assert msg["grounding"]["fleet_ids"] == ["M007"]          # named anyway: the web shows a warning


async def test_a_plant_question_is_grounded_as_before(ch, mock):
    h, repo = ch
    embed_ok(mock)
    sent = []
    gemini_stream(mock, captured=sent)
    _, ev = await _turn(h, "What happened to HX-217's gauge?")
    assert ev["sources"]["retrieval"]["answer_mode"] == "grounded" and ev["sources"]["sources"]
    assert sent[0]["body"]["systemInstruction"]["parts"][0]["text"] == SYSTEM_PROMPT
    assert "<sources>" in sent[0]["body"]["contents"][-1]["parts"][0]["text"]
    g = ev["done"]["message"]["grounding"]
    assert g["mode"] == "grounded" and g["grounded"] is True and "fleet_ids" not in g


async def test_a_mission_chat_grounds_a_greeting(ch, mock):
    h, repo = ch
    embed_ok(mock)
    sent = []
    gemini_stream(mock, captured=sent)
    _, ev = await _turn(h, "hi", mission_id="M007")
    assert ev["sources"]["retrieval"]["answer_mode"] == "grounded" and ev["sources"]["retrieval"]["route"] == "mission_chat"
    assert any(s["id"] == "mission:M007" for s in ev["sources"]["sources"])
    assert sent[0]["body"]["systemInstruction"]["parts"][0]["text"] == SYSTEM_PROMPT


async def test_a_probe_with_only_fleet_stats_answers_generally(ch, mock):
    h, repo = ch
    embed_ok(mock)
    repo.docs, repo.nodes, repo.neighbours = [], [], []
    sent = []
    gemini_stream(mock, answer_chunks(["Lidar measures distance with laser pulses."]), captured=sent)
    _, ev = await _turn(h, "How does lidar work?")
    assert "search_documents" in _retrieval_calls(repo)                    # the probe did run
    assert ev["sources"]["sources"] == [] and ev["sources"]["retrieval"]["answer_mode"] == "general"
    assert ev["sources"]["retrieval"]["route"] == "unsure_no_data"
    assert sent[0]["body"]["systemInstruction"]["parts"][0]["text"] == GENERAL_PROMPT
    assert ev["done"]["message"]["grounding"] == {
        "grounded": False, "cited_ids": [], "unknown_ids": [], "unverified_numbers": [], "source_count": 0,
        "retrieval_mode": None, "graph_seeds": [], "mode": "general", "fleet_ids": []}


async def test_a_probe_that_finds_only_manual_pages_answers_generally(ch, mock):
    h, repo = ch
    embed_ok(mock)
    repo.docs = [{"doc_id": "manual:p19-1", "kind": "manual", "title": "Spot User Guide p.11 (PDF p.19): SAFETY",
                  "content": "Spot carries cameras and depth sensors; keep clear of the robot while it walks.",
                  "asset_id": None, "location_id": None, "mission_id": None, "action_id": None, "ts": None,
                  "score": 0.0323},
                 {"doc_id": "policy:battery_low", "kind": "policy", "title": "Low battery policy",
                  "content": "Do not start a mission below 40% battery.", "asset_id": None, "location_id": None,
                  "mission_id": None, "action_id": None, "ts": None, "score": 0.0161}]
    repo.nodes, repo.neighbours = [], []
    gemini_stream(mock, answer_chunks(["Lidar times laser pulses to measure distance."]))
    _, ev = await _turn(h, "How does lidar work?")
    assert ev["sources"]["retrieval"]["answer_mode"] == "general" and ev["sources"]["sources"] == []


async def test_a_probe_that_finds_plant_records_is_grounded(ch, mock):
    h, repo = ch
    embed_ok(mock)
    gemini_stream(mock)
    _, ev = await _turn(h, "How does lidar work?")
    assert ev["sources"]["retrieval"]["answer_mode"] == "grounded"
    assert ev["sources"]["retrieval"]["route"] == "unsure_found_data"
    assert any(not s["id"].startswith("stat:") for s in ev["sources"]["sources"])


async def test_small_talk_works_with_the_database_down(ch, mock):
    h, repo = ch
    embed_ok(mock)
    repo.fail |= {"search_documents", "match_graph_nodes", "fleet_snapshot", "entity_digests"}
    gemini_stream(mock, answer_chunks(["Hello!"]))
    _, ev = await _turn(h, "hello")
    assert "done" in ev and "error" not in ev
    _, ev = await _turn(h, "How does lidar work?")                        # a probe falls back to general
    assert "done" in ev and ev["sources"]["retrieval"]["answer_mode"] == "general"
    _, ev = await _turn(h, "Why did M007 fail?")                          # a grounded turn still fails loudly
    assert ev["error"]["code"] == "retrieval_failed"


async def test_thanks_after_a_cited_answer_is_general_and_a_follow_up_is_grounded(ch, mock):
    h, repo = ch
    embed_ok(mock)
    gemini_stream(mock)
    cid, ev = await _turn(h, "What happened to HX-217's gauge?")
    assert ev["done"]["message"]["citations"]
    _, ev = await _turn(h, "thanks!", cid=cid)
    assert ev["sources"]["retrieval"]["answer_mode"] == "general"
    _, ev = await _turn(h, "why?", cid=cid)
    assert ev["sources"]["retrieval"]["route"] == "follow_up"         # the general "thanks" reply is skipped
    assert any(src["id"] == "incident:AN012" for src in ev["sources"]["sources"])   # carry-over kept
