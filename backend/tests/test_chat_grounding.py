"""Grounding check (contract §2), retrieval (entities, merge, seeds, budget, fallbacks) and prompt helpers."""
import pytest

from app.services.chat.gemini import GeminiClient, parse_retry_delay
from app.services.chat.grounding import check, citations_in, normalize_number, numbers_in
from app.services.chat.prompt import auto_title, build_body, clean_title, history_contents
from app.services.chat.retrieval import MAX_CONTEXT_CHARS, NON_INSTANCE_KINDS, Retriever, extract_entities

from .fakes_chat import FakeChatRepo

SOURCES = [{"id": "incident:AN012"}, {"id": "asset:HX-217"}, {"id": "stat:battery_bands"}, {"id": "manual:p52-1"}]
TEXTS = ["HX-217 read 412.5 psi; the safe band ends at 380 psi. 1,234 inspections.",
         "- band=<=25, n=30, success_rate_pct=62.50"]


def g(answer, question=""):
    return check(answer, SOURCES, TEXTS, question, "hybrid", ["asset:HX-217"])


# ── grounding ───────────────────────────────────────────────────────────────────────────────────
def test_valid_answer_is_grounded():
    out = g("HX-217 read 412.5 psi [incident:AN012]; low-battery actions succeed 62.5% of the time "
            "[stat:battery_bands].")
    assert out == {"grounded": True, "cited_ids": ["incident:AN012", "stat:battery_bands"], "unknown_ids": [],
                   "unverified_numbers": [], "source_count": 4, "retrieval_mode": "hybrid",
                   "graph_seeds": ["asset:HX-217"], "mode": "grounded"}


def test_unknown_id_is_not_grounded():
    out = g("It failed [incident:AN099] and [incident:AN012].")
    assert out["cited_ids"] == ["incident:AN012"] and out["unknown_ids"] == ["incident:AN099"]
    assert out["grounded"] is False


def test_no_citation_is_not_grounded():
    assert g("HX-217 is fine.")["grounded"] is False


def test_unverified_number():
    out = g("HX-217 read 415 psi and 380 psi is the limit [incident:AN012].")
    assert out["unverified_numbers"] == ["415"] and out["grounded"] is True


def test_ids_are_not_numbers():
    out = g("M007, HX-204, WP09, A0133, act-A0001 and V2 were involved [asset:HX-217] [manual:p52-1].")
    assert out["unverified_numbers"] == [] and out["cited_ids"] == ["asset:HX-217", "manual:p52-1"]


def test_comma_and_adjacent_citations_in_first_use_order():
    out = g("Both [asset:HX-217, incident:AN012][stat:battery_bands] and again [incident:AN012; manual:p52-1].")
    assert out["cited_ids"] == ["asset:HX-217", "incident:AN012", "stat:battery_bands", "manual:p52-1"]


def test_number_normalization_and_question_numbers():
    assert normalize_number("62.50%") == "62.5" and normalize_number("1,234") == "1234"
    assert normalize_number("007") == "7" and normalize_number("0.50") == "0.5" and normalize_number("40.0") == "40"
    out = g("1234 inspections, 62.5% [stat:battery_bands]; you asked about 77 [incident:AN012].",
            question="what about 77 missions?")
    assert out["unverified_numbers"] == []


def test_bracketed_numbers_and_list_markers_are_ignored():
    assert numbers_in("1. First [stat:battery_bands]\n2) Second [99]") == []
    assert citations_in("see [x] and [1] and [Some text]", ["stat:fleet"]) == ([], [])


def test_citations_are_matched_case_insensitively_to_the_canonical_id():
    assert citations_in("[Incident:AN012]", ["incident:AN012"])[0] == ["incident:AN012"]


# ── retrieval ───────────────────────────────────────────────────────────────────────────────────
def test_entity_regexes():
    e = extract_entities("Compare m007 and M012 on hx-217 at WP09; flag #14, Flag 3 and #22", "earlier: M007")
    assert e == {"missions": ["M007", "M012"], "assets": ["HX-217"], "locations": ["WP09"], "flags": [14, 3, 22]}


