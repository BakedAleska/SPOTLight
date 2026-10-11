#!/usr/bin/env python3
"""SPOTlight live runner: walks queued live runs and bridges the AMD box to the Vercel backend.

Runs on the presenter laptop (the only machine that can reach the AMD box over SSH). It polls the backend for a run
an operator queued from the web console, claims it, and walks the route's 8 stops:

  per stop   start  -> backend logs ACTION_STARTED; SPOT appears at the stop on the map
             wait   -> the inspection itself (LIVE_WORK_S)
             Laya   -> System 1 /v1/predict on the canonical action event the backend returned (live, on the GPU)
             result -> backend runs the rules beside Laya (parallel mode), writes the flag, red/green ping
             in the background, while SPOT walks on: embed the new texts, vision on the stop's photo (a simulated
             capture: a kit sample picked by action type), System 2 (Qwen) with the photo, then post the analysis
             and embed the explanation. Qwen's answer is streamed (/v1/assess/stream): a side thread posts the
             newest unverified draft to the capture (/draft) so /live shows the text while it is written; the
             checked answer then replaces it (/analysis, unchanged)
  between    walk   -> LIVE_TRAVEL_S

It never holds database credentials: everything goes through the backend's admin API (X-Admin-Token).
Standard library only. Settings (env or flags): BACKEND_URL, ADMIN_TOKEN; the box services through SSH tunnels
(SYSTEM1_URL, VISION_URL, SYSTEM2_URL, EMBED_URL) and their keys (VISION_API_KEY, SYSTEM2_API_KEY). With
--tunnel HOST the runner opens the four tunnels itself (`ssh -N -L ...`). Streaming: SYSTEM2_STREAM (default 1;
0 = plain /v1/assess, as does a System 2 that answers the stream with 404/405), LIVE_DRAFT_INTERVAL_S (default
0.5: at most one draft post per interval). SYSTEM2_TIMEOUT_S (default 60) bounds the whole System 2 call, stream
included. Runbook: docs/LIVE-RUNBOOK.md.

  python scripts/live_runner.py --tunnel spot-amd            # serve runs until Ctrl-C
  python scripts/live_runner.py --dry-run                    # Laya on all 40 stops vs the plan, no writes
  python scripts/live_runner.py --check                      # one real request to each service
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.client import HTTPConnection, HTTPException, HTTPSConnection   # not `import http.client`: http() below
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
PHOTO_ROOT = REPO / "web" / "public"
# local tunnel port -> box port (llama-server :8080 stays on the box; System 2 talks to it there)
TUNNELS = {"SYSTEM1_URL": (18082, 8082), "EMBED_URL": (18081, 8081), "SYSTEM2_URL": (18083, 8083),
           "VISION_URL": (18084, 8084)}
V_ID = re.compile(r"^V[0-9]{1,3}$")
FORCE_LOW_FLAGS = {"AMBIGUOUS_NEEDLE", "DEFAULT_SWEEP"}
LOG_LOCK = threading.Lock()
DRAFT_POST_TIMEOUT_S = 5.0        # a draft post is best effort: short timeout, no retries
DRAFT_FIELDS_MAX = 16 * 1024      # the backend rejects larger draft fields
STREAM_FALLBACK_STATUS = (404, 405, 503)   # no /v1/assess/stream (older build) or all its stream slots busy


def log(msg: str, *a: Any) -> None:
    with LOG_LOCK:
        print("%s  %s" % (dt.datetime.now().strftime("%H:%M:%S"), msg % a if a else msg), flush=True)


HEARTBEAT = os.environ.get("LIVE_HEARTBEAT_FILE")   # container healthcheck: touched on every poll and stop


def beat() -> None:
    if HEARTBEAT:
        try:
            Path(HEARTBEAT).touch()
        except OSError:
            pass


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


class HttpError(Exception):
    def __init__(self, url: str, status: Optional[int], detail: str):
        super().__init__("%s -> %s %s" % (url, status, detail))
        self.status, self.detail = status, detail


def http(method: str, url: str, body: Any = None, headers: Optional[Dict[str, str]] = None,
         timeout: float = 30.0) -> Tuple[Dict[str, Any], Dict[str, str]]:
    data = None if body is None else json.dumps(body, default=str).encode("utf-8")
    h = {"Accept": "application/json", "User-Agent": "spotlight-live-runner/1"}
    if data is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return (json.loads(raw) if raw else {}), {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise HttpError(url, exc.code, detail) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise HttpError(url, None, "%s: %s" % (type(exc).__name__, exc)) from None


# ── configuration ────────────────────────────────────────────────────────────────────────────────
class Config:
    def __init__(self, a: argparse.Namespace):
        env = os.environ.get
        self.backend = (a.backend_url or env("BACKEND_URL") or "").rstrip("/")
        if self.backend and not self.backend.endswith("/api/v1"):
            self.backend += "/api/v1"
        self.token = a.admin_token or env("ADMIN_TOKEN") or env("BACKEND_ADMIN_TOKEN") or ""
        self.system1 = (env("SYSTEM1_URL") or "http://127.0.0.1:%d" % TUNNELS["SYSTEM1_URL"][0]).rstrip("/")
        self.embed = (env("EMBED_URL") or "http://127.0.0.1:%d" % TUNNELS["EMBED_URL"][0]).rstrip("/")
        self.system2 = (env("SYSTEM2_URL") or "http://127.0.0.1:%d" % TUNNELS["SYSTEM2_URL"][0]).rstrip("/")
        self.vision = (env("VISION_URL") or "http://127.0.0.1:%d" % TUNNELS["VISION_URL"][0]).rstrip("/")
        self.vision_key = env("VISION_API_KEY") or ""
        self.system2_key = env("SYSTEM2_API_KEY") or env("AMD_API_KEY") or ""
        self.embed_model = env("EMBED_MODEL") or "Qwen/Qwen3-Embedding-0.6B"
        self.travel_s = float(env("LIVE_TRAVEL_S") or a.travel_s)
        self.work_s = float(env("LIVE_WORK_S") or a.work_s)
        self.trust_min = float(env("VISION_TRUST_MIN") or 0.5)
        self.system2_timeout = float(env("SYSTEM2_TIMEOUT_S") or 60)
        self.system2_stream = (env("SYSTEM2_STREAM") or "1").strip().lower() not in ("0", "false", "no", "off")
        self.draft_interval = max(0.0, float(env("LIVE_DRAFT_INTERVAL_S") or 0.5))
        self.name = a.name or env("RUNNER_NAME") or "presenter-laptop"

    def need_backend(self) -> None:
        if not self.backend or not self.token:
            sys.exit("BACKEND_URL and ADMIN_TOKEN are required (env or --backend-url/--admin-token)")


class Backend:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def get(self, path: str, timeout: float = 20) -> Dict[str, Any]:
        return http("GET", self.cfg.backend + path, headers={"X-Admin-Token": self.cfg.token}, timeout=timeout)[0]

    def post(self, path: str, body: Any = None, timeout: float = 60, retries: int = 2) -> Dict[str, Any]:
        for attempt in range(retries + 1):
            try:
                return http("POST", self.cfg.backend + path, body if body is not None else {},
                            headers={"X-Admin-Token": self.cfg.token}, timeout=timeout)[0]
            except HttpError as exc:
                transient = exc.status is None or exc.status >= 500
                if not transient or attempt == retries:
                    raise
                log("  backend %s failed (%s), retrying", path, exc.status or exc.detail[:80])
                time.sleep(1.5 * (attempt + 1))
        raise AssertionError("unreachable")


# ── box services ─────────────────────────────────────────────────────────────────────────────────
def call_laya(cfg: Config, event: Dict[str, Any], request_id: str) -> Dict[str, Any]:
    t0 = time.perf_counter()
    try:
        body, _ = http("POST", cfg.system1 + "/v1/predict",
                       {"request_id": request_id, "items": [{"id": event["event_id"], "kind": event.get("kind", "action"),
                                                              "event": event}]},
                       headers={"X-Request-ID": request_id}, timeout=20)
        items = body.get("items") or []
        item = next((i for i in items if i.get("id") == event["event_id"]), items[0] if items else None)
        if not item or not item.get("answers"):
            raise HttpError(cfg.system1, 200, "no answers in the response")
        return {"status": "ok", "answers": item["answers"], "model": body.get("model"),
                "fine_tuned": body.get("fine_tuned"), "device": body.get("device"),
                "latency_ms": body.get("latency_ms") or int((time.perf_counter() - t0) * 1000)}
    except HttpError as exc:
        return {"status": "unavailable", "error": "Laya: %s %s" % (exc.status or "", exc.detail[:200])}


def embed_texts(cfg: Config, texts: List[str]) -> Optional[List[List[float]]]:
    if not texts:
        return []
    try:
        body, _ = http("POST", cfg.embed + "/v1/embeddings", {"model": cfg.embed_model, "input": texts}, timeout=30)
        data = sorted(body.get("data") or [], key=lambda d: d.get("index", 0))
        vecs = [d["embedding"] for d in data]
        return vecs if len(vecs) == len(texts) else None
    except (HttpError, KeyError, TypeError) as exc:
        log("  embed failed: %s", exc)
        return None


def post_embeddings(cfg: Config, be: Backend, run_id: int, items: List[Dict[str, str]]) -> str:
    if not items:
        return "nothing to embed"
    vecs = embed_texts(cfg, [i["text"] for i in items])
    if vecs is None:
        return "embed service unavailable"
    out = be.post("/live/runs/%d/embeddings" % run_id, {"model": cfg.embed_model, "items": [
        {"key": i["key"], "text": i["text"], "vector": v} for i, v in zip(items, vecs)]})
    return "%d embedded, %d skipped" % (len(out.get("embedded") or []), len(out.get("skipped") or []))


def vision_rows(resp: Dict[str, Any], trust_min: float) -> List[Dict[str, Any]]:
    """Vision readings -> V# rows for System 2, as the orchestrator builds them for a demo image (pairing demo:
    the photo is a kit sample, never this asset, so no comparison with the log is attached)."""
    rows = []
    kind = resp.get("kind") or (resp.get("router") or {}).get("modality")
    # vision.2 returns `readings`; the older vision-1.0 build returns ready V# rows under `vision`
    for i, r in enumerate([r for r in (resp.get("readings") or resp.get("vision") or []) if isinstance(r, dict)][:6]):
        rid = r.get("id") if isinstance(r.get("id"), str) and V_ID.match(r["id"]) else "V%d" % (i + 1)
        conf = r.get("confidence")
        conf = min(1.0, max(0.0, float(conf))) if isinstance(conf, (int, float)) else None
        flags = [f for f in (r.get("flags") or []) if isinstance(f, str)][:8]
        high = conf is not None and conf >= trust_min and not FORCE_LOW_FLAGS & set(flags)
        row = {"id": rid, "tool": str(r.get("method") or r.get("tool") or "vision")[:64], "kind": kind,
               "quantity": r.get("quantity"), "value": r.get("value"), "unit": r.get("unit"), "confidence": conf,
               "trust": "HIGH" if high else "LOW", "flags": flags, "pairing": "demo"}
        if "CLIPPED" in flags:
            row["bound"] = "lower"
        rows.append({k: v for k, v in row.items() if v not in (None, "", [])})
    return rows


def run_vision(cfg: Config, stop: Dict[str, Any], b64: str, request_id: str) -> Tuple[Optional[Dict], Optional[str]]:
    t0 = time.perf_counter()
    body = {"request_id": request_id, "media_id": Path(stop["photo"]).name, "kind": stop.get("photo_kind") or "auto",
            "action_type": stop.get("action_type"), "image_b64": b64.partition(",")[2] if b64.startswith("data:")
            else b64, "annotate": True}   # plain base64: older vision builds reject data: URLs
    try:
        resp, _ = http("POST", cfg.vision + "/v1/vision", body, headers={"X-API-Key": cfg.vision_key}, timeout=30)
    except HttpError as exc:
        return None, "vision: %s %s" % (exc.status or "", exc.detail[:200])
    ann = resp.get("annotated_image_b64") if isinstance(resp.get("annotated_image_b64"), str) else None
    ocr = [{"text": o["text"].strip()[:80], "conf": o.get("conf")} for o in resp.get("ocr") or []
           if isinstance(o, dict) and isinstance(o.get("text"), str) and o["text"].strip()][:8]
    view = {"pairing": "demo", "kind": resp.get("kind") or (resp.get("router") or {}).get("modality"),
            "router": resp.get("router"),
            "rows": vision_rows(resp, cfg.trust_min), "ocr": ocr, "key_results": resp.get("key_results") or [],
            "demo_alert": resp.get("alert"), "latency_ms": resp.get("latency_ms") or int((time.perf_counter() - t0) * 1000),
            "version": resp.get("version") or resp.get("versions"), "media_id": resp.get("media_id"),
            "annotated_image_b64": ann if ann and len(ann) <= 300_000 else None}
    return view, None


def _sse_value(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def stream_post(url: str, body: Any, headers: Dict[str, str], deadline: float,
                on_event: Callable[[str, Any], bool]) -> bool:
    """POST JSON and read the text/event-stream answer, handing each event (name, parsed data) to on_event until it
    returns True (-> True) or the stream ends (-> False); `: keep-alive` comments are skipped. `deadline`
    (time.monotonic()) bounds the WHOLE call: every read waits at most the time left and a watchdog shuts the socket
    at the deadline, so a trickling stream cannot outlive it. A 200 that is not an event stream (plain JSON) is passed
    on as one `json` event {body, headers}. Raises HttpError: the status for a non-200 answer, None for transport
    errors and the deadline."""
    u = urllib.parse.urlsplit(url)
    left = deadline - time.monotonic()
    if left <= 0:
        raise HttpError(url, None, "timed out before the call")
    conn = (HTTPSConnection if u.scheme == "https" else HTTPConnection)(u.hostname or "", u.port, timeout=left)
    socks: List[socket.socket] = []
    expired = threading.Event()

    def watchdog() -> None:
        expired.set()
        for s in socks + [conn.sock]:
            if s is not None:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    timer = threading.Timer(left, watchdog)
    timer.daemon = True
    timer.start()
    try:
        h = {"Accept": "text/event-stream", "Content-Type": "application/json", "User-Agent": "spotlight-live-runner/1"}
        h.update(headers)
        conn.request("POST", (u.path or "/") + ("?" + u.query if u.query else ""),
                     body=json.dumps(body, default=str).encode("utf-8"), headers=h)
        socks.append(conn.sock)   # getresponse() may drop conn.sock (Connection: close); the response keeps it
        resp = conn.getresponse()
        if resp.status != 200:
            raise HttpError(url, resp.status, resp.read(400).decode("utf-8", "replace"))
        if "text/event-stream" not in (resp.getheader("Content-Type") or ""):
            raw = resp.read()
            return bool(on_event("json", {"body": json.loads(raw) if raw else {},
                                          "headers": {k.lower(): v for k, v in resp.getheaders()}}))
        buf, name, data = b"", None, []
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise socket.timeout("deadline")
            if socks[0] is not None:
                socks[0].settimeout(left)
            chunk = resp.read1(65536)
            if not chunk:
                if expired.is_set():
                    raise socket.timeout("deadline")
                return False
            *lines, buf = (buf + chunk).split(b"\n")
            for raw in lines:
                line = raw.rstrip(b"\r").decode("utf-8", "replace")
                if not line:                     # blank line: dispatch the event
                    if name or data:
                        if on_event(name or "message", _sse_value("\n".join(data))):
                            return True
                    name, data = None, []
                elif line.startswith(":"):       # comment (`: keep-alive`)
                    continue
                else:
                    field, _, value = line.partition(":")
                    value = value[1:] if value.startswith(" ") else value
                    if field == "event":
                        name = value
                    elif field == "data":
                        data.append(value)
    except HttpError:
        raise
    except (OSError, HTTPException, ValueError) as exc:
        if expired.is_set() or time.monotonic() >= deadline:
            raise HttpError(url, None, "timed out (whole call)") from None
        raise HttpError(url, None, "%s: %s" % (type(exc).__name__, exc)) from None
    finally:
        timer.cancel()
        conn.close()


class S2Result:
    """One System 2 call: the checked response (or an error) and how it arrived."""

    def __init__(self) -> None:
        self.out: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None
        self.image: Optional[str] = None           # X-System2-Image / final.image
        self.cache: Optional[str] = None           # X-System2-Cache / final.cache
        self.ms = 0
        self.via = "plain"                         # stream | plain | fallback (the stream answered 404/405/503)
        self.fallback_status: Optional[int] = None
        self.drafts = 0                            # draft events received
        self.first_draft_ms: Optional[int] = None
        self.call = 0                              # newest model call seen in the drafts


def _s2_error(exc: HttpError) -> str:
    return "System 2: %s" % " ".join(p for p in (str(exc.status or ""), exc.detail[:240]) if p)


def run_system2(cfg: Config, req: Dict[str, Any],
                on_draft: Optional[Callable[[int, Optional[int], Dict[str, Any]], None]] = None) -> S2Result:
    """System 2 within SYSTEM2_TIMEOUT_S in total. Streams (/v1/assess/stream) unless SYSTEM2_STREAM=0, handing every
    newest-call draft to on_draft(call, tokens, fields) on this thread (keep it non-blocking); a 404/405 (older
    build) or 503 (all stream slots busy) from the stream falls back to /v1/assess in the time left. The result is final.response, exactly /v1/assess's body."""
    r = S2Result()
    t0 = time.perf_counter()
    deadline = time.monotonic() + cfg.system2_timeout
    headers = {"X-API-Key": cfg.system2_key, "X-Request-ID": req.get("request_id") or ""}
    url = cfg.system2 + "/v1/assess/stream"

    def on_event(name: str, data: Any) -> bool:
        if name == "draft" and isinstance(data, dict) and isinstance(data.get("fields"), dict):
            call = data["call"] if isinstance(data.get("call"), int) else 1
            if call < r.call:
                return False                   # a straggler of an older call: the newest call wins
            r.call, r.drafts = call, r.drafts + 1
            if r.first_draft_ms is None:
                r.first_draft_ms = int((time.perf_counter() - t0) * 1000)
            if on_draft:
                tokens = data.get("tokens")
                on_draft(call, tokens if isinstance(tokens, int) else None, data["fields"])
        elif name == "final":
            resp = data.get("response") if isinstance(data, dict) else None
            if not isinstance(resp, dict):
                raise HttpError(url, None, "final event without a response")
            r.out, r.cache, r.image = resp, data.get("cache"), data.get("image")
            return True
        elif name == "json":                   # a 200 JSON answer instead of a stream: same as /v1/assess
            r.out, r.cache, r.image = (data["body"], data["headers"].get("x-system2-cache"),
                                       data["headers"].get("x-system2-image"))
            return True
        elif name == "error":
            raise HttpError(url, None, "stream error: %s" % ((data.get("detail") if isinstance(data, dict) else data)
                                                             or "unknown"))
        return False

    try:
        if cfg.system2_stream:
            try:
                r.via = "stream"
                if not stream_post(url, req, headers, deadline, on_event):
                    raise HttpError(url, None, "stream ended without a final event")
                return r
            except HttpError as exc:
                if exc.status not in STREAM_FALLBACK_STATUS:
                    raise
                r.via, r.fallback_status = "fallback", exc.status   # older build or streams busy: no drafts
        left = deadline - time.monotonic()
        if left <= 0:
            raise HttpError(cfg.system2 + "/v1/assess", None, "timed out (whole call)")
        out, h = http("POST", cfg.system2 + "/v1/assess", req, timeout=left, headers=headers)
        r.out, r.cache, r.image = out, h.get("x-system2-cache"), h.get("x-system2-image")
    except HttpError as exc:
        r.error = _s2_error(exc)
    finally:
        r.ms = int((time.perf_counter() - t0) * 1000)
    return r


