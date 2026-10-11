"""Retrieval unit gaps (docs/chat-contract.md §2, steps 2-8): entity regexes on the kit's real ids, the 60 s snapshot
cache on a fake clock, the budget deadline (slow steps cancelled, `gaps` recorded), cancellation on Stop, the merge of
sources that share an id, the context cap, and the carry-over cap. Fast and deterministic: no sleeps longer than the
budget under test, and the clock is faked where a TTL is involved."""
import asyncio
import time
import types

import pytest

from app.services.chat import retrieval as R
from app.services.chat.retrieval import (CARRY_MAX, DIGEST_CHARS, SNAPSHOT_TTL_S, Prior, Retriever, _Builder,
                                         extract_entities, prior_answer)

from .fakes_chat import FakeChatRepo


# ── step 3: entity regexes on real kit ids ──────────────────────────────────────────────────────
KIT_ASSETS = ["HX-201", "PUMP-205", "PANEL-203", "VALVE-207", "GAUGE-209", "MOTOR-206", "COMP-214"]


def test_every_kit_asset_prefix_is_recognised():
    e = extract_entities("Compare " + ", ".join(KIT_ASSETS) + " and panel-212")
    assert e["assets"] == KIT_ASSETS + ["PANEL-212"]


@pytest.mark.parametrize("text,expected", [
    ("What failed on M001?", {"missions": ["M001"]}),
    ("M001-M005 and m020's battery", {"missions": ["M001", "M005", "M020"]}),
    ("HX-201's gauge", {"assets": ["HX-201"]}),
    ("at WP05 and wp12", {"locations": ["WP05", "WP12"]}),
    ("explain flag 14", {"flags": [14]}),
    ("explain #14", {"flags": [14]}),
    ("Flag #7, flag: 3 and FLAG9", {"flags": [7, 3, 9]}),
    ("as cited in [flag:22]", {"flags": [22]}),               # the citation id form counts too
])
def test_entity_regexes_on_real_ids(text, expected):
    e = extract_entities(text)
    want = {"missions": [], "assets": [], "locations": [], "flags": []}
    want.update(expected)
    assert e == want, text


@pytest.mark.parametrize("text", [
    "Does V2 beat V1?", "On 2026-09-01 at 18:00", "Priority P1 and P2", "62.5% at 25%", "A0133 and AN012",
    "event:act-A0001 and manual:p52-1", "gfx1100 GPUs", "WP5 or WP100", "M07, M0071, AM001", "HX-20, HX-2017, X-201",
    "ISO-9001 and COVID-19", "flags were raised", "flagged 12 actions",
])
def test_entity_regexes_ignore_lookalikes(text):
    assert extract_entities(text) == {"missions": [], "assets": [], "locations": [], "flags": []}, text


def test_entities_dedupe_in_order_across_texts_and_ignore_none():
    e = extract_entities("M007 then M003", None, "", "m007 again, flag 2 and #2")
    assert e["missions"] == ["M007", "M003"] and e["flags"] == [2]


# ── step 4: fleet_snapshot cached 60 s (fake clock) ─────────────────────────────────────────────
@pytest.fixture
def clock(monkeypatch):
    """Fakes `time.monotonic` inside the retrieval module only (asyncio keeps the real clock)."""
    now = [1000.0]
    monkeypatch.setattr(R, "time", types.SimpleNamespace(monotonic=lambda: now[0], perf_counter=time.perf_counter))
    return now


def _snapshots(repo):
    return sum(1 for n, _ in repo.calls if n == "fleet_snapshot")


async def test_snapshot_is_cached_for_60_seconds(clock):
    repo = FakeChatRepo()
    r = Retriever(repo, None)
    first = await r.snapshot()
    clock[0] += SNAPSHOT_TTL_S - 0.5
    assert await r.snapshot() is first and _snapshots(repo) == 1
    repo.snapshot["missions"] = 21
    clock[0] += 1.0                                       # 60.5 s after the first read: stale
    fresh = await r.snapshot()
    assert _snapshots(repo) == 2 and fresh["missions"] == 21
    assert await r.snapshot() is fresh and _snapshots(repo) == 2