async def test_retrieval_sources_merge_seeds_and_stats():
    repo = FakeChatRepo()
    r = await Retriever(repo, None).retrieve("Why does HX-217 fail at WP06? see flag 14")
    ids = [s["id"] for s in r.sources]
    assert len(ids) == len(set(ids))                                        # merged by id
    assert ids[:3] == ["asset:HX-217", "location:WP06", "flag:14"]          # named entities first
    assert {"stat:fleet", "stat:battery_bands", "stat:config", "stat:locations", "stat:assets",
            "stat:failure_types"} <= set(ids)
    hx = r.sources[0]
    assert hx["origin"] == "sql" and hx["kind"] == "asset" and len(hx["snippet"]) <= 280
    wp = next(s for s in r.sources if s["id"] == "location:WP06")
    assert wp["path"] == "asset:HX-217 -located_at-> location:WP06"         # graph path kept on the digest
    assert r.graph_seeds[:3] == ["asset:HX-217", "location:WP06", "flag:14"] and len(r.graph_seeds) == 4
    nb = [a for n, a in repo.calls if n == "graph_neighborhood"]
    hub_call = next(a for a in nb if "location:WP06" in a["seed_ids"])
    assert hub_call["kind_filter"] == NON_INSTANCE_KINDS                    # a hub seed hides instance nodes
    assert r.mode == "fulltext" and r.gaps                                  # no embed client -> full text
    assert len(r.block) <= MAX_CONTEXT_CHARS and r.block.startswith("<sources>")
    assert len(r.texts) == len(r.sources) and '<source id="stat:fleet"' in r.texts[-6] + "".join(r.texts[-6:])


async def test_graph_entry_nodes_add_digests():
    repo = FakeChatRepo()
    r = await Retriever(repo, None).retrieve("which heat exchanger is worst?")
    digest_calls = [a for n, a in repo.calls if n == "entity_digests"]
    assert digest_calls == [{"missions": [], "assets": ["HX-217"], "locations": [], "flags": []}]
    assert next(s for s in r.sources if s["id"] == "asset:HX-217")["origin"] == "sql"


async def test_budget_drops_lowest_ranked_but_keeps_the_snapshot():
    repo = FakeChatRepo()
    repo.docs = [{"doc_id": "event:act-A%04d" % i, "kind": "event", "title": "Event %d" % i, "content": "x " * 900,
                  "score": 1.0 - i / 100} for i in range(30)]
    r = await Retriever(repo, None, max_chars=6000).retrieve("tell me everything")
    ids = [s["id"] for s in r.sources]
    assert len(r.block) <= 6000 and "stat:fleet" in ids and "stat:failure_types" in ids
    kept = [i for i in ids if i.startswith("event:")]
    assert kept and kept == ["event:act-A%04d" % i for i in range(len(kept))]   # best first, tail dropped


async def test_partial_failures_become_gaps_and_total_failure_raises():
    from app.services.chat.retrieval import RetrievalFailed
    repo = FakeChatRepo()
    repo.fail = {"match_graph_nodes", "entity_digests"}
    r = await Retriever(repo, None).retrieve("HX-217?")
    assert any(gap.startswith("graph match failed:") for gap in r.gaps)
    assert any(gap.startswith("entity digests failed:") for gap in r.gaps)
    assert "incident:AN012" in [s["id"] for s in r.sources]
    repo.fail = {"search_documents", "match_graph_nodes", "fleet_snapshot"}
    with pytest.raises(RetrievalFailed):
        await Retriever(repo, None).retrieve("HX-217?")


async def test_slow_queries_are_cut_at_the_budget():
    import asyncio
    repo = FakeChatRepo()
    orig = repo.search_documents

    async def slow(*a, **kw):
        await asyncio.sleep(5)
        return await orig(*a, **kw)
    repo.search_documents = slow
    r = await Retriever(repo, None, budget_s=0.3).retrieve("HX-217?")
    assert r.latency_ms < 2000 and "document search timed out (retrieval budget)" in r.gaps
    assert not any(s["origin"] == "rag" for s in r.sources)