def stream_off_reason(status: Optional[int]) -> str:
    return "streams are all busy" if status == 503 else "has no /v1/assess/stream"


def no_draft_reason(r: S2Result) -> str:
    """Why a streamed call produced no drafts, for the log: the model never ran, or it failed first."""
    path = (r.out or {}).get("path")
    if r.error:
        return " (%s)" % r.error[:80]
    if r.cache == "hit":
        return " (stored answer: nothing generated)"
    if path == "insufficient_context":
        return " (insufficient context: no model call)"
    return " (path %s, cache %s)" % (path, r.cache) if path or r.cache else ""


class DraftPoster:
    """Posts one stop's newest Qwen draft to the backend (`/draft`) from its own thread, so posting never slows the
    stream reader. Latest wins: offer() only replaces the pending snapshot (each draft is a full snapshot, so a
    skipped one costs nothing). At most one post per `interval` s (from the start of the previous post), short
    timeout, no retries; a snapshot over DRAFT_FIELDS_MAX is skipped. A failure is logged once and never fails the
    stop; a 4xx ends this stop's drafts (404: backend without /draft; 409: the run is no longer running, e.g.
    cancelled, or the stop is not triaged; 413: too large). close() drops any snapshot not yet sent and waits for
    the post in flight, so no draft lands after /analysis."""

    def __init__(self, be: Backend, run_id: int, idx: int, interval: float, tag: str,
                 timeout: float = DRAFT_POST_TIMEOUT_S):
        self.be, self.interval, self.tag, self.timeout = be, interval, tag, timeout
        self.path = "/live/runs/%d/stops/%d/draft" % (run_id, idx)
        self.cv = threading.Condition()
        self.pending: Optional[Dict[str, Any]] = None
        self.closed = False
        self.posted = self.failed = self.superseded = 0
        self.thread = threading.Thread(target=self._loop, name="draft-%d-%d" % (run_id, idx), daemon=True)
        self.thread.start()

    def offer(self, call: int, tokens: Optional[int], fields: Dict[str, Any]) -> None:
        body: Dict[str, Any] = {"call": call, "fields": fields}
        if tokens is not None:
            body["tokens"] = tokens
        with self.cv:
            if self.closed:
                return
            if self.pending is not None:
                self.superseded += 1
            self.pending = body
            self.cv.notify()

    def _loop(self) -> None:
        next_at = 0.0
        while True:
            with self.cv:
                while not self.closed and (self.pending is None or time.monotonic() < next_at):
                    self.cv.wait(None if self.pending is None else next_at - time.monotonic())
                if self.closed:
                    return
                body, self.pending = self.pending, None
            if len(json.dumps(body["fields"], default=str)) > DRAFT_FIELDS_MAX:
                continue                          # too big for the backend; the next snapshot may fit
            next_at = time.monotonic() + self.interval
            try:
                self.be.post(self.path, body, timeout=self.timeout, retries=0)
                self.posted += 1
            except Exception as exc:  # noqa: BLE001 - a draft is best effort, never fails the stop
                self.failed += 1
                status = getattr(exc, "status", None)
                if self.failed == 1:
                    log("%s draft post failed (%s); the checked answer still follows", self.tag,
                        ("%s %s" % (status or "", getattr(exc, "detail", exc))).strip()[:120])
                if isinstance(status, int) and 400 <= status < 500:
                    with self.cv:
                        self.closed, self.pending = True, None
                    return

    def close(self) -> None:
        with self.cv:
            self.closed, self.pending = True, None
            self.cv.notify_all()
        self.thread.join(self.timeout * 3 + 1)   # urllib's timeout is per socket operation
        if self.thread.is_alive():
            log("%s a draft post is still in flight; posting the analysis anyway", self.tag)


