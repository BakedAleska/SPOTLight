"""In-memory ChatRepo (docs/chat-contract.md §1) plus helpers for the chat tests: a harness wired to it, a
respx-mocked Gemini, SSE parsing, and a raw ASGI driver that can disconnect mid-stream (httpx's ASGITransport
buffers the whole body and never disconnects, so Stop can't be tested through it)."""
from __future__ import annotations

import asyncio
import copy
import datetime as dt
import itertools
import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.db.chat import ConversationNotFound
from app.db.pool import DatabaseUnavailable
from app.services.chat import ChatService

GEMINI = "http://gemini.test/v1beta"
GEMINI_KEY = "AIzaTEST-secret-key-0123456789"
STREAM_RE = r"^http://gemini\.test/v1beta/models/(?P<model>[^/:]+):streamGenerateContent\?alt=sse$"
TITLE_RE = r"^http://gemini\.test/v1beta/models/(?P<model>[^/:]+):generateContent$"
T0 = dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.timezone.utc)


def _mid(v: Any) -> str:
    """'m007', 'mission:M007' -> 'M007' (as app.db.chat._text_ids)."""
    s = str(v or "").strip()
    return (s.split(":", 1)[1] if ":" in s else s).strip().upper()


def _uid(v: Any) -> Optional[str]:
    try:
        return str(uuid.UUID(str(v).strip()))
    except (ValueError, TypeError, AttributeError):
        return None


