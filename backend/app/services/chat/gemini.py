"""Gemini REST client for the chat (docs/chat-contract.md §0, §5). httpx only, no SDK.

- `POST {GEMINI_BASE_URL}/models/{model}:streamGenerateContent?alt=sse` for answers, `:generateContent` for titles.
- The key goes in the `x-goog-api-key` header and nowhere else: never in a URL, a log line, an error detail or a
  response. Every string that leaves this module passes `scrub()` as a second line of defence.
- One attempt, no retries, no model fallback: the free tier allows 5 requests/min/model, so a retry would only burn
  quota, and silently answering with another model would hide which model spoke (same rule as the gateway).
- Errors become `GeminiError(code)` with the contract's codes; a 429 carries `retry_after_s` from RetryInfo. A 503,
  or an in-stream error with status UNAVAILABLE / "high demand", is `model_unavailable` (`chat.2`).
- Every call is logged to `app.model_calls` with `system='chat'`, `metadata.provider='gemini'`.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import httpx

from ..calllog import LogSink
from ..http import short_error

# finishReason values that mean the answer was withheld by a safety or policy filter.
BLOCKED_FINISH = {"SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY",
                  "IMAGE_PROHIBITED_CONTENT", "IMAGE_RECITATION"}
_MODEL_400 = re.compile(r"models?/|thinking|not supported|unsupported|developer instruction|system instruction|"
                        r"model\b.*\b(not found|unavailable|does not|doesn't)", re.I)
# A model that is overloaded right now (HTTP 503, or an in-stream error with status UNAVAILABLE): `chat.2` maps it to
# model_unavailable, so the operator is offered another model instead of a generic upstream error.
_OVERLOADED = re.compile(r"high demand|overloaded|currently unavailable", re.I)
_DELAY = re.compile(r"(\d+(?:\.\d+)?)\s*s\b")
_RETRY_IN = re.compile(r"retry in (\d+(?:\.\d+)?)\s*s", re.I)


class GeminiError(Exception):
    """A failed Gemini call. `code` is one of the contract's error codes (§4)."""

    def __init__(self, code: str, detail: str, http_status: Optional[int] = None,
                 retry_after_s: Optional[int] = None):
        super().__init__(detail)
        self.code, self.detail, self.http_status, self.retry_after_s = code, detail, http_status, retry_after_s


@dataclass
class StreamResult:
    usage: Dict[str, Optional[int]] = field(default_factory=dict)
    finish_reason: Optional[str] = None
    model_version: Optional[str] = None


def parse_retry_delay(value: Any) -> Optional[int]:
    """RetryInfo.retryDelay ("41s", "41.53s") -> whole seconds, rounded up."""
    if value is None:
        return None
    m = _DELAY.search(str(value))
    return max(1, math.ceil(float(m.group(1)))) if m else None


def usage_of(meta: Optional[Dict[str, Any]]) -> Dict[str, Optional[int]]:
    meta = meta or {}
    return {"prompt_tokens": meta.get("promptTokenCount"), "output_tokens": meta.get("candidatesTokenCount"),
            "thoughts_tokens": meta.get("thoughtsTokenCount")}