async def test_snapshot_cache_is_shared_across_turns_and_a_failure_is_not_cached(clock):
    repo = FakeChatRepo()
    r = Retriever(repo, None)
    await r.retrieve("fleet health?")
    await r.retrieve("and again?")
    assert _snapshots(repo) == 1                          # the second turn used the cache
    clock[0] += SNAPSHOT_TTL_S + 1
    repo.fail = {"fleet_snapshot"}
    out = await r.retrieve("and now?")
    assert any(g.startswith("fleet stats failed:") for g in out.gaps)
    assert not any(s["id"].startswith("stat:") for s in out.sources)
    repo.fail = set()
    out = await r.retrieve("and now?")                    # the failure did not poison the cache
    assert _snapshots(repo) == 3 and any(s["id"] == "stat:fleet" for s in out.sources)


# ── the budget deadline ─────────────────────────────────────────────────────────────────────────
def _slow(repo, name, seconds, cancelled: list):
    orig = getattr(repo, name)

    async def slow(*a, **kw):
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            cancelled.append(name)
            raise
        return await orig(*a, **kw)
    setattr(repo, name, slow)


async def _settle_loop():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_a_slow_step_is_cancelled_not_left_running():
    repo, cancelled = FakeChatRepo(), []
    _slow(repo, "search_documents", 30, cancelled)
    _slow(repo, "entity_digests", 30, cancelled)
    t0 = time.perf_counter()
    r = await Retriever(repo, None, budget_s=0.2).retrieve("HX-217 at WP06?")
    await _settle_loop()
    assert time.perf_counter() - t0 < 2.0
    assert sorted(cancelled) == ["entity_digests", "search_documents"]
    assert "document search timed out (retrieval budget)" in r.gaps
    assert "entity digests timed out (retrieval budget)" in r.gaps
    assert any(s["id"] == "stat:fleet" for s in r.sources)      # what finished in time is still used


async def test_the_neighbourhood_walk_is_skipped_or_cut_when_the_budget_is_spent():
    """Step 1 eats the whole budget, so the graph walk (step 2) has no time left: it is skipped (or, if the
    deadline timer fired a hair early, cut at once). Either way a readable gap names it and the turn goes on."""
    repo, cancelled = FakeChatRepo(), []
    _slow(repo, "search_documents", 30, cancelled)              # the graph match is fast, so seeds exist
    r = await Retriever(repo, None, budget_s=0.15).retrieve("HX-217?")
    await _settle_loop()
    assert any("graph neighbourhood" in g and "budget" in g for g in r.gaps), r.gaps
    assert "search_documents" in cancelled
    assert not any(s.get("path") for s in r.sources)            # no neighbourhood rows made it in


async def test_slow_embedding_falls_back_to_fulltext_with_a_gap():
    class SlowEmbed:
        async def embed_query(self, q, request_id=None, timeout_s=None):
            await asyncio.sleep(30)

    repo = FakeChatRepo()
    r = await Retriever(repo, SlowEmbed(), search_embed_timeout_s=0.05, budget_s=8).retrieve("HX-217?")
    assert r.mode == "fulltext" and "embedding slower than 0.1s: full-text search only" in r.gaps
    assert all(a["vec"] is None for n, a in repo.calls if n in ("search_documents", "match_graph_nodes"))


async def test_a_working_embedding_is_hybrid_and_sent_as_a_vector_literal():
    class Embed:
        async def embed_query(self, q, request_id=None, timeout_s=None):
            return [0.5, -0.25, 1.0]

    repo = FakeChatRepo()
    r = await Retriever(repo, Embed()).retrieve("HX-217?")
    assert r.mode == "hybrid" and r.gaps == []
    docs = [a for n, a in repo.calls if n == "search_documents"]
    assert docs[0]["vec"] == "[0.5,-0.25,1]"