# ── one run ──────────────────────────────────────────────────────────────────────────────────────
def analyse(cfg: Config, be: Backend, run_id: int, idx: int, stop: Dict[str, Any], res: Dict[str, Any]) -> str:
    t0 = time.perf_counter()
    tag = "[stop %s]" % stop["stop"]
    emb1 = post_embeddings(cfg, be, run_id, res.get("embed") or [])
    path = PHOTO_ROOT / stop["photo"].lstrip("/")
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    b64 = "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))
    rid = "live-%d-%s-%s" % (run_id, stop["stop"], uuid.uuid4().hex[:6])
    tv = time.perf_counter()
    vision, vision_error = run_vision(cfg, stop, b64, rid)
    vision_ms = int((time.perf_counter() - tv) * 1000)
    req = json.loads(json.dumps(res["system2_request"]))
    req["request_id"] = rid
    ctx = req["context"]
    if vision and vision.get("rows"):
        ctx["vision"] = vision["rows"]
        ctx["citation_ids"] = list(ctx.get("citation_ids") or []) + [r["id"] for r in vision["rows"]]
    if vision and vision.get("ocr"):
        ctx["ocr"] = vision["ocr"]
    req["image_b64"] = b64
    log("%s Qwen analysing (photo %s, %d vision rows)", tag, path.name, len((vision or {}).get("rows") or []))
    poster = DraftPoster(be, run_id, idx, cfg.draft_interval, tag) if cfg.system2_stream else None
    try:
        r = run_system2(cfg, req, on_draft=poster.offer if poster else None)
    finally:
        if poster:
            poster.close()   # no draft after the result: drop the unsent one, wait for the post in flight
    s2, s2_error, image_status, s2_ms = r.out, r.error, r.image, r.ms
    if r.via == "stream":
        log("%s Qwen streamed %d drafts%s; %d posted%s", tag, r.drafts,
            ", first after %.1fs" % (r.first_draft_ms / 1000) if r.first_draft_ms is not None else no_draft_reason(r),
            poster.posted if poster else 0, ", %d failed" % poster.failed if poster and poster.failed else "")
    elif r.via == "fallback":
        log("%s System 2 %s (%s): plain /v1/assess, no drafts", tag, stream_off_reason(r.fallback_status),
            r.fallback_status)
    sent_req = dict(req)
    sent_req.pop("image_b64", None)
    an = be.post("/live/runs/%d/stops/%d/analysis" % (run_id, idx), {
        "photo": {"path": stop["photo"], "sha256": sha, "bytes": len(data)}, "vision": vision,
        "vision_error": vision_error, "system2_request": sent_req, "system2": s2, "system2_error": s2_error,
        "system2_ms": s2_ms, "image_status": image_status or ("sent" if s2 else None),
        "timings": {"vision_ms": vision_ms, "system2_ms": s2_ms,
                    "analysis_ms": int((time.perf_counter() - t0) * 1000)}}, timeout=60)
    emb2 = post_embeddings(cfg, be, run_id, an.get("embed") or [])
    src = an.get("qwen_source")
    log("%s %s in %.1fs%s; embeddings: %s / %s", tag, "Qwen answered" if src == "system2" else "TEMPLATE",
        s2_ms / 1000, "" if src == "system2" else " (%s)" % (an.get("fallback_reason") or "")[:120], emb1, emb2)
    return src or "?"