# ── prompt / gemini helpers ─────────────────────────────────────────────────────────────────────
def test_history_is_plain_alternating_turns():
    h = [{"role": "assistant", "content": "orphan"}, {"role": "user", "content": "a"},
         {"role": "assistant", "content": ""}, {"role": "user", "content": "b"},
         {"role": "assistant", "content": "answer [incident:AN012]."}]
    assert history_contents(h, 12) == [{"role": "user", "parts": [{"text": "a\nb"}]},
                                       {"role": "model", "parts": [{"text": "answer."}]}]
    assert history_contents(h, 1) == []                       # a lone model turn is dropped
    body = build_body("gemini-3.8-flash", "fast", h, "<sources>..</sources>\n\nQuestion: q", 12)
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]


def test_titles():
    assert auto_title("short question") == "short question"
    t = auto_title("Which waypoints fail the most often during night rounds at the plant, and why?")
    assert t.endswith("…") and len(t) <= 61 and not t[:-1].endswith(" ")
    assert clean_title('Title: "**HX-217 Gauge Reading.**"\nextra') == "HX-217 Gauge Reading"
    assert clean_title("  ") is None and clean_title("...") is None


def test_retry_delay_and_error_mapping():
    assert parse_retry_delay("41s") == 41 and parse_retry_delay("41.53s") == 42 and parse_retry_delay(None) is None
    c = GeminiClient("KEY-abc-123", "http://g", None, None)
    e = c.map_http_error(429, b'{"error": {"code": 429, "message": "quota KEY-abc-123. Please retry in 7.2s."}}', "m")
    assert e.code == "rate_limited" and e.retry_after_s == 8 and "KEY-abc-123" not in e.detail
    assert c.map_http_error(400, b'{"error": {"message": "Request contains an invalid argument."}}', "m").code == \
        "upstream_error"
    assert c.map_http_error(502, b"<html>bad gateway</html>", "m").code == "upstream_error"
    assert "KEY-abc-123" not in repr(c)


# ── chat.2: the shared citation rule (the web runs the same fixtures) ────────────────────────────
FIXTURE_SOURCES = ["incident:AN012", "stat:fleet", "mission:M001"]


@pytest.mark.parametrize("text,cited,unknown,chips", [
    ("[incident:AN012]", ["incident:AN012"], [], [["incident:AN012"]]),
    ("[incident:AN012, stat:fleet]", ["incident:AN012", "stat:fleet"], [], [["incident:AN012", "stat:fleet"]]),
    ("[mission:M001; stat:fleet]", ["mission:M001", "stat:fleet"], [], [["mission:M001", "stat:fleet"]]),
    ("[incident:an012]", ["incident:AN012"], [], [["incident:AN012"]]),                  # case-insensitive
    ("[mission:M001, M002]", ["mission:M001"], [], [["mission:M001"]]),                  # M002 isn't id-shaped
    ("[not an id]", [], [], [[]]),                                                       # literal text
    ("[foo:bar]", [], ["foo:bar"], [[]]),                                                # unknown, literal
])
def test_shared_citation_fixtures(text, cited, unknown, chips):
    from app.services.chat.grounding import citation_groups
    assert citations_in(text, FIXTURE_SOURCES) == (cited, unknown)
    assert [g["chips"] for g in citation_groups(text, FIXTURE_SOURCES)] == chips
    out = check("A claim " + text + ".", [{"id": i} for i in FIXTURE_SOURCES], [], "")
    assert out["cited_ids"] == cited and out["unknown_ids"] == unknown
    assert out["grounded"] is (bool(cited) and not unknown)


