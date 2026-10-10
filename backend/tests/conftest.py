"""Shared fixtures: an app wired to the in-memory FakeRepo, a RecordingSink, and respx-mocked services."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
import pytest
import respx

from app.config import Settings
from app.deps import build_services
from app.main import create_app
from app.services import validator as V

from .fakes import FakeRepo, RecordingSink

REPO_ROOT = Path(__file__).resolve().parents[2]
KIT = REPO_ROOT / "amd" / "system1" / "data" / "kit"
S1 = "http://system1.test"
EMBED = "http://embed.test"
MCP = "http://mcp.test/mcp"
S2 = "http://system2.test"
ADMIN_TOKEN = "test-admin-token"
ADMIN = {"X-Admin-Token": ADMIN_TOKEN}


def replay_events():
    with open(KIT / "replay.ndjson") as f:
        return [json.loads(line) for line in f if line.strip()]


def replay_event(event_id: str) -> Dict[str, Any]:
    return next(e for e in replay_events() if e["event_id"] == event_id)


def make_settings(**kw) -> Settings:
    base = dict(database_url=None, logs_database_url=None, system1_url=S1, embed_url=EMBED, mcp_url=MCP,
                system2_url=None, kit_dir=KIT, triage_mode="serial", breaker_cooldown_s=0.0, embed_dim=4,
                log_requests=True, admin_token=ADMIN_TOKEN)
    base.update(kw)
    return Settings(**base)


class Harness:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.svc = build_services(settings)
        self.repo = FakeRepo(self.svc.ref)
        self.sink = RecordingSink()
        s = self.svc
        s.repo = self.repo
        s.sink = self.sink
        for obj in (s.system1, s.embed, s.system2, s.orchestrator, s.pipeline, s.replay, s.flags):
            obj.sink = self.sink
        s.pipeline.repo = self.repo
        s.flags.repo = self.repo
        if hasattr(s.replay, "repo"):          # REPLAY_MODE=stepped keeps its run in the (fake) DB
            s.replay.repo = self.repo
        self.app = create_app(settings, services=s)
        # Sends the admin token like the dashboard's operator console would (audit H3); tests/test_auth.py
        # covers the missing/wrong/unset cases with its own clients.
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://backend",
                                        headers=ADMIN if settings.admin_token else None)


@pytest.fixture
def make_harness():
    made = []

    def _make(**kw) -> Harness:
        h = Harness(make_settings(**kw))
        made.append(h)
        return h
    yield _make


@pytest.fixture
async def h(make_harness):
    harness = make_harness()
    yield harness
    await harness.client.aclose()


@pytest.fixture
def mock():
    with respx.mock(assert_all_called=False) as router:
        yield router


def laya_answers(kind: str, escalate: Optional[bool] = None, p: float = 0.83, priority: Optional[str] = None,
                 category: Optional[str] = None, event: Optional[dict] = None) -> Dict[str, Any]:
    """Fake Laya answers. By default they agree with the validator (computed from `event`)."""
    if kind == "pre_action":
        return {"will_fail": {"answer": "A", "probs": {"A": 0.64, "B": 0.36}, "confidence": 0.64},
                "failure_mode": {"answer": "navigation", "probs": {"navigation": 0.5}, "confidence": 0.5},
                "anomaly_expected": {"answer": "B", "probs": {"A": 0.2, "B": 0.8}, "confidence": 0.8}}
    rv = V.rules_verdict(kind, event) if event else None
    esc = rv["escalate"] if escalate is None and rv else bool(escalate)
    pr = priority or (rv["priority"] if rv else ("P2" if esc else "P4"))
    pa = p if esc else 1 - p
    out = {"escalate": {"answer": "A" if esc else "B", "probs": {"A": pa, "B": 1 - pa}, "confidence": max(pa, 1 - pa)},
           "priority": {"answer": pr, "probs": {pr: 0.7}, "confidence": 0.7}}
    if kind == "action":
        out["category"] = {"answer": category or (rv["category"] if rv else "none"), "probs": {}, "confidence": 0.6}
        out["reading_trust"] = {"answer": "C", "probs": {}, "confidence": 0.9}
    else:
        out["mission_misleading"] = {"answer": "A", "probs": {"A": 0.9}, "confidence": 0.9}
    return out


def system1_route(mock, answers_fn, captured: Optional[list] = None):
    """Mock POST /v1/predict: answers_fn(item) -> answers dict."""
    def handler(request: httpx.Request):
        body = json.loads(request.content)
        if captured is not None:
            captured.append({"body": body, "headers": dict(request.headers)})
        return httpx.Response(200, json={"request_id": body.get("request_id"), "model": "laya-test", "fine_tuned": True,
                                         "device": "cpu", "latency_ms": 5,
                                         "items": [{"id": it["id"], "kind": it["kind"], "answers": answers_fn(it)}
                                                   for it in body["items"]]})
    return mock.post(S1 + "/v1/predict").mock(side_effect=handler)