DOCS = [
    {"doc_id": "incident:AN012", "kind": "incident", "title": "Gauge out of range on HX-217 (M014)",
     "content": "During mission M014 the pressure gauge on HX-217 read 412.5 psi, above the safe band of 380 psi. "
                "Severity HIGH.", "asset_id": "HX-217", "location_id": "WP06", "mission_id": "M014",
     "action_id": "A0133", "ts": T0, "score": 0.0331},
    {"doc_id": "policy:battery_low", "kind": "policy", "title": "Low battery policy",
     "content": "Do not start a mission below 40% battery. Below 25% the action success rate drops sharply.",
     "asset_id": None, "location_id": None, "mission_id": None, "action_id": None, "ts": None, "score": 0.0213},
]
# chat.3: what search_documents(mission_id=...) returns (filtered by mission_id).
MISSION_DOCS = [
    {"doc_id": "event:act-A0061", "kind": "event", "title": "A0061 NAVIGATION failed at WP05 (M007)",
     "content": "Mission M007 action A0061 (NAVIGATION) at WP05 failed with LOCALIZATION_FAILURE at 47% battery.",
     "asset_id": None, "location_id": "WP05", "mission_id": "M007", "action_id": "A0061", "ts": T0, "score": 0.0402},
    {"doc_id": "incident:AN012", "kind": "incident", "title": "Gauge out of range on HX-217 (M014)",
     "content": "During mission M014 the pressure gauge on HX-217 read 412.5 psi, above the safe band of 380 psi. "
                "Severity HIGH.", "asset_id": "HX-217", "location_id": "WP06", "mission_id": "M014",
     "action_id": "A0133", "ts": T0, "score": 0.0331},
]
NODES = [
    {"node_id": "asset:HX-217", "kind": "asset", "label": "Heat Exchanger 17",
     "summary": "HX-217 is a CRITICAL heat exchanger at WP06 with 3 anomalies.", "doc_id": None, "score": 0.81,
     "match": "both"},
    {"node_id": "failure_type:LOCALIZATION_FAILURE", "kind": "failure_type", "label": "Localization failure",
     "summary": "The robot lost localization; 9 occurrences, most at WP05.", "doc_id": None, "score": 0.52,
     "match": "text"},
    {"node_id": "topic:weather", "kind": "topic", "label": "weather", "summary": "Weak vector-only match.",
     "doc_id": None, "score": 0.2, "match": "vector"},
]
NEIGHBOURS = [
    {"node_id": "location:WP06", "kind": "location", "label": "Turbine Hall South",
     "summary": "WP06 has a 62.5% action success rate.", "doc_id": None, "hop": 1, "via_rel": "located_at",
     "via_node": "asset:HX-217", "path": ["asset:HX-217", "-located_at->", "location:WP06"], "weight": 1.0,
     "evidence": None},
    {"node_id": "action:A0061", "kind": "action", "label": "Action A0061",
     "summary": "A0061 failed with LOCALIZATION_FAILURE at WP05.", "doc_id": None, "hop": 1,
     "via_rel": "failed_with", "via_node": "failure_type:LOCALIZATION_FAILURE",
     "path": ["failure_type:LOCALIZATION_FAILURE", "<-failed_with-", "action:A0061"], "weight": 0.11,
     "evidence": "event:act-A0061"},
]
SNAPSHOT = {
    "missions": 20, "reported_completed": 17, "completed_with_failures": 6, "reported_failed": 3, "actions": 297,
    "failed_actions": 41, "anomalies": 28, "serious_anomalies": 9, "open_flags": 12,
    "open_by_severity": {"CRITICAL": 2, "HIGH": 5, "MEDIUM": 3, "LOW": 2, "INFO": 0},
    "battery_bands": [{"band": "<=25", "n": 30, "success_rate_pct": 55.0},
                      {"band": "26-40", "n": 50, "success_rate_pct": 78.0}],
    "by_config": [{"config": "V1", "n": 150, "success_rate_pct": 80.7},
                  {"config": "V2", "n": 147, "success_rate_pct": 91.2}],
    "worst_locations": [{"location_id": "WP06", "location_name": "Turbine Hall South", "environment": "indoor",
                         "n": 16, "success_rate_pct": 62.5}],
    "repeat_offenders": [{"asset_id": "HX-217", "asset_name": "Heat Exchanger 17", "criticality": "CRITICAL",
                          "anomalies": 3, "serious": 2}],
    "failure_types": [{"failure_type": "LOCALIZATION_FAILURE", "n": 9}],
}
DIGESTS = {
    "missions": {"M007": {"mission_id": "M007", "mission_name": "Night rounds", "scenario": "routine",
                          "configuration_version": "V1", "start_time": T0, "end_time": T0, "duration_min": 42,
                          "start_battery_pct": 88, "end_battery_pct": 31, "mission_status": "COMPLETED",
                          "actions": 15, "failed": 4,
                          "failures": [{"action_id": "A0061", "sequence": 7, "action_type": "NAVIGATION",
                                        "asset_id": None, "location_id": "WP05", "battery_pct": 47,
                                        "failure_type": "LOCALIZATION_FAILURE"}],
                          "anomalies": [], "flags": [{"flag_id": 14, "severity": "HIGH", "status": "OPEN",
                                                      "title": "Hidden failures on M007"}]}},
    "assets": {"HX-217": {"asset_id": "HX-217", "asset_name": "Heat Exchanger 17", "asset_type": "heat_exchanger",
                          "criticality": "CRITICAL", "location_id": "WP06", "location_name": "Turbine Hall South",
                          "inspections": 9, "failed": 2, "anomalies": [{"anomaly_id": "AN012", "mission_id": "M014",
                                                                         "anomaly_type": "GAUGE_HIGH",
                                                                         "severity": "HIGH", "timestamp": T0}],
                          "open_flags": 1}},
    "locations": {"WP06": {"location_id": "WP06", "location_name": "Turbine Hall South", "zone_type": "turbine",
                           "environment": "indoor", "actions": 16, "failed": 6, "success_rate_pct": 62.5,
                           "failure_types": [{"failure_type": "LOCALIZATION_FAILURE", "n": 3}],
                           "assets": [{"asset_id": "HX-217", "asset_name": "Heat Exchanger 17"}]}},
    "flags": {14: {"flag_id": 14, "title": "Hidden failures on M007", "severity": "HIGH", "status": "OPEN",
                   "escalated": True, "mission_id": "M007", "action_id": None, "asset_id": None, "location_id": None,
                   "reasons": [{"rule": "mission_misleading"}], "created_at": T0, "assessment": None, "acks": []}},
}
FACTS = {"hidden_failures": {"count": 6, "of": 17},
         "top_asset": {"asset_id": "HX-217", "asset_name": "Heat Exchanger 17", "anomalies": 3},
         "worst_location": {"location_id": "WP06", "location_name": "Turbine Hall South", "success_rate_pct": 62.5},
         "latest_serious_flag": {"flag_id": 14, "title": "Hidden failures on M007", "mission_id": "M007"},
         "low_battery": {"band": "<=25", "success_rate_pct": 55.0}}