def test_citation_rule_edges():
    ids = ["incident:AN012", "stat:fleet"]
    # parts are trimmed of whitespace, quotes and backticks; first use orders the result; ids are deduped
    assert citations_in("x [ `stat:fleet` ][\"incident:AN012\"] y ['stat:fleet']", ids) == (
        ["stat:fleet", "incident:AN012"], [])
    # a group with a citing part keeps its unknown parts as unknown (and they are not chips)
    assert citations_in("[incident:AN012, Foo:Bar_1]", ids) == (["incident:AN012"], ["Foo:Bar_1"])
    # not inside inline or fenced code, not a Markdown link's text
    assert citations_in("`[incident:AN012]` and ``see [stat:fleet]``", ids) == ([], [])
    assert citations_in("```\n[incident:AN012]\n```\n~~~js\n[stat:fleet]\n~~~", ids) == ([], [])
    assert citations_in("[stat:fleet](https://example.com)", ids) == ([], [])
    assert citations_in("```\nunclosed [stat:fleet]", ids) == ([], [])
    # a group never spans lines
    assert citations_in("[incident:AN012\n]", ids) == ([], [])


# ── chat.2: dates, times, list markers ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("source_text", ["start_time: 2026-09-01T18:00:00", "start_time: 2026-09-01 18:00:00"])
def test_clock_times_and_dates_are_matched_part_by_part(source_text):
    srcs = [{"id": "mission:M001"}]
    for answer in ("M001 started at 18:00 [mission:M001].", "M001 ran on 2026-09-01 [mission:M001].",
                   "Start: 2026-09-01T18:00 [mission:M001].", "It began 2026/09/01 at 18:00:00 [mission:M001]."):
        assert check(answer, srcs, [source_text], "")["unverified_numbers"] == [], answer
    assert check("M001 started at 18:30 [mission:M001].", srcs, [source_text], "")["unverified_numbers"] == ["30"]


def test_list_markers_and_numbered_headings_are_not_numbers():
    answer = ("## 2. Battery\n1. First [stat:fleet]\n  2) Nested\n> 3. Quoted\n### 4 Things\n"
              "10. Tenth item with 77 actions")
    assert numbers_in(answer) == ["4", "77"]       # a heading's own number without a dot is text, kept


def test_source_timestamps_reach_the_model_with_a_space():
    import datetime as dt

    from app.services.chat.prompt import clean_source_text
    from app.services.chat.retrieval import _fmt
    assert _fmt(dt.datetime(2026, 9, 1, 18, 0, 5, 123456)) == "2026-09-01 18:00:05"
    assert _fmt(dt.datetime(2026, 9, 1, 18, 0, tzinfo=dt.timezone.utc)) == "2026-09-01 18:00:00 UTC"
    assert clean_source_text("ts 2026-09-01T18:00:00Z and 2026-09-01T07:30") == "ts 2026-09-01 18:00:00Z and " \
                                                                                "2026-09-01 07:30"


# ── chat.2: history rules ────────────────────────────────────────────────────────────────────────
def test_history_strips_citations_skips_errors_and_merges_user_turns():
    h = [{"role": "user", "content": "Which missions hid failures?"},
         {"role": "assistant", "status": "complete",
          "content": "M001 [incident:flag-3] and M006 [incident:flag-13, stat:fleet][mission:M006]. See [note]."},
         {"role": "user", "content": "And PANEL-212?"},
         {"role": "assistant", "status": "error", "content": "Partial text before a 503"},
         {"role": "user", "content": "Try again: PANEL-212?"},
         {"role": "assistant", "status": "error", "content": ""}]
    contents = history_contents(h, 12)
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    assert contents[1]["parts"][0]["text"] == "M001 and M006. See [note]."      # no old ids to copy
    assert contents[2]["parts"][0]["text"] == "And PANEL-212?\nTry again: PANEL-212?"
    body = build_body("gemini-3.5-flash", "fast", h, "<sources>x</sources>\n\nQuestion: q3", 12)
    roles = [c["role"] for c in body["contents"]]
    assert roles == ["user", "model", "user"] and roles[-1] == "user"          # was ['user','model','user','user']
    assert body["contents"][-1]["parts"][0]["text"] == ("And PANEL-212?\nTry again: PANEL-212?\n"
                                                        "<sources>x</sources>\n\nQuestion: q3")
    # a stopped answer with text stays (as conversation), citations stripped
    stopped = [{"role": "user", "content": "q"}, {"role": "assistant", "status": "stopped", "content": "half [stat:fleet]"}]
    assert history_contents(stopped, 12)[1]["parts"][0]["text"] == "half"
    # error turn right after the only question: still alternates and ends on the user turn
    only = [{"role": "user", "content": "q1"}, {"role": "assistant", "status": "error", "content": ""}]
    assert [c["role"] for c in build_body("gemini-3.5-flash", "fast", only, "q2", 12)["contents"]] == ["user"]