def walk(cfg: Config, be: Backend, claim: Dict[str, Any]) -> None:
    run, route = claim["run"], claim["route"]
    run_id = run["run_id"]
    log("RUN %d claimed: route %s (%s), %d stops", run_id, route["route_id"], route["name"], len(route["stops"]))
    pool = cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="qwen")   # one GPU: Qwen calls in order
    jobs: List[cf.Future] = []
    try:
        for i, stop in enumerate(route["stops"]):
            beat()
            state = be.get("/live/runs/%d" % run_id)["run"]
            if state["status"] != "running":
                log("RUN %d is %s: stopping the walk", run_id, state["status"])
                break
            if i:
                log("  walking to %s (%s)", stop["location_id"], stop.get("location_name") or "")
                time.sleep(cfg.travel_s)
            s = be.post("/live/runs/%d/stops/%d/start" % (run_id, i))
            log("[stop %s] at %s | %s on %s", stop["stop"], stop["location_id"], stop["action_type"], stop["asset_id"])
            time.sleep(cfg.work_s)
            ts = now_iso()
            ev = dict(s["action_event"], timestamp=ts)
            laya = call_laya(cfg, ev, "live-%d-%s-laya" % (run_id, stop["stop"]))
            res = be.post("/live/runs/%d/stops/%d/result" % (run_id, i), {"timestamp": ts, "laya": laya})
            lv = (res["capture"].get("laya") or {}).get("verdict") or {}
            log("[stop %s] %s %s | Laya %s | rules %s %s", stop["stop"], res["verdict"].upper(),
                res["capture"].get("priority"),
                ("escalate=%s p=%s" % (lv.get("escalate"), lv.get("p_escalate"))) if laya["status"] == "ok"
                else "UNAVAILABLE (%s)" % laya.get("error", "")[:80],
                (res["capture"].get("rules") or {}).get("priority"),
                ",".join((res["capture"].get("rules") or {}).get("fired") or []) or "-")
            jobs.append(pool.submit(analyse, cfg, be, run_id, i, stop, res))
        for j in jobs:
            beat()
            try:
                j.result()
            except Exception as exc:  # noqa: BLE001 - one stop's analysis must not abort the run
                log("  analysis failed: %s", exc)
        fin = be.post("/live/runs/%d/finish" % run_id, {"status": "done"})["run"]
        log("RUN %d done: %s", run_id, json.dumps(fin.get("counts")))
    except HttpError as exc:
        if exc.status == 409 and "cancelled" in exc.detail:
            log("RUN %d was cancelled by the operator: stopping", run_id)
            return
        log("RUN %d failed: %s", run_id, exc)
        try:
            be.post("/live/runs/%d/finish" % run_id, {"status": "error", "error": str(exc)[:400]}, retries=0)
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        log("RUN %d failed: %s", run_id, exc)
        try:
            be.post("/live/runs/%d/finish" % run_id, {"status": "error", "error": str(exc)[:400]}, retries=0)
        except Exception:  # noqa: BLE001
            pass
    finally:
        pool.shutdown(wait=False)