class GeminiClient:
    name = "gemini"

    def __init__(self, api_key: Optional[str], base_url: str, http: httpx.AsyncClient, sink: LogSink,
                 timeout_s: float = 120.0):
        self._key = api_key or None
        self.base_url = (base_url or "").rstrip("/")
        self.http = http
        self.sink = sink
        self.timeout_s = timeout_s

    def __repr__(self) -> str:   # never the key
        return "GeminiClient(base_url=%r, configured=%s)" % (self.base_url, self.configured)

    @property
    def configured(self) -> bool:
        return bool(self._key)

    def scrub(self, text: Any) -> str:
        s = "" if text is None else str(text)
        return s.replace(self._key, "[redacted]") if self._key and self._key in s else s

    def _headers(self) -> Dict[str, str]:
        return {"x-goog-api-key": self._key or "", "content-type": "application/json"}

    # ── errors ──────────────────────────────────────────────────────────────────────────────────
    def map_http_error(self, status: int, raw: bytes, model: str) -> GeminiError:
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        if isinstance(body, list):          # the streaming endpoint wraps errors in a JSON array
            body = body[0] if body and isinstance(body[0], dict) else {}
        err = body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), dict) else {}
        msg = self.scrub(err.get("message") or raw.decode("utf-8", "replace")[:300] or "HTTP %d" % status)
        details = [d for d in (err.get("details") or []) if isinstance(d, dict)]
        retry = next((parse_retry_delay(d.get("retryDelay")) for d in details
                      if str(d.get("@type", "")).endswith("RetryInfo")), None)
        if retry is None:
            m = _RETRY_IN.search(msg)
            retry = max(1, math.ceil(float(m.group(1)))) if m else None
        reasons = {str(d.get("reason")) for d in details if d.get("reason")}
        rpc_status = str(err.get("status") or "")
        short = " ".join(msg.split())[:240]
        if status == 429:
            return GeminiError("rate_limited", "%s is rate limited (free tier: 5 requests/min per model): %s"
                               % (model, short), status, retry or 60)
        if status in (401, 403) or "API_KEY_INVALID" in reasons or re.search(r"api key", msg, re.I):
            return GeminiError("gemini_auth", "Gemini rejected the API key (HTTP %d): %s" % (status, short), status)
        if status == 404 or (status == 400 and _MODEL_400.search(msg)):
            return GeminiError("model_unavailable", "%s is not available (HTTP %d): %s" % (model, status, short),
                               status)
        if status == 503 or rpc_status == "UNAVAILABLE" or _OVERLOADED.search(msg):
            return GeminiError("model_unavailable", "%s is unavailable right now (HTTP %d%s): %s"
                               % (model, status, ", " + rpc_status if rpc_status else "", short), status)
        return GeminiError("upstream_error", "Gemini returned HTTP %d for %s: %s" % (status, model, short), status)

    # ── streaming answer ────────────────────────────────────────────────────────────────────────
    async def stream(self, model: str, body: Dict[str, Any], on_part: Callable[[bool, str], None], *,
                     request_id: Optional[str], metadata: Optional[Dict[str, Any]] = None,
                     timeout_s: Optional[float] = None) -> StreamResult:
        """Run one streamed generation. `on_part(is_thought, text)` is called for every text part as it arrives.
        Raises GeminiError on any failure (timeouts and transport errors are `upstream_error`). Cancellation
        (the operator pressed Stop) closes the HTTP stream and is logged, then re-raised."""
        if not self._key:
            raise GeminiError("gemini_unset", "GEMINI_API_KEY is not set on the backend")
        timeout = timeout_s or self.timeout_s
        url = "%s/models/%s:streamGenerateContent?alt=sse" % (self.base_url, model)
        res = StreamResult()
        t0 = time.perf_counter()
        first: List[float] = []
        meta = {"provider": "gemini", "purpose": "answer", **(metadata or {})}

        def part(is_thought: bool, text: str) -> None:
            if not first:
                first.append(time.perf_counter())
            on_part(is_thought, text)

        try:
            async with asyncio.timeout(timeout):
                async with self.http.stream("POST", url, json=body, headers=self._headers(),
                                            timeout=httpx.Timeout(timeout, connect=10.0)) as r:
                    if r.status_code != 200:
                        raise self.map_http_error(r.status_code, await r.aread(), model)
                    data: List[str] = []
                    async for line in r.aiter_lines():
                        if line.startswith("data:"):
                            data.append(line[5:].strip())
                        elif not line.strip() and data:
                            self._chunk("\n".join(data), res, part, model)
                            data = []
                    if data:
                        self._chunk("\n".join(data), res, part, model)
        except GeminiError as exc:
            self._log(False, "gemini:streamGenerateContent", model, t0, request_id, res, meta, first,
                      error=exc.detail, code=exc.code, http_status=exc.http_status)
            raise
        except TimeoutError:
            err = GeminiError("upstream_error", "Gemini did not finish within CHAT_TIMEOUT_S=%gs" % timeout)
            self._log(False, "gemini:streamGenerateContent", model, t0, request_id, res, meta, first,
                      error=err.detail, code=err.code)
            raise err from None
        except asyncio.CancelledError:
            self._log(False, "gemini:streamGenerateContent", model, t0, request_id, res, meta, first,
                      error="stopped: the client disconnected", code="stopped")
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            err = GeminiError("upstream_error", "Gemini call failed: %s" % self.scrub(short_error(exc)))
            self._log(False, "gemini:streamGenerateContent", model, t0, request_id, res, meta, first,
                      error=err.detail, code=err.code)
            raise err from exc
        self._log(True, "gemini:streamGenerateContent", model, t0, request_id, res, meta, first)
        return res

    def _chunk(self, raw: str, res: StreamResult, part: Callable[[bool, str], None], model: str) -> None:
        obj = json.loads(raw)
        if isinstance(obj, list):
            obj = obj[0] if obj else {}
        if isinstance(obj.get("error"), dict):
            raise self.map_http_error(int(obj["error"].get("code") or 500), raw.encode(), model)
        if obj.get("usageMetadata"):
            res.usage = usage_of(obj["usageMetadata"])
        res.model_version = obj.get("modelVersion") or res.model_version
        block = (obj.get("promptFeedback") or {}).get("blockReason")
        if block:
            raise GeminiError("blocked", "Gemini blocked the request (%s)" % block)
        cands = obj.get("candidates") or []
        if not cands:
            return
        c = cands[0]
        for p in ((c.get("content") or {}).get("parts") or []):
            text = p.get("text")
            if isinstance(text, str) and text:
                part(bool(p.get("thought")), text)
        if c.get("finishReason"):
            res.finish_reason = c["finishReason"]
            if res.finish_reason in BLOCKED_FINISH:
                raise GeminiError("blocked", "Gemini stopped the answer (finishReason %s)" % res.finish_reason)

    # ── one-shot (titles) ───────────────────────────────────────────────────────────────────────
    async def generate(self, model: str, body: Dict[str, Any], *, request_id: Optional[str], timeout_s: float,
                       metadata: Optional[Dict[str, Any]] = None) -> str:
        """Non-streamed `generateContent`; returns the answer text (thought parts dropped)."""
        if not self._key:
            raise GeminiError("gemini_unset", "GEMINI_API_KEY is not set on the backend")
        url = "%s/models/%s:generateContent" % (self.base_url, model)
        res = StreamResult()
        meta = {"provider": "gemini", **(metadata or {})}
        t0 = time.perf_counter()
        try:
            async with asyncio.timeout(timeout_s):
                r = await self.http.post(url, json=body, headers=self._headers(),
                                         timeout=httpx.Timeout(timeout_s, connect=min(5.0, timeout_s)))
            if r.status_code != 200:
                raise self.map_http_error(r.status_code, r.content, model)
            obj = r.json()
            res.usage = usage_of(obj.get("usageMetadata"))
            res.model_version = obj.get("modelVersion")
            c = (obj.get("candidates") or [{}])[0]
            res.finish_reason = c.get("finishReason")
            text = "".join(p.get("text") or "" for p in ((c.get("content") or {}).get("parts") or [])
                           if not p.get("thought"))
        except GeminiError as exc:
            self._log(False, "gemini:generateContent", model, t0, request_id, res, meta, [], error=exc.detail,
                      code=exc.code, http_status=exc.http_status)
            raise
        except TimeoutError:
            err = GeminiError("upstream_error", "no answer within %gs" % timeout_s)
            self._log(False, "gemini:generateContent", model, t0, request_id, res, meta, [], error=err.detail,
                      code=err.code)
            raise err from None
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            err = GeminiError("upstream_error", "Gemini call failed: %s" % self.scrub(short_error(exc)))
            self._log(False, "gemini:generateContent", model, t0, request_id, res, meta, [], error=err.detail,
                      code=err.code)
            raise err from exc
        self._log(True, "gemini:generateContent", model, t0, request_id, res, meta, [])
        return text

    # ── logging ─────────────────────────────────────────────────────────────────────────────────
    def _log(self, ok: bool, endpoint: str, model: str, t0: float, request_id: Optional[str], res: StreamResult,
             meta: Dict[str, Any], first: List[float], error: Optional[str] = None, code: Optional[str] = None,
             http_status: Optional[int] = None) -> None:
        ms = int((time.perf_counter() - t0) * 1000)
        u = res.usage or {}
        out = (u.get("output_tokens") or 0) + (u.get("thoughts_tokens") or 0)
        md = dict(meta)
        md.update({k: v for k, v in (("finish_reason", res.finish_reason), ("thoughts_tokens",
                                                                           u.get("thoughts_tokens")),
                                     ("model_version", res.model_version), ("code", code),
                                     ("http_status", http_status),
                                     ("first_token_ms", int((first[0] - t0) * 1000) if first else None))
                   if v is not None})
        self.sink.model_call(system="chat", ok=ok, endpoint=endpoint, request_id=request_id, model=model,
                             latency_ms=ms, input_tokens=u.get("prompt_tokens"), output_tokens=out or None,
                             tokens_per_sec=(round(out / (ms / 1000.0), 1) if out and ms else None),
                             error=self.scrub(error) if error else None, metadata=md)
