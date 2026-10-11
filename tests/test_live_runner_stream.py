"""Live runner streaming (contract: docs/backend-services-contract.md "Streaming Qwen drafts", live runner part).

scripts/live_runner.py calls System 2 /v1/assess/stream and posts the newest draft to the backend's /draft endpoint
from a side thread (throttled, latest wins, never after /analysis), falling back to /v1/assess on 404/405 or with
SYSTEM2_STREAM=0. Fakes: a System 2 that emits SSE over chunked HTTP/1.1 with small delays (plus 404, error-event,
stalled and never-final variants) and a backend that records every POST with its arrival and completion time.
Standard library + pytest only, ~10 s: `backend/.venv/bin/python -m pytest tests/test_live_runner_stream.py -q`.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("live_runner", ROOT / "scripts" / "live_runner.py")
lr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lr)

FINAL = {"evidence": [{"claim": "Pressure 182 psi is above the band", "ids": ["R1"]}], "answer": "Over pressure.",
         "question": "q", "recommended_action": "MANUAL_READING", "confidence": "medium",
         "insufficient_context": False, "flag_id": 7, "model": "qwen3.8-27b-q8_0", "latency_ms": 900,
         "tokens_per_sec": 22.5, "path": "llm"}
PLAIN = dict(FINAL, answer="Plain answer.")


def draft_fields(i: int) -> Dict[str, Any]:
    return {"evidence": [{"claim": "Pressure 182 psi is above" + " the band" * (i > 3), "ids": ["R1"]}],
            "answer": "Over" + " pressure" * i}


# ── fake System 2 ───────────────────────────────────────────────────────────────────────────────
class FakeSystem2(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a: Any) -> None:
        pass

    def _json(self, status: int, body: Any, headers: Dict[str, str] = {}) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _chunk(self, data: bytes) -> None:
        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
        self.wfile.flush()

    def _event(self, name: str, data: Any) -> None:
        self._chunk(("event: %s\ndata: %s\n\n" % (name, json.dumps(data))).encode())

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        sc = self.server.scenario
        self.server.calls.append({"path": self.path, "key": self.headers.get("X-API-Key"), "body": body,
                                  "t": time.monotonic()})
        if self.path == "/v1/assess":
            return self._json(200, PLAIN, {"X-System2-Cache": "miss", "X-System2-Image": "text_only"})
        if self.path != "/v1/assess/stream" or sc["mode"] in (404, 405, 503):
            return self._json(sc["mode"] if sc["mode"] in (404, 405, 503) else 404, {"detail": "Not Found"})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            self._event("start", {"flag_id": 7, "request_id": body.get("request_id"), "model": "q", "mode": "live"})
            if sc["mode"] == "stall":                    # headers, then nothing
                time.sleep(sc.get("hold", 5))
                return
            if sc["mode"] == "trickle":                  # one byte at a time, never a full line
                for _ in range(100):
                    self._chunk(b"x")
                    time.sleep(0.1)
                return
            self._chunk(b": keep-alive\n\n")
            n = sc.get("drafts", 10)
            for i in range(1, n + 1 if sc["mode"] != "endless" else 10_000):
                self._event("draft", {"call": 1, "tokens": i * 3, "fields": draft_fields(i)})
                time.sleep(sc.get("gap", 0.05))
            if sc["mode"] == "error":
                self._event("error", {"detail": "boom: llama-server went away"})
            else:
                self.server.final_at = time.monotonic()
                self._event("final", {"response": FINAL, "cache": "miss", "image": "used"})
            self._chunk(b"")
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


# ── fake backend (+ vision) ─────────────────────────────────────────────────────────────────────
class FakeBackend(BaseHTTPRequestHandler):
    def log_message(self, *a: Any) -> None:
        pass

    def do_POST(self) -> None:
        t_in = time.monotonic()
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        sc = self.server.scenario
        rec = {"path": self.path, "body": body, "t_in": t_in, "token": self.headers.get("X-Admin-Token")}
        status, out = 200, {}
        if self.path.endswith("/draft"):
            time.sleep(sc.get("draft_delay", 0.0))
            status = sc.get("draft_status", 200)
            out = {"updated": True} if status == 200 else {"error": "nope"}
        elif self.path.endswith("/analysis"):
            out = {"qwen_source": "system2" if body.get("system2") else "template", "embed": [],
                   "fallback_reason": None if body.get("system2") else "system2 unavailable"}
        elif self.path == "/v1/vision":
            out = {"kind": "gauge", "readings": [], "ocr": []}
        rec["t_out"] = time.monotonic()            # recorded before the client can see the answer
        with self.server.lock:
            self.server.records.append(rec)
        raw = json.dumps(out).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


    def do_GET(self) -> None:                       # --check reads the R2 preview (no writes)
        raw = json.dumps({"stops": [{"stop": "E", "action_event": {"event_id": "e1", "kind": "action"}}]}).encode()
        self.send_response(200 if self.path.startswith("/api/v1/live/preview") else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _serve(handler: type, scenario: Dict[str, Any]) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    srv.scenario, srv.calls, srv.records, srv.lock, srv.final_at = scenario, [], [], threading.Lock(), None
    srv.handle_error = lambda *a: None                   # the runner hangs up on purpose (final, timeout)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return srv


@pytest.fixture
def env(monkeypatch, tmp_path):
    s2 = _serve(FakeSystem2, {"mode": "stream"})
    be = _serve(FakeBackend, {})
    photo = tmp_path / "kit" / "inspection" / "gauge.jpg"
    photo.parent.mkdir(parents=True)
    photo.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    monkeypatch.setattr(lr, "PHOTO_ROOT", tmp_path)
    monkeypatch.setenv("SYSTEM2_URL", "http://127.0.0.1:%d" % s2.server_port)
    monkeypatch.setenv("VISION_URL", "http://127.0.0.1:%d" % be.server_port)
    monkeypatch.setenv("EMBED_URL", "http://127.0.0.1:%d" % be.server_port)
    monkeypatch.setenv("SYSTEM1_URL", "http://127.0.0.1:%d" % be.server_port)   # never the real box tunnels
    monkeypatch.setenv("SYSTEM2_API_KEY", "s2-test-key")
    for k in ("SYSTEM2_STREAM", "LIVE_DRAFT_INTERVAL_S", "SYSTEM2_TIMEOUT_S"):
        monkeypatch.delenv(k, raising=False)

    def make_cfg() -> Any:
        a = argparse.Namespace(backend_url="http://127.0.0.1:%d" % be.server_port, admin_token="admin-test",
                               travel_s=0, work_s=0, name="test")
        return lr.Config(a)

    yield s2, be, make_cfg
    s2.shutdown()
    be.shutdown()


STOP = {"stop": "E", "photo": "/kit/inspection/gauge.jpg", "photo_kind": "gauge", "action_type": "GAUGE_READING"}
RES = {"embed": [], "system2_request": {"flag_id": 7, "flag": {}, "context": {"citation_ids": ["R1"]}}}


def run_stop(make_cfg) -> str:
    cfg = make_cfg()
    return lr.analyse(cfg, lr.Backend(cfg), 1, 0, STOP, RES)


def posts(be, suffix: str) -> List[Dict[str, Any]]:
    return [r for r in be.records if r["path"].endswith(suffix)]


# ── tests ───────────────────────────────────────────────────────────────────────────────────────
def test_drafts_posted_progressively_and_throttled(env, monkeypatch):
    s2, be, make_cfg = env
    s2.scenario.update(drafts=20, gap=0.05)                 # ~1 s of drafts
    monkeypatch.setenv("LIVE_DRAFT_INTERVAL_S", "0.25")
    assert run_stop(make_cfg) == "system2"

    assert [c["path"] for c in s2.calls] == ["/v1/assess/stream"]
    assert s2.calls[0]["key"] == "s2-test-key" and s2.calls[0]["body"]["image_b64"].startswith("data:image/jpeg")
    drafts = posts(be, "/stops/0/draft")
    assert 3 <= len(drafts) <= 6, len(drafts)                # throttled: 20 drafts in ~1 s at most every 0.25 s
    assert drafts[0]["t_in"] < s2.final_at                   # progressive: while Qwen is still writing
    gaps = [b["t_in"] - a["t_in"] for a, b in zip(drafts, drafts[1:])]
    assert min(gaps) >= 0.2, gaps
    tokens = [d["body"]["tokens"] for d in drafts]
    assert tokens == sorted(tokens) and len(set(tokens)) == len(tokens)
    assert all(d["body"]["call"] == 1 and set(d["body"]) == {"call", "fields", "tokens"} for d in drafts)
    assert drafts[0]["body"]["fields"] == draft_fields(1)    # the first snapshot goes out at once
    assert all(d["token"] == "admin-test" for d in drafts)

    (analysis,) = posts(be, "/stops/0/analysis")
    assert analysis["t_in"] >= max(d["t_out"] for d in drafts)   # no draft after /analysis
    assert analysis["body"]["system2"] == FINAL                   # unchanged: final.response
    assert analysis["body"]["image_status"] == "used"             # final.image
    assert analysis["body"]["system2_error"] is None
    assert "image_b64" not in analysis["body"]["system2_request"]


def test_newest_snapshot_wins_while_a_post_is_in_flight(env):
    _, be, make_cfg = env
    be.scenario["draft_delay"] = 0.3
    cfg = make_cfg()
    poster = lr.DraftPoster(lr.Backend(cfg), 1, 0, 0.0, "[t]")
    poster.offer(1, 1, {"answer": "A"})
    time.sleep(0.1)                                           # A is in flight
    for i, text in enumerate(("B", "C", "D"), start=2):
        poster.offer(1, i, {"answer": text})
    time.sleep(0.8)
    poster.close()
    assert [d["body"]["fields"]["answer"] for d in posts(be, "/draft")] == ["A", "D"]
    assert poster.superseded == 2 and poster.posted == 2


def test_close_drops_unsent_snapshot_and_waits_for_the_post_in_flight(env):
    _, be, make_cfg = env
    be.scenario["draft_delay"] = 0.4
    cfg = make_cfg()
    poster = lr.DraftPoster(lr.Backend(cfg), 1, 0, 10.0, "[t]")
    poster.offer(1, 1, {"answer": "A"})
    time.sleep(0.1)
    poster.offer(1, 2, {"answer": "B"})                       # throttled: still pending at close
    t0 = time.monotonic()
    poster.close()
    assert time.monotonic() - t0 >= 0.2                       # waited for A's post to finish
    assert len(posts(be, "/draft")) == 1 and posts(be, "/draft")[0]["t_out"] <= time.monotonic()
    poster.offer(1, 3, {"answer": "C"})                       # offers after close are ignored
    time.sleep(0.1)
    assert [d["body"]["fields"]["answer"] for d in posts(be, "/draft")] == ["A"]


def test_slow_draft_posts_never_land_after_analysis(env, monkeypatch):
    s2, be, make_cfg = env
    s2.scenario.update(drafts=6, gap=0.01)
    be.scenario["draft_delay"] = 0.5                          # the backend is slower than the stream
    monkeypatch.setenv("LIVE_DRAFT_INTERVAL_S", "0")
    assert run_stop(make_cfg) == "system2"
    drafts = posts(be, "/draft")
    (analysis,) = posts(be, "/analysis")
    assert 1 <= len(drafts) <= 2
    assert analysis["t_in"] >= max(d["t_out"] for d in drafts)


@pytest.mark.parametrize("status", [404, 405, 503])
def test_stream_404_405_falls_back_to_plain_assess(env, status):
    s2, be, make_cfg = env
    s2.scenario["mode"] = status
    assert run_stop(make_cfg) == "system2"
    assert [c["path"] for c in s2.calls] == ["/v1/assess/stream", "/v1/assess"]
    assert s2.calls[0]["body"] == s2.calls[1]["body"]
    assert posts(be, "/draft") == []
    (analysis,) = posts(be, "/analysis")
    assert analysis["body"]["system2"] == PLAIN and analysis["body"]["image_status"] == "text_only"


def test_system2_stream_0_uses_plain_assess(env, monkeypatch):
    s2, be, make_cfg = env
    monkeypatch.setenv("SYSTEM2_STREAM", "0")
    assert run_stop(make_cfg) == "system2"
    assert [c["path"] for c in s2.calls] == ["/v1/assess"]
    assert posts(be, "/draft") == []
    assert posts(be, "/analysis")[0]["body"]["system2"] == PLAIN


@pytest.mark.parametrize("draft_status, max_attempts", [(500, 99), (404, 1)])
def test_draft_post_failure_never_fails_the_stop(env, monkeypatch, draft_status, max_attempts):
    s2, be, make_cfg = env
    s2.scenario.update(drafts=10, gap=0.05)
    be.scenario["draft_status"] = draft_status
    monkeypatch.setenv("LIVE_DRAFT_INTERVAL_S", "0.1")
    assert run_stop(make_cfg) == "system2"
    attempts = posts(be, "/draft")
    assert 1 <= len(attempts) <= max_attempts                 # a 4xx ends this stop's drafts; a 5xx does not
    if draft_status == 500:
        assert len(attempts) >= 2
    (analysis,) = posts(be, "/analysis")
    assert analysis["body"]["system2"] == FINAL


def test_error_event_reports_system2_error_without_fallback(env):
    s2, be, make_cfg = env
    s2.scenario.update(mode="error", drafts=2)
    assert run_stop(make_cfg) == "template"
    assert [c["path"] for c in s2.calls] == ["/v1/assess/stream"]   # an error event is not a 404: no second call
    (analysis,) = posts(be, "/analysis")
    assert analysis["body"]["system2"] is None
    assert "boom: llama-server went away" in analysis["body"]["system2_error"]
    assert analysis["body"]["image_status"] is None


@pytest.mark.parametrize("mode", ["stall", "trickle", "endless"])
def test_total_timeout_bounds_the_whole_stream(env, monkeypatch, mode):
    s2, _, make_cfg = env
    s2.scenario.update(mode=mode, gap=0.2)
    monkeypatch.setenv("SYSTEM2_TIMEOUT_S", "1.2")
    cfg = make_cfg()
    t0 = time.monotonic()
    r = lr.run_system2(cfg, dict(RES["system2_request"], request_id="t"), on_draft=lambda *_: None)
    took = time.monotonic() - t0
    assert 1.1 <= took < 2.0, took
    assert r.out is None and "timed out" in r.error and r.via == "stream"
    if mode == "endless":
        assert r.drafts >= 3


def test_json_answer_on_the_stream_endpoint_is_taken_as_final(env, monkeypatch):
    """A proxy or build that answers /v1/assess/stream with plain JSON still yields the answer."""
    s2, _, make_cfg = env
    monkeypatch.setattr(FakeSystem2, "do_POST", lambda self: (
        self.rfile.read(int(self.headers["Content-Length"])),
        self._json(200, FINAL, {"X-System2-Cache": "hit", "X-System2-Image": "used"})))
    r = lr.run_system2(make_cfg(), dict(RES["system2_request"], request_id="t"))
    assert r.out == FINAL and r.cache == "hit" and r.image == "used" and r.drafts == 0


@pytest.mark.parametrize("mode, line", [("stream", "stream    OK   10 drafts, first after"),
                                        (404, "stream    OFF  System 2 answered /v1/assess/stream with 404")])
def test_check_reports_the_stream_and_posts_nothing(env, capsys, mode, line):
    s2, be, make_cfg = env
    s2.scenario["mode"] = mode
    photo = lr.PHOTO_ROOT / "kit" / "inspection" / "gauge_pressure_01.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    lr.check(make_cfg())                                     # Laya/embed answer {}: their lines FAIL
    out = capsys.readouterr().out
    assert line in out and "system2   OK" in out
    assert [r for r in be.records if r["path"].startswith("/api/")] == []   # no backend writes