class FakeChatRepo:
    """Same interface and row shapes as app.db.chat.ChatRepo. `fail` holds method names that raise
    DatabaseUnavailable; `calls` records retrieval calls (name, args) for assertions."""

    def __init__(self):
        self.conversations: Dict[str, Dict[str, Any]] = {}
        self.messages: Dict[str, List[Dict[str, Any]]] = {}
        self.fail: set = set()
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.docs, self.nodes, self.neighbours = copy.deepcopy(DOCS), copy.deepcopy(NODES), copy.deepcopy(NEIGHBOURS)
        self.snapshot, self.facts = copy.deepcopy(SNAPSHOT), copy.deepcopy(FACTS)
        self.mission_docs = copy.deepcopy(MISSION_DOCS)
        self._clock = itertools.count(1)

    def _now(self) -> dt.datetime:
        return T0 + dt.timedelta(seconds=next(self._clock))

    def _check(self, name: str) -> None:
        if name in self.fail:
            raise DatabaseUnavailable("main database is not reachable")

    def _conv_out(self, c: Dict[str, Any]) -> Dict[str, Any]:
        d = DIGESTS["missions"].get(c.get("mission_id") or "")
        mission = ({"mission_id": d["mission_id"], "mission_name": d["mission_name"],
                    "mission_status": d["mission_status"]} if d else None)
        return {**c, "message_count": len(self.messages.get(c["conversation_id"], [])), "mission": mission}

    # conversations
    async def list_conversations(self, q=None, limit=50, offset=0, mission_id=None):
        self._check("list_conversations")
        rows = [c for c in self.conversations.values() if self.messages.get(c["conversation_id"])]   # chat.2
        if mission_id:                                                                                # chat.3
            rows = [c for c in rows if c.get("mission_id") == _mid(mission_id)]
        if q:
            ql = q.lower()
            rows = [c for c in rows if ql in c["title"].lower() or any(
                ql in m["content"].lower() for m in self.messages.get(c["conversation_id"], []))]
        rows.sort(key=lambda c: (not c["pinned"], -c["updated_at"].timestamp()))
        out = []
        for c in rows[offset: offset + limit]:
            msgs = [m for m in self.messages.get(c["conversation_id"], []) if m["content"].strip()]
            out.append({**self._conv_out(c),
                        "last_message_preview": " ".join(msgs[-1]["content"].split())[:120] if msgs else None})
        return out, len(rows)

    async def create_conversation(self, *, title, model, mode, mission_id=None):
        self._check("create_conversation")
        cid, now = str(uuid.uuid4()), self._now()
        mid = _mid(mission_id) if mission_id else None
        if mid is not None and mid not in DIGESTS["missions"]:
            raise ValueError("FK violation: unknown mission %r" % mid)     # the real table has an FK
        self.conversations[cid] = {"conversation_id": cid, "title": title, "title_source": "default", "model": model,
                                   "mode": mode, "pinned": False, "created_at": now, "updated_at": now,
                                   "mission_id": mid}
        self.messages[cid] = []
        return self._conv_out(self.conversations[cid])

    async def get_conversation(self, conversation_id):
        self._check("get_conversation")
        c = self.conversations.get(_uid(conversation_id) or "")
        return self._conv_out(c) if c else None

    async def update_conversation(self, conversation_id, *, title=None, title_source=None, pinned=None, model=None,
                                  mode=None, keep_user_title=False):
        self._check("update_conversation")
        c = self.conversations.get(_uid(conversation_id) or "")
        if c is None or (keep_user_title and c.get("title_source") == "user"):
            return None
        for k, v in (("title", title), ("title_source", title_source), ("pinned", pinned), ("model", model),
                     ("mode", mode)):
            if v is not None:
                c[k] = v
        c["updated_at"] = self._now()
        return self._conv_out(c)

    async def delete_conversation(self, conversation_id):
        self._check("delete_conversation")
        cid = _uid(conversation_id) or ""
        self.messages.pop(cid, None)
        return self.conversations.pop(cid, None) is not None

    # messages
    async def list_messages(self, conversation_id):
        self._check("list_messages")
        return [dict(m) for m in sorted(self.messages.get(_uid(conversation_id) or "", []), key=lambda m: m["seq"])]

    async def add_message(self, conversation_id, m):
        self._check("add_message")
        cid = _uid(conversation_id)
        if cid is None or cid not in self.conversations:
            raise ConversationNotFound(conversation_id)
        if not m.get("role"):
            raise ValueError("message role is required ('user' or 'assistant')")
        json.dumps(m)   # what the real repo stores as jsonb must be JSON-serializable
        msgs = self.messages[cid]
        row = {"message_id": str(uuid.uuid4()), "conversation_id": cid, "role": None,
               "seq": max((x["seq"] for x in msgs), default=0) + 1, "content": "", "thinking": None,
               "thinking_ms": None, "model": None, "mode": None, "status": "complete", "sources": [], "citations": [],
               "grounding": None, "usage": None, "latency_ms": None, "error": None, "error_code": None,
               "request_id": None, "created_at": self._now()}
        row.update({k: v for k, v in m.items() if k in row and k not in ("message_id", "seq", "created_at",
                                                                         "conversation_id") and v is not None})
        msgs.append(row)
        self.conversations[cid]["updated_at"] = self._now()
        return dict(row)

    async def delete_messages_from(self, conversation_id, seq):
        self._check("delete_messages_from")
        cid = _uid(conversation_id) or ""
        before = self.messages.get(cid, [])
        self.messages[cid] = [m for m in before if m["seq"] < seq]
        return len(before) - len(self.messages[cid])

    # retrieval
    async def search_documents(self, q, vec, k=8, kinds=None, mission_id=None):
        self.calls.append(("search_documents", {"q": q, "vec": vec, "k": k, "mission_id": mission_id}))
        self._check("search_documents")
        if mission_id:                                                                               # chat.3
            return (copy.deepcopy([d for d in self.mission_docs if d["mission_id"] == _mid(mission_id)][:k]),
                    ("hybrid" if vec else "fulltext"))
        return copy.deepcopy(self.docs[:k]), ("hybrid" if vec else "fulltext")

    async def match_graph_nodes(self, q, vec, k=6):
        self.calls.append(("match_graph_nodes", {"q": q, "vec": vec, "k": k}))
        self._check("match_graph_nodes")
        return copy.deepcopy(self.nodes[:k])

    async def graph_neighborhood(self, seed_ids, max_hops=2, max_nodes=25, kind_filter=None):
        self.calls.append(("graph_neighborhood", {"seed_ids": list(seed_ids), "kind_filter": kind_filter}))
        self._check("graph_neighborhood")
        rows = [r for r in self.neighbours if not kind_filter or r["kind"] in kind_filter]
        return copy.deepcopy(rows[:max_nodes])

    async def fleet_snapshot(self):
        self.calls.append(("fleet_snapshot", {}))
        self._check("fleet_snapshot")
        return copy.deepcopy(self.snapshot)

    async def entity_digests(self, *, mission_ids=(), asset_ids=(), location_ids=(), flag_ids=()):
        self.calls.append(("entity_digests", {"missions": list(mission_ids), "assets": list(asset_ids),
                                              "locations": list(location_ids), "flags": list(flag_ids)}))
        self._check("entity_digests")
        out = {}
        for kind, ids in (("missions", mission_ids), ("assets", asset_ids), ("locations", location_ids),
                          ("flags", flag_ids)):
            ids = [i if kind == "flags" else _mid(i) for i in ids]          # the real repo matches any case
            out[kind] = [copy.deepcopy(DIGESTS[kind][i]) for i in ids if i in DIGESTS[kind]][:5]
        return out

    async def list_missions(self, q=None, limit=100):
        self.calls.append(("list_missions", {"q": q, "limit": limit}))
        self._check("list_missions")
        out = []
        for mid, d in sorted(DIGESTS["missions"].items()):
            if q and q.lower() not in mid.lower() and q.lower() not in (d.get("mission_name") or "").lower():
                continue
            out.append({k: d[k] for k in ("mission_id", "mission_name", "scenario", "configuration_version",
                                          "start_time", "duration_min", "start_battery_pct", "end_battery_pct",
                                          "mission_status", "actions", "failed")}
                       | {"anomalies": len(d["anomalies"]),
                          "serious_anomalies": sum(a["severity"] in ("HIGH", "CRITICAL") for a in d["anomalies"]),
                          "open_flags": sum(f["status"] == "OPEN" for f in d["flags"]),
                          "hidden_failure": d["mission_status"] == "COMPLETED" and d["failed"] > 0,
                          "chats": sum(1 for c in self.conversations.values() if c.get("mission_id") == mid
                                       and self.messages.get(c["conversation_id"]))})
        return out[:limit]

    async def suggestion_facts(self):
        self._check("suggestion_facts")
        return copy.deepcopy(self.facts)