def serve(cfg: Config, once: bool) -> None:
    be = Backend(cfg)
    log("live runner '%s' polling %s for queued runs (Ctrl-C to stop)", cfg.name, cfg.backend)
    while True:
        beat()
        try:
            claim = be.post("/live/claim", {"runner": cfg.name}, retries=0)
        except HttpError as exc:
            log("claim failed: %s %s", exc.status or "", exc.detail[:160])
            time.sleep(5)
            continue
        if claim.get("run"):
            walk(cfg, be, claim)
            if once:
                return
        time.sleep(2)


# ── checks ───────────────────────────────────────────────────────────────────────────────────────
def check(cfg: Config) -> int:
    """One REAL request to each service (llama.cpp can keep /health OK while every answer fails)."""
    ok = True
    be = Backend(cfg)
    try:
        prev = be.get("/live/preview?route_id=R2")
        log("backend   OK   %d stops in the R2 preview", len(prev["stops"]))
    except HttpError as exc:
        ok = False
        log("backend   FAIL %s %s", exc.status, exc.detail[:160])
        return 1
    stop = next(s for s in prev["stops"] if s["stop"] == "E")
    laya = call_laya(cfg, stop["action_event"], "live-check-laya")
    if laya["status"] == "ok":
        log("laya      OK   %s on %s, %s ms, escalate=%s", laya.get("model"), laya.get("device"), laya.get("latency_ms"),
            (laya["answers"].get("escalate") or {}).get("answer"))
    else:
        ok = False
        log("laya      FAIL %s", laya["error"])
    vecs = embed_texts(cfg, ["live runner check"])
    log("embed     %s", "OK   %d dims" % len(vecs[0]) if vecs else "FAIL")
    ok &= bool(vecs)
    photo = PHOTO_ROOT / "kit" / "inspection" / "gauge_pressure_01.jpg"
    b64 = "data:image/jpeg;base64," + base64.b64encode(photo.read_bytes()).decode()
    v, verr = run_vision(cfg, {"photo": "/kit/inspection/gauge_pressure_01.jpg", "photo_kind": "gauge",
                               "action_type": "GAUGE_READING"}, b64, "live-check-vision")
    log("vision    %s", "OK   %d rows, %s" % (len(v["rows"]), [(r.get("value"), r.get("unit")) for r in v["rows"]][:3])
        if v else "FAIL %s" % verr)
    ok &= v is not None
    from_fixture = REPO / "amd" / "system2" / "fixtures" / "multimodal" / "gauge_demo.json"
    req = json.loads(from_fixture.read_text(encoding="utf-8"))["request"]
    req["request_id"] = "live-check-system2"
    r = run_system2(cfg, req, on_draft=lambda *_: None)   # streams like a stop, posts nothing
    out, err, img, ms = r.out, r.error, r.image, r.ms
    if out:
        real = out.get("path") not in ("fallback", "insufficient_context") and out.get("model") not in ("template", "stub")
        log("system2   %s model=%s path=%s %d ms image=%s", "OK  " if real else "TEMPLATE", out.get("model"),
            out.get("path"), ms, img)
        if not real:
            log("          Qwen did not answer: restart llama-server (empty grammar stack bug), see the runbook")
        ok &= real
    else:
        ok = False
        log("system2   FAIL %s", err)
    # drafts are a display nicety: the stream line never changes the exit code
    if not cfg.system2_stream:
        log("stream    OFF  SYSTEM2_STREAM=0: plain /v1/assess, no drafts on /live")
    elif r.via == "fallback":
        log("stream    OFF  System 2 answered /v1/assess/stream with %s (%s): plain /v1/assess, no drafts",
            r.fallback_status, "streams busy" if r.fallback_status == 503 else "older build")
    elif out:
        log("stream    OK   %d drafts%s, cache=%s%s", r.drafts,
            ", first after %.1fs" % (r.first_draft_ms / 1000) if r.first_draft_ms is not None else "", r.cache,
            " (stored answer: nothing generated, so no drafts)" if r.cache == "hit" and not r.drafts else "")
    else:
        log("stream    FAIL %d drafts before the error", r.drafts)
    return 0 if ok else 1


