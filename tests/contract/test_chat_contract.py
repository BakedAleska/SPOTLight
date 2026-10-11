"""Cross-layer contract for the AI Assistance chat (docs/chat-contract.md, chat.2): backend side.

1. Golden SSE transcripts. Each scenario drives a REAL backend turn in-process: the FastAPI app, ChatService,
   retrieval, the Gemini SSE parser, grounding and storage, against backend/tests/fakes_chat.py (FakeChatRepo plus a
   respx-mocked Gemini). There is no network and no GEMINI_API_KEY. The SSE body is normalized (uuids, timestamps,
   latencies, request ids) and compared with tests/contract/chat_golden/<scenario>.sse. web/tests/chat-contract.test.ts
   feeds the SAME files through the real Next.js proxy route, the web SSE parser and the chat store, and checks the
   thread the operator would see. If the backend renames a field or changes an event's shape, this file fails. If
   someone regenerates the goldens anyway, the web test fails.
2. Every event is also checked against the contract's key sets (§3, §4), so a blind regeneration can't hide drift.
3. The shared citation fixtures (tests/contract/chat_citations.json), the same file web/tests/chat-logic.test.ts
   reads, are run through the backend's grounding module.

Regenerate after an intended change, then run the web side and review the diff:
    UPDATE_GOLDEN=1 make test-chat-contract && (cd web && npm run test:chat) && git diff tests/contract/chat_golden

Runs in the backend venv (`make test-chat-contract`; CI job `chat-contract`). In another venv (the vision one runs
`pytest tests/contract`) it skips.
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
GOLDEN = Path(__file__).resolve().parent / "chat_golden"
CITATIONS = Path(__file__).resolve().parent / "chat_citations.json"
UPDATE = os.environ.get("UPDATE_GOLDEN") == "1"

for mod in ("respx", "psycopg_pool", "fastapi"):
    pytest.importorskip(mod, reason="backend deps missing: run in the backend venv (make test-chat-contract)")

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _backend_tests():
    """backend/tests as the package `chat_backend_tests`. The repo-root `tests/` dir (and amd/*/tests, which the
    vision contract test puts on sys.path) would shadow a plain `import tests`."""
    name = "chat_backend_tests"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, BACKEND / "tests" / "__init__.py",
                                                      submodule_search_locations=[str(BACKEND / "tests")])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return importlib.import_module(name + ".conftest"), importlib.import_module(name + ".fakes_chat")


conftest, fakes = _backend_tests()

import respx  # noqa: E402

from app.services.chat.grounding import check, citation_groups, citations_in  # noqa: E402


# ── normalization ─────────────────────────────────────────────────────────────────────────────────
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")
MS_RE = re.compile(r'"(latency_ms|thinking_ms)":\d+')
MS_VALUE = {"latency_ms": 1000, "thinking_ms": 2000}
REQUEST_ID = "r-golden"


def golden_uuid(i: int) -> str:
    return "00000000-0000-4000-8000-%012d" % i


def golden_time(i: int) -> str:
    return "2026-10-10T12:00:%02dZ" % i


class Normalizer:
    """Replaces what changes between runs, keeping the backend's exact framing (event/data lines, blank-line
    separators, compact JSON). uuids and timestamps map to stable values in order of first appearance, shared
    across every body one normalizer sees (a turn's SSE and its reload agree on ids), so "same id" and "later
    than" still hold. Latencies become constants; the request id becomes r-golden."""

    def __init__(self):
        self.ids: Dict[str, str] = {}
        self.times: Dict[str, str] = {}

    def __call__(self, text: str) -> str:
        rid = re.search(r'"request_id":"([^"]+)"', text)
        if rid:
            text = text.replace('"%s"' % rid.group(1), '"%s"' % REQUEST_ID)
        text = UUID_RE.sub(lambda m: self.ids.setdefault(m.group(0), golden_uuid(len(self.ids) + 1)), text)
        text = ISO_RE.sub(lambda m: self.times.setdefault(m.group(0), golden_time(len(self.times) + 1)), text)
        return MS_RE.sub(lambda m: '"%s":%d' % (m.group(1), MS_VALUE[m.group(1)]), text)


def normalize(text: str) -> str:
    return Normalizer()(text)


def test_normalize_is_stable_and_keeps_identity():
    a, b = "3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b", "11111111-2222-4333-8444-555555555555"
    raw = ('event: meta\ndata: {"conversation_id":"%s","request_id":"r-abc123","x":"%s","y":"%s",'
           '"created_at":"2026-10-10T12:00:07Z","updated_at":"2026-10-10T12:00:09.5+00:00","latency_ms":87,'
           '"thinking_ms":null}\n\n' % (a, b, a))
    out = normalize(raw)
    assert out == normalize(out)                                  # idempotent: a golden file normalizes to itself
    assert golden_uuid(1) in out and golden_uuid(2) in out and a not in out and b not in out
    assert out.count(golden_uuid(1)) == 2                         # the same id stays the same id
    assert '"request_id":"r-golden"' in out and '"latency_ms":1000' in out and '"thinking_ms":null' in out
    assert golden_time(1) in out and golden_time(2) in out


# ── scenarios ─────────────────────────────────────────────────────────────────────────────────────
QUESTION = "What happened to HX-217's gauge?"


class Scenario:
    def __init__(self, name: str, *, model: str, mode: str, setup: Callable[[Any], None],
                 title_model: Optional[str] = None, question: str = QUESTION):
        self.name, self.model, self.mode, self.setup = name, model, mode, setup
        self.title_model, self.question = title_model, question


def _fast(mock):
    fakes.gemini_stream(mock)                     # ANSWER_PARTS: [incident:AN012] and [asset:HX-217, incident:AN012]


def _thinking(mock):
    fakes.gemini_stream(mock, chunks=fakes.answer_chunks(
        thoughts=["Looking at the gauge source. ", "Comparing to the 380 psi band."]))


def _rate_limited(mock):
    fakes.gemini_stream(mock, status=429, error={"error": {
        "code": 429, "status": "RESOURCE_EXHAUSTED",
        "message": "You exceeded your current quota. Please retry in 41.53s.",
        "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                     "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "41s"}]}})


def _title(mock):
    fakes.gemini_stream(mock)
    fakes.gemini_title(mock, text='"HX-217 Gauge Reading."')


def _general(mock):
    # chat.4 (§10): small talk is answered without retrieval: no sources, the general badge
    fakes.gemini_stream(mock, chunks=fakes.answer_chunks(parts=[
        "Hi! I'm SPOTbot. ", "Ask me about a mission, an asset or a flag and I'll look it up."]))


def _ungrounded(mock):
    # an unknown id ([incident:AN999]) and a figure in no source (12.3%): the "bad" badge, with both lists filled
    fakes.gemini_stream(mock, chunks=fakes.answer_chunks(parts=[
        "Fleet success fell to 12.3% ", "[stat:fleet] after the gauge fault [incident:AN999]."]))


SCENARIOS = [
    Scenario("fast_cited", model="gemini-3.8-flash", mode="fast", setup=_fast),
    Scenario("thinking", model="gemini-3.5-flash", mode="thinking", setup=_thinking),
    Scenario("rate_limited", model="gemini-3.8-flash", mode="fast", setup=_rate_limited),
    Scenario("first_exchange_title", model="gemini-3.8-flash", mode="fast", setup=_title,
             title_model="gemini-3.1-flash-lite"),
    Scenario("ungrounded", model="gemini-3.5-flash-lite", mode="fast", setup=_ungrounded,
             question="How is the fleet doing?"),
    Scenario("general", model="gemini-3.8-flash", mode="fast", setup=_general, question="hi"),   # chat.4
]


def run_turn(sc: Scenario) -> Tuple[str, Dict[str, Any]]:
    """One first exchange in a fresh conversation, through the real app. Returns the normalized SSE body and the
    normalized GET /conversations/{id} that a reload reads afterwards."""
    async def main() -> Tuple[str, str]:
        h, _repo = fakes.chat_harness(lambda **kw: conftest.Harness(conftest.make_settings(**kw)),
                                      chat_title_model=sc.title_model)
        try:
            with respx.mock(assert_all_called=False) as mock:
                fakes.embed_ok(mock)
                sc.setup(mock)
                r = await h.client.post("/api/v1/chat/conversations", json={"model": sc.model, "mode": sc.mode})
                assert r.status_code == 201, r.text
                cid = r.json()["conversation_id"]
                r = await h.client.post("/api/v1/chat/conversations/%s/messages" % cid,
                                        json={"content": sc.question, "model": sc.model, "mode": sc.mode})
                assert r.status_code == 200, r.text
                assert r.headers["content-type"].startswith("text/event-stream")
                d = await h.client.get("/api/v1/chat/conversations/%s" % cid)
                assert d.status_code == 200, d.text
                return r.text, d.text
        finally:
            await h.client.aclose()
    sse_text, detail_text = asyncio.run(main())
    norm = Normalizer()
    return norm(sse_text), json.loads(norm(detail_text))


def get_models() -> Dict[str, Any]:
    """GET /chat/models with the default catalog (what the web's model picker and labels read)."""
    async def main() -> str:
        h, _repo = fakes.chat_harness(lambda **kw: conftest.Harness(conftest.make_settings(**kw)))
        try:
            r = await h.client.get("/api/v1/chat/models")
            assert r.status_code == 200, r.text
            return r.text
        finally:
            await h.client.aclose()
    return json.loads(asyncio.run(main()))


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    assert path.exists(), "missing %s: run with UPDATE_GOLDEN=1" % path.relative_to(ROOT)
    return json.loads(path.read_text(encoding="utf-8"))


def parse(text: str) -> List[Tuple[str, Any]]:
    """[(event, data)], keep-alive comments dropped. Strict: every block is `event:` + one `data:` line."""
    out = []
    for block in text.split("\n\n"):
        if not block or block.startswith(":"):
            continue
        lines = block.split("\n")
        assert len(lines) == 2 and lines[0].startswith("event: ") and lines[1].startswith("data: "), block
        out.append((lines[0][7:], json.loads(lines[1][6:])))
    return out


# ── contract key sets (§3, §4) ────────────────────────────────────────────────────────────────────
MESSAGE = {"message_id", "conversation_id", "seq", "role", "content", "thinking", "thinking_ms", "model", "mode",
           "status", "sources", "citations", "grounding", "usage", "latency_ms", "error", "error_code", "created_at"}
CONVERSATION = {"conversation_id", "title", "title_source", "model", "mode", "pinned", "created_at", "updated_at",
                "message_count", "mission_id", "mission"}                       # chat.3: mission_id, mission
SOURCE = {"id", "n", "origin", "kind", "title", "snippet", "score", "refs"}
SOURCE_OPTIONAL = {"path"}
REFS = {"mission_id", "asset_id", "location_id", "action_id", "flag_id"}
GROUNDING = {"grounded", "cited_ids", "unknown_ids", "unverified_numbers", "source_count", "retrieval_mode",
             "graph_seeds", "mode"}                     # chat.4: mode
GROUNDING_GENERAL = GROUNDING | {"fleet_ids"}            # chat.4 (§10): a general answer adds the plant ids it names
USAGE = {"prompt_tokens", "output_tokens", "thoughts_tokens"}
RETRIEVAL = {"mode", "graph_seeds", "latency_ms", "counts", "gaps", "mission_id",   # chat.3: mission_id
             "answer_mode", "route"}                                            # chat.4 (§10)
ERROR_CODES = {"rate_limited", "model_unavailable", "gemini_auth", "blocked", "upstream_error", "retrieval_failed",
               "internal_error"}
EVENT_KEYS = {"meta": {"conversation_id", "request_id", "model", "mode", "user_message"},
              "sources": {"sources", "retrieval"}, "thinking": {"delta"}, "delta": {"delta"},
              "done": {"message", "conversation"}, "title": {"conversation_id", "title"},
              "error": {"code", "detail", "message"}}
ORDER = re.compile(r"^meta sources (thinking )*(delta )*((thinking |delta )*delta done( title)?|error)$")


def check_source(s: Dict[str, Any]) -> None:
    assert SOURCE <= set(s) <= SOURCE | SOURCE_OPTIONAL, set(s) ^ SOURCE
    assert s["origin"] in ("rag", "graph", "sql") and isinstance(s["n"], int) and len(s["snippet"]) <= 280
    assert s["refs"] is None or set(s["refs"]) <= REFS, s["refs"]


def check_message(m: Dict[str, Any], role: str) -> None:
    assert set(m) == MESSAGE, set(m) ^ MESSAGE
    assert m["role"] == role and m["status"] in ("complete", "error", "stopped")
    for s in m["sources"]:
        check_source(s)
    if role == "user":
        assert m["sources"] == [] and m["citations"] == [] and m["grounding"] is None and m["usage"] is None
        assert m["thinking"] is None
        return
    if m["grounding"] is not None:
        want = GROUNDING_GENERAL if m["grounding"].get("mode") == "general" else GROUNDING
        assert set(m["grounding"]) == want, set(m["grounding"]) ^ want
        assert m["grounding"]["mode"] in ("grounded", "general")
        assert m["citations"] == m["grounding"]["cited_ids"]
    if m["usage"] is not None:
        assert set(m["usage"]) == USAGE
    assert (m["error_code"] is None) == (m["status"] != "error")


def check_events(evs: List[Tuple[str, Any]]) -> None:
    names = " ".join(n for n, _ in evs)
    assert ORDER.match(names), "event order breaks contract §4: " + names
    for name, data in evs:
        keys = EVENT_KEYS[name] | ({"retry_after_s"} if name == "error" else set())
        assert EVENT_KEYS[name] <= set(data) <= keys, (name, set(data) ^ EVENT_KEYS[name])
        if name == "meta":
            check_message(data["user_message"], "user")
        elif name == "sources":
            assert set(data["retrieval"]) == RETRIEVAL and set(data["retrieval"]["counts"]) == {"rag", "graph", "sql"}
            assert data["retrieval"]["answer_mode"] in ("grounded", "general")
            if data["retrieval"]["answer_mode"] == "general":        # chat.4: no retrieval reaches the model
                assert data["sources"] == [] and data["retrieval"]["mode"] is None
            for s in data["sources"]:
                check_source(s)
        elif name in ("thinking", "delta"):
            assert isinstance(data["delta"], str)
        elif name == "done":
            check_message(data["message"], "assistant")
            assert set(data["conversation"]) == CONVERSATION, set(data["conversation"]) ^ CONVERSATION
        elif name == "error":
            assert data["code"] in ERROR_CODES and isinstance(data["detail"], str)
            assert "retry_after_s" not in data or isinstance(data["retry_after_s"], int)
            if data["message"] is not None:
                check_message(data["message"], "assistant")
                assert data["message"]["error_code"] == data["code"]


# ── tests ─────────────────────────────────────────────────────────────────────────────────────────
# Scenarios whose reload (GET /conversations/{id}) is also a golden: a stored error renders from error_code.
RELOAD_GOLDENS = {"rate_limited"}


@pytest.mark.parametrize("sc", SCENARIOS, ids=[s.name for s in SCENARIOS])
def test_golden_sse_transcript(sc: Scenario):
    actual, detail = run_turn(sc)
    evs = parse(actual)
    check_events(evs)
    # A streamed turn and a reloaded thread serialize identically (schemas/chat.py), so the web can swap one for
    # the other: the reload's messages are exactly meta.user_message + the done/error message.
    final = dict(evs).get("done", dict(evs).get("error"))["message"]
    assert detail["messages"] == [dict(evs)["meta"]["user_message"], final]
    assert set(detail["conversation"]) == CONVERSATION
    path = GOLDEN / (sc.name + ".sse")
    reload_path = GOLDEN / (sc.name + ".detail.json")
    if UPDATE:
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(actual, encoding="utf-8", newline="")
        if sc.name in RELOAD_GOLDENS:
            write_json(reload_path, detail)
        return
    assert path.exists(), "missing %s: run with UPDATE_GOLDEN=1" % path.relative_to(ROOT)
    expected = path.read_text(encoding="utf-8")
    # Structure first, for a readable diff of what changed in which event; then the bytes (framing, key order).
    assert evs == parse(expected), (
        "%s: the backend's SSE events changed (contract §4). If intended: UPDATE_GOLDEN=1, then make the web side "
        "(web/tests/chat-contract.test.ts) pass too" % path.name)
    assert actual == expected, ("%s: same events, but the framing or JSON serialization changed. If intended: "
                                "UPDATE_GOLDEN=1" % path.name)
    if sc.name in RELOAD_GOLDENS:
        assert detail == read_json(reload_path), "%s changed (contract §4 GET /conversations/{id})" % reload_path.name


def test_golden_models_catalog():
    """GET /chat/models (§4): the web's picker, model labels and "Try with <other model>" read it."""
    actual = get_models()
    assert set(actual) == {"configured", "default_model", "default_mode", "models", "modes"}
    for m in actual["models"]:
        assert set(m) == {"id", "label", "description", "family", "thinking_summaries"}
    for m in actual["modes"]:
        assert set(m) == {"id", "label", "description"}
    path = GOLDEN / "models.json"
    if UPDATE:
        write_json(path, actual)
        return
    assert actual == read_json(path), "models.json changed (contract §4 GET /chat/models). If intended: UPDATE_GOLDEN=1"


def test_golden_scenarios_cover_the_contract():
    """The goldens the web test replays cover every event the UI handles, and both thread endings."""
    seen = set()
    for sc in SCENARIOS:
        seen |= {n for n, _ in parse((GOLDEN / (sc.name + ".sse")).read_text(encoding="utf-8"))}
    assert seen == set(EVENT_KEYS)
    names = {s.name for s in SCENARIOS}
    expected = {n + ".sse" for n in names} | {n + ".detail.json" for n in RELOAD_GOLDENS} | {"models.json"}
    assert {p.name for p in GOLDEN.iterdir() if not p.name.startswith(".")} == expected, "stale or missing goldens"


def test_golden_semantics():
    """What the web test asserts, pinned on the backend side too, so a regenerated golden can't quietly drift."""
    ev = {sc.name: parse((GOLDEN / (sc.name + ".sse")).read_text(encoding="utf-8")) for sc in SCENARIOS}
    done = dict(ev["fast_cited"])["done"]["message"]
    assert done["citations"] == ["incident:AN012", "asset:HX-217"] and done["grounding"]["grounded"] is True
    thinking = dict(ev["thinking"])["done"]["message"]
    assert thinking["thinking"] == "Looking at the gauge source. Comparing to the 380 psi band."
    err = dict(ev["rate_limited"])["error"]
    assert err["code"] == "rate_limited" and err["retry_after_s"] == 41 and err["message"]["status"] == "error"
    assert dict(ev["first_exchange_title"])["title"]["title"] == "HX-217 Gauge Reading"
    g = dict(ev["ungrounded"])["done"]["message"]["grounding"]
    assert g["unknown_ids"] == ["incident:AN999"] and g["unverified_numbers"] == ["12.3%"] and not g["grounded"]
    general = dict(ev["general"])
    assert general["sources"]["sources"] == [] and general["sources"]["retrieval"]["answer_mode"] == "general"
    assert general["sources"]["retrieval"]["route"] == "small_talk"
    gm = general["done"]["message"]
    assert gm["sources"] == [] and gm["citations"] == [] and gm["grounding"]["mode"] == "general"
    assert gm["grounding"]["fleet_ids"] == [] and gm["grounding"]["grounded"] is False


# ── shared citation fixtures (contract §2, chat.2) ────────────────────────────────────────────────
FIXTURES = json.loads(CITATIONS.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", FIXTURES["cases"], ids=[c["name"] for c in FIXTURES["cases"]])
def test_shared_citation_fixture(case):
    sources = FIXTURES["sources"]
    assert citations_in(case["text"], sources) == (case["cited_ids"], case["unknown_ids"])
    assert [{"raw": g["raw"], "chips": g["chips"]} for g in citation_groups(case["text"], sources)] == case["groups"]
    out = check(case["text"], [{"id": i} for i in sources], [], "")
    assert out["cited_ids"] == case["cited_ids"] and out["unknown_ids"] == case["unknown_ids"]
    assert out["grounded"] is (bool(case["cited_ids"]) and not case["unknown_ids"])


def test_shared_citation_fixtures_loaded():
    assert len(FIXTURES["cases"]) >= 13 and FIXTURES["sources"]