# ── harness ───────────────────────────────────────────────────────────────────────────────────────
def chat_harness(make_harness, **kw):
    """A conftest Harness whose ChatService uses a FakeChatRepo and the RecordingSink. Returns (harness, repo)."""
    settings = dict(gemini_api_key=GEMINI_KEY, gemini_base_url=GEMINI, chat_title_model=None)
    settings.update(kw)
    h = make_harness(**settings)
    repo = FakeChatRepo()
    h.svc.chat_repo = repo
    h.svc.chat = ChatService(h.settings, repo, h.svc.embed, h.svc.http, h.sink)
    return h, repo


def embed_ok(mock, dim: int = 4):
    from .conftest import EMBED
    return mock.post(EMBED + "/v1/embeddings").mock(return_value=httpx.Response(200, json={
        "model": "test-embed", "data": [{"index": 0, "embedding": [0.1] * dim}], "usage": {"prompt_tokens": 7}}))


def embed_down(mock):
    from .conftest import EMBED
    return mock.post(EMBED + "/v1/embeddings").mock(return_value=httpx.Response(503, text="loading"))


def chunk(*parts: Tuple[str, bool], finish: Optional[str] = None, usage: Optional[dict] = None) -> dict:
    """One streamGenerateContent event: parts are (text, is_thought)."""
    cand: Dict[str, Any] = {"content": {"role": "model", "parts": [
        {"text": t, **({"thought": True} if th else {})} for t, th in parts]}, "index": 0}
    if finish:
        cand["finishReason"] = finish
    out: Dict[str, Any] = {"candidates": [cand], "modelVersion": "test-model"}
    if usage:
        out["usageMetadata"] = usage
    return out