@pytest.mark.parametrize("phase", ["queries", "embedding"])
async def test_stop_during_retrieval_cancels_every_pending_query(phase):
    """The operator pressed Stop (the turn's task is cancelled): no query is left running against the DB."""
    repo, cancelled = FakeChatRepo(), []
    _slow(repo, "fleet_snapshot", 30, cancelled)
    _slow(repo, "search_documents", 30, cancelled)
    embed = None
    if phase == "embedding":
        class Embed:
            async def embed_query(self, q, request_id=None, timeout_s=None):
                await asyncio.sleep(30)
        embed = Embed()
    task = asyncio.ensure_future(Retriever(repo, embed, search_embed_timeout_s=20, budget_s=60).retrieve(
        "HX-217?", "earlier question"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _settle_loop()
    assert "fleet_snapshot" in cancelled
    if phase == "queries":
        assert "search_documents" in cancelled


# ── step 5: sources that share an id merge ──────────────────────────────────────────────────────
def test_a_digest_and_a_graph_node_with_one_id_merge_into_one_source():
    b = _Builder()
    digest = {"id": "location:WP06", "origin": "sql", "kind": "location", "title": "Waypoint WP06 · Turbine Hall",
              "snippet": "turbine (indoor).", "score": None, "refs": {"location_id": "WP06"}}
    node = {"id": "location:WP06", "origin": "graph", "kind": "location", "title": "Turbine Hall South",
            "snippet": "graph summary", "score": 0.81, "refs": {"location_id": "WP06", "asset_id": "HX-217"},
            "path": "asset:HX-217 -located_at-> location:WP06"}
    b.add(digest, "location_id: WP06\nsuccess_rate_pct: 62.5", rank=(0, 1))
    b.add(node, "WP06 has a 62.5% action success rate.", rank=(3, 0))
    b.add(node, "WP06 has a 62.5% action success rate.", rank=(3, 4))    # the same text again: kept once
    (item,) = b.items.values()
    src = item["src"]
    assert src["origin"] == "sql" and src["title"].startswith("Waypoint WP06")    # the richer origin wins
    assert src["path"] == node["path"]                                             # the graph path is kept
    assert src["score"] == 0.81 and src["refs"] == {"location_id": "WP06", "asset_id": "HX-217"}
    assert item["rank"] == (0, 1)                                                  # the better rank
    assert item["text"].count("62.5% action success rate") == 1 and item["text"].startswith("location_id: WP06")


def test_a_graph_node_seen_first_still_yields_the_digest_origin_and_keeps_its_path():
    b = _Builder()
    b.add({"id": "asset:HX-217", "origin": "graph", "kind": "asset", "title": "HX", "snippet": "", "score": 0.9,
           "refs": {}, "path": "a -r-> asset:HX-217"}, "node text", rank=(1, 0, 1))
    b.add({"id": "asset:HX-217", "origin": "sql", "kind": "asset", "title": "Asset HX-217", "snippet": "",
           "score": None, "refs": {"asset_id": "HX-217"}}, "digest text", rank=(0, 1))
    src = b.items["asset:HX-217"]["src"]
    assert src["origin"] == "sql" and src["path"] == "a -r-> asset:HX-217" and src["refs"]["asset_id"] == "HX-217"


def test_merged_text_is_capped():
    b = _Builder()
    s = {"id": "mission:M007", "origin": "sql", "kind": "mission", "title": "M007", "snippet": "", "score": None,
         "refs": {}}
    b.add(s, "a" * 1500, rank=(0, 1))
    b.add({**s, "origin": "graph"}, "b" * 1500, rank=(1, 0))
    assert len(b.items["mission:M007"]["text"]) == DIGEST_CHARS


async def test_retrieved_ids_are_unique_and_numbered_in_block_order():
    repo = FakeChatRepo()
    repo.docs.append(dict(repo.docs[0]))                              # the same document twice
    r = await Retriever(repo, None).retrieve("HX-217 and WP06 and M007 and flag 14?", "earlier: HX-217")
    ids = [s["id"] for s in r.sources]
    assert len(ids) == len(set(ids)) and [s["n"] for s in r.sources] == list(range(1, len(ids) + 1))
    positions = [r.block.index('<source id="%s"' % i) for i in ids]
    assert positions == sorted(positions)                             # `n` is the block order
    assert r.texts == [t for t in r.texts if t.startswith('<source id="')] and len(r.texts) == len(ids)


# ── the context cap ─────────────────────────────────────────────────────────────────────────────
async def test_the_snapshot_stays_even_when_it_alone_is_over_the_cap():
    r = await Retriever(FakeChatRepo(), None, max_chars=300).retrieve("HX-217 at WP06?")
    ids = [s["id"] for s in r.sources]
    assert ids == ["stat:fleet", "stat:battery_bands", "stat:config", "stat:locations", "stat:assets",
                   "stat:failure_types"]
    assert r.block.startswith("<sources>\n") and r.block.endswith("\n</sources>")


async def test_the_default_cap_holds_with_many_large_candidates():
    repo = FakeChatRepo()
    repo.docs = [{"doc_id": "incident:AN%03d" % i, "kind": "incident", "title": "Incident %d" % i,
                  "content": "word " * 600, "score": 1 - i / 100} for i in range(8)]
    repo.nodes = [{"node_id": "topic:t%d" % i, "kind": "topic", "label": "t%d" % i, "summary": "s " * 300,
                   "doc_id": None, "score": 0.9, "match": "text"} for i in range(6)]
    r = await Retriever(repo, None).retrieve("everything about M007, HX-217, WP06 and flag 14")
    assert len(r.block) <= R.MAX_CONTEXT_CHARS
    ids = [s["id"] for s in r.sources]
    assert "stat:fleet" in ids and ids[0] == "mission:M007"           # named entities first, snapshot kept
    assert len([i for i in ids if i.startswith("incident:")]) < 8    # something had to go


# ── step 7: carry-over cap ──────────────────────────────────────────────────────────────────────
def test_carry_over_is_capped_at_six_in_citation_order_and_skips_error_turns():
    sources = [{"id": "event:act-A%04d" % i, "title": "E%d" % i, "snippet": "s%d" % i} for i in range(10)]
    cites = ["event:act-A%04d" % i for i in (9, 8, 7, 9, 6, 5, 4, 3, 2)] + ["stat:gone"]
    hist = [{"role": "user", "content": "q"},
            {"role": "assistant", "status": "complete", "content": "answer", "sources": sources, "citations": cites},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "status": "error", "content": "partial", "sources": sources[:1],
             "citations": ["event:act-A0000"]}]
    p = prior_answer(hist)
    assert len(p.cited) == CARRY_MAX == 6
    assert [s["id"] for s in p.cited] == ["event:act-A%04d" % i for i in (9, 8, 7, 6, 5, 4)]


async def test_carry_over_never_pushes_out_fresh_sources_or_the_snapshot():
    carried = [{"id": "note:%d" % i, "origin": "rag", "kind": "operator_note", "title": "Note %d" % i,
                "snippet": "x " * 130, "refs": {}} for i in range(6)]
    base = await Retriever(FakeChatRepo(), None).retrieve("why?")
    r = await Retriever(FakeChatRepo(), None, max_chars=len(base.block) + 400).retrieve(
        "why?", prior=Prior(text="see notes", cited=carried))
    fresh = [s["id"] for s in base.sources]
    ids = [s["id"] for s in r.sources]
    assert [i for i in ids if not i.startswith("note:")] == fresh    # every fresh source kept, same order
    kept_notes = [i for i in ids if i.startswith("note:")]
    assert 0 < len(kept_notes) < 6 and kept_notes == ["note:%d" % i for i in range(len(kept_notes))]
    assert len(r.block) <= len(base.block) + 400