def dry_run(cfg: Config, route: Optional[str]) -> int:
    """Laya on every stop (no writes): where does the live verdict (rules ‖ Laya) differ from the plan?"""
    be = Backend(cfg)
    prev = be.get("/live/preview" + ("?route_id=%s" % route if route else ""))
    rows, disagree = [], 0
    for s in prev["stops"]:
        laya = call_laya(cfg, s["action_event"], "live-dryrun-%s-%s" % (s["route_id"], s["stop"]))
        esc = (laya.get("answers") or {}).get("escalate") or {}
        laya_red = esc.get("answer") == "A" if laya["status"] == "ok" else None
        rules_red = bool((s["rules"] or {}).get("escalate"))
        live = "red" if rules_red or laya_red else "green"
        note = ""
        if laya_red is None:
            note = "LAYA UNAVAILABLE"
        elif laya_red != (s["planned_verdict"] == "red"):
            note = "Laya %s, plan %s%s" % ("escalates" if laya_red else "does not escalate", s["planned_verdict"],
                                           " (rules catch it)" if rules_red else " -> LIVE VERDICT DIFFERS")
        if live != s["planned_verdict"] or laya_red is None or note:
            disagree += 1
        p = (esc.get("probs") or {}).get("A")
        rows.append((s["route_id"], s["stop"], s["source_action_id"], s["planned_verdict"], s["planned_priority"],
                     "red" if rules_red else "green", "-" if laya_red is None else ("red" if laya_red else "green"),
                     "-" if p is None else "%.2f" % p, live, note))
    print("route stop action  plan  prio rules laya  p_esc live  note")
    for r in rows:
        print("%-5s %-4s %-7s %-5s %-4s %-5s %-5s %-5s %-5s %s" % r)
    model = laya.get("model") if prev["stops"] else None
    print("\n%d stops, %d with a note. Laya model: %s. Mode: %s (rules ‖ Laya; either escalates)."
          % (len(rows), disagree, model, prev.get("mode")))
    return 0