def sse_body(chunks: List[dict]) -> bytes:
    return "".join("data: %s\r\n\r\n" % json.dumps(c) for c in chunks).encode()


USAGE = {"promptTokenCount": 3100, "candidatesTokenCount": 40, "thoughtsTokenCount": 12, "totalTokenCount": 3152}
ANSWER_PARTS = ["HX-217's gauge read 412.5 psi ", "[incident:AN012], above the 380 psi band ",
                "[asset:HX-217, incident:AN012]."]


def answer_chunks(parts=ANSWER_PARTS, thoughts=(), finish="STOP") -> List[dict]:
    out = [chunk((t, True)) for t in thoughts]
    out += [chunk((p, False)) for p in parts[:-1]]
    out.append(chunk((parts[-1], False), finish=finish, usage=USAGE))
    return out


def gemini_stream(mock, chunks: Optional[List[dict]] = None, status: int = 200, error: Optional[dict] = None,
                  captured: Optional[list] = None, stream=None):
    """Mock streamGenerateContent. `stream` = an async-iterator factory for a slow / never-ending body."""
    def handler(request: httpx.Request, model: str):
        if captured is not None:
            captured.append({"model": model, "url": str(request.url), "headers": dict(request.headers),
                             "body": json.loads(request.content)})
        if status != 200:
            return httpx.Response(status, json=error or {"error": {"code": status, "message": "boom"}})
        if stream is not None:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream())
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=sse_body(chunks if chunks is not None else answer_chunks()))
    return mock.post(url__regex=STREAM_RE).mock(side_effect=handler)


def gemini_title(mock, text: str = "HX-217 gauge reading", captured: Optional[list] = None, side=None):
    def handler(request: httpx.Request, model: str):
        if captured is not None:
            captured.append({"model": model, "url": str(request.url), "headers": dict(request.headers),
                             "body": json.loads(request.content)})
        if side:
            side()
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]},
                                                         "finishReason": "STOP"}],
                                         "usageMetadata": {"promptTokenCount": 90, "candidatesTokenCount": 5}})
    return mock.post(url__regex=TITLE_RE).mock(side_effect=handler)


def parse_sse(text: str) -> List[Tuple[str, Any]]:
    """[(event, data)] in order; keep-alive comments come back as (':', comment)."""
    out = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        if not block.strip():
            continue
        if block.startswith(":"):
            out.append((":", block[1:].strip()))
            continue
        name, data = None, []
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        out.append((name, json.loads("\n".join(data)) if data else None))
    return out


def events(text: str) -> List[Tuple[str, Any]]:
    return [(n, d) for n, d in parse_sse(text) if n != ":"]


async def drive(app, path: str, body: dict, disconnect_after: Optional[str] = None,
                disconnect: Optional[asyncio.Event] = None, timeout: float = 10.0) -> List[dict]:
    """Call the ASGI app directly (uvicorn's spec_version 2.3, so Starlette listens for http.disconnect) and
    disconnect once a body chunk containing `event: <disconnect_after>` was sent. Returns the sent messages."""
    raw = json.dumps(body).encode()
    sent: List[dict] = []
    gone = disconnect or asyncio.Event()
    state = {"body": False}

    async def receive():
        if not state["body"]:
            state["body"] = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(msg):
        sent.append(msg)
        if (disconnect_after and msg["type"] == "http.response.body"
                and ("event: %s" % disconnect_after).encode() in msg.get("body", b"")):
            gone.set()

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
             "root_path": "", "client": ("127.0.0.1", 50000), "server": ("backend", 80),
             "headers": [(b"host", b"backend"), (b"content-type", b"application/json"),
                         (b"content-length", str(len(raw)).encode())]}
    await asyncio.wait_for(app(scope, receive, send), timeout)
    return sent


def body_text(sent: List[dict]) -> str:
    return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body").decode()