def test_every_family_gets_system_instruction_and_the_rules():
    from app.services.chat.prompt import SYSTEM_PROMPT
    for model in ("gemini-3.8-flash", "gemma-4-31b-it", "gemma-4-26b-a4b-it"):
        body = build_body(model, "fast", [], "<sources></sources>\n\nQuestion: q", 12)
        assert body["systemInstruction"] == {"parts": [{"text": SYSTEM_PROMPT}]}
        assert body["contents"] == [{"role": "user", "parts": [{"text": "<sources></sources>\n\nQuestion: q"}]}]
    assert "LaTeX" in SYSTEM_PROMPT and "≤ 25%" in SYSTEM_PROMPT and "no images" in SYSTEM_PROMPT


def test_title_body_minimal_thinking_and_40_tokens():
    from app.services.chat.prompt import title_body
    b = title_body("gemini-3.1-flash-lite", "Why HX-217?", "Because [incident:AN012].")
    assert b["generationConfig"] == {"maxOutputTokens": 40, "thinkingConfig": {"thinkingLevel": "minimal"}}
    assert "[incident:AN012]" not in b["contents"][0]["parts"][0]["text"]
    assert title_body("gemma-4-31b-it", "q", "a")["generationConfig"] == {"maxOutputTokens": 40}


# ── chat.2: 503 / UNAVAILABLE ────────────────────────────────────────────────────────────────────
def test_503_and_unavailable_are_model_unavailable():
    c = GeminiClient("KEY-abc-123", "http://g", None, None)
    high = b'{"error": {"code": 503, "message": "This model is currently experiencing high demand.", ' \
           b'"status": "UNAVAILABLE"}}'
    assert c.map_http_error(503, high, "gemma-4-31b-it").code == "model_unavailable"
    assert c.map_http_error(503, b"", "m").code == "model_unavailable"
    assert c.map_http_error(500, b'{"error": {"code": 500, "status": "UNAVAILABLE", "message": "x"}}', "m").code == \
        "model_unavailable"
    assert c.map_http_error(500, b'{"error": {"code": 500, "message": "Internal error"}}', "m").code == \
        "upstream_error"


# ── chat.2: previous answer -> entities and carry-over ───────────────────────────────────────────
def test_prior_answer_entities_text_order_then_refs_capped_at_5():
    from app.services.chat.retrieval import Prior, prior_answer, prior_entity_list
    cited = [{"id": "incident:AN012", "refs": {"mission_id": "M014", "asset_id": "HX-217", "location_id": "WP06"}},
             {"id": "flag:14", "refs": {"flag_id": 14, "mission_id": "M007"}}]
    p = Prior(text="First M003, then M006 [flag:22] and #9; again M003.", cited=cited)
    assert prior_entity_list(p) == [("missions", "M003"), ("missions", "M006"), ("flags", 22), ("flags", 9),
                                    ("missions", "M014")]
    assert prior_entity_list(None) == []
    hist = [{"role": "user", "content": "q"},
            {"role": "assistant", "status": "complete", "content": "ok [flag:14][incident:AN012]",
             "sources": [{"id": "incident:AN012"}, {"id": "flag:14"}, {"id": "stat:fleet"}],
             "citations": ["flag:14", "incident:AN012"]},
            {"role": "user", "content": "q2"}, {"role": "assistant", "status": "error", "content": ""}]
    pr = prior_answer(hist)
    assert pr.text == "ok [flag:14][incident:AN012]" and [s["id"] for s in pr.cited] == ["flag:14", "incident:AN012"]
    assert prior_answer([{"role": "user", "content": "q"}]) is None