# ── tunnels ──────────────────────────────────────────────────────────────────────────────────────
def open_tunnels(host: str) -> subprocess.Popen:
    ssh = shutil.which("ssh")
    if not ssh:
        sys.exit("ssh not found on PATH")
    args = [ssh, "-N", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15"]
    for local, remote in TUNNELS.values():
        args += ["-L", "%d:127.0.0.1:%d" % (local, remote)]
    proc = subprocess.Popen(args + [host])
    time.sleep(3)
    if proc.poll() is not None:
        sys.exit("ssh tunnels to %s failed (exit %s); is `ssh %s` working?" % (host, proc.returncode, host))
    log("tunnels open to %s: %s", host, ", ".join("%d->%d" % v for v in TUNNELS.values()))
    return proc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend-url")
    ap.add_argument("--admin-token")
    ap.add_argument("--tunnel", metavar="SSH_HOST", help="open the SSH tunnels to the AMD box first (e.g. spot-amd)")
    ap.add_argument("--name", help="runner label shown on the run (default presenter-laptop)")
    ap.add_argument("--travel-s", type=float, default=5.0, help="walking time between stops (LIVE_TRAVEL_S)")
    ap.add_argument("--work-s", type=float, default=3.0, help="inspection time at a stop (LIVE_WORK_S)")
    ap.add_argument("--once", action="store_true", help="serve one run, then exit")
    ap.add_argument("--check", action="store_true", help="one real request to every service, then exit")
    ap.add_argument("--dry-run", action="store_true", help="Laya on all stops vs the plan (no writes)")
    ap.add_argument("--route", help="--dry-run: only this route (R1..R5)")
    a = ap.parse_args()
    cfg = Config(a)
    cfg.need_backend()
    tunnel = open_tunnels(a.tunnel) if a.tunnel else None
    if tunnel:
        signal.signal(signal.SIGTERM, lambda *_: (tunnel.terminate(), sys.exit(0)))
    try:
        if a.check:
            return check(cfg)
        if a.dry_run:
            return dry_run(cfg, a.route)
        serve(cfg, a.once)
        return 0
    except KeyboardInterrupt:
        log("stopped")
        return 0
    finally:
        if tunnel:
            tunnel.terminate()


if __name__ == "__main__":
    sys.exit(main())