async def test_follow_up_fetches_the_previous_answers_subject():
    from app.services.chat.retrieval import Prior
    repo = FakeChatRepo()
    prior = Prior(text="Two missions hid failures: M014 [incident:AN012] and M007 [flag:14].",
                  cited=[{"id": "flag:14", "refs": {"flag_id": 14, "mission_id": "M007"}}])
    r = await Retriever(repo, None).retrieve("What about the second one?", "Which missions hid failures?",
                                             prior=prior)
    (dig,) = [a for n, a in repo.calls if n == "entity_digests"][:1]
    assert dig["missions"] == ["M014", "M007"] and dig["flags"] == [14]
    ids = [s["id"] for s in r.sources]
    assert "mission:M007" in ids and "flag:14" in ids
    # answer-only digests rank with the fresh results (not ahead of everything), so documents still make it in
    assert ids.index("incident:AN012") < ids.index("mission:M007")


async def test_question_entities_still_rank_first():
    from app.services.chat.retrieval import Prior
    repo = FakeChatRepo()
    r = await Retriever(repo, None).retrieve("And WP06?", prior=Prior(text="M007 hid failures.", cited=[]))
    ids = [s["id"] for s in r.sources]
    assert ids[0] == "location:WP06" and "mission:M007" in ids


async def test_carry_over_sources_join_after_fresh_and_drop_first():
    from app.services.chat.retrieval import Prior
    old = {"id": "event:act-A0099", "n": 4, "origin": "rag", "kind": "event", "title": "A0099 failed at WP05",
           "snippet": "A0099 failed with LOCALIZATION_FAILURE at 31% battery.", "score": 0.4,
           "refs": {"action_id": "A0099"}}
    again = {"id": "incident:AN012", "n": 1, "origin": "rag", "kind": "incident", "title": "OLD TITLE",
             "snippet": "OLD SNIPPET 999", "score": 0.1, "refs": {}}
    prior = Prior(text="It failed [event:act-A0099][incident:AN012].", cited=[old, again])
    repo = FakeChatRepo()
    r = await Retriever(repo, None).retrieve("why?", prior=prior)
    ids = [s["id"] for s in r.sources]
    assert "event:act-A0099" in ids and ids.count("incident:AN012") == 1
    assert ids.index("event:act-A0099") == len(ids) - 7                     # after the fresh ones, before the stats
    carried = r.sources[ids.index("event:act-A0099")]
    assert carried["title"] == "A0099 failed at WP05" and carried["refs"] == {"action_id": "A0099"}
    assert "31% battery" in r.texts[ids.index("event:act-A0099")]
    an = ids.index("incident:AN012")
    assert "OLD" not in r.texts[an] and r.sources[an]["title"] != "OLD TITLE"   # the fresh text wins
    assert check("31% [event:act-A0099]", r.sources, r.texts, "")["unverified_numbers"] == []
    # over budget: the carry-over goes before any fresh source
    tight = await Retriever(FakeChatRepo(), None, max_chars=len(r.block) - 50).retrieve("why?", prior=prior)
    tids = [s["id"] for s in tight.sources]
    assert "event:act-A0099" not in tids and set(tids) == set(ids) - {"event:act-A0099"}


# ── chat.2: gaps ─────────────────────────────────────────────────────────────────────────────────
async def test_gaps_are_readable_in_meta_and_logged_at_warning(caplog):
    import logging
    caplog.set_level(logging.WARNING, logger="backend.chat.retrieval")
    repo = FakeChatRepo()
    repo.fail = {"match_graph_nodes"}
    r = await Retriever(repo, None).retrieve("HX-217?", "earlier")
    assert r.meta()["gaps"] == r.gaps
    assert "no embedding client: full-text search only" in r.gaps
    assert "graph match failed: DatabaseUnavailable: main database is not reachable" in r.gaps
    assert "follow-up graph match failed: DatabaseUnavailable: main database is not reachable" in r.gaps
    warned = [rec.getMessage() for rec in caplog.records if rec.levelno == logging.WARNING]
    assert len(warned) == len(r.gaps) and all("chat retrieval degraded" in w for w in warned)
