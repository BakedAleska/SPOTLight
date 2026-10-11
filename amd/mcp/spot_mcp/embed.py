"""Query embeddings through the OpenAI-compatible /v1/embeddings API (contract §1)."""
from __future__ import annotations

import asyncio
import math
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Protocol
from urllib.parse import urlparse

from .db import LogSink, NullLogSink


class EmbeddingError(RuntimeError):
    def __init__(self, msg: str, retryable: bool = False):
        super().__init__(msg)
        self.retryable = retryable


# One retry for a dropped connection or a proxy/tunnel hiccup (the query path goes over a tunnel): statuses a
# retry can fix, and transport errors other than a timeout (a timeout already spent the budget).
RETRY_STATUS = (429, 502, 503, 504)


class Embedder(Protocol):
    # flag_id / action_id only label the call's app.model_calls row; embedders may omit them (ContextService checks).
    async def embed_queries(self, texts: List[str], request_id: Optional[str] = None, *,
                            flag_id: Optional[int] = None, action_id: Optional[str] = None) -> List[List[float]]: ...
    async def close(self) -> None: ...


def _host(url: str) -> Optional[str]:
    """host[:port] of a URL, never its credentials."""
    try:
        u = urlparse(url)
        return (u.hostname or "") + (":%d" % u.port if u.port else "") or None
    except ValueError:
        return None


def _pos(v: Any) -> Optional[float]:
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0
    return float(v) if ok else None


def _text(*vals: Any) -> Optional[str]:
    """The first non-blank string (app.model_calls.device is text; anything else would fail the whole insert)."""
    return next((v.strip()[:200] for v in vals if isinstance(v, str) and v.strip()), None)


class HttpEmbedder:
    """Prepends EMBED_QUERY_PREFIX, batches inputs, checks the dimension and caches recent queries.

    Each HTTP call is one app.model_calls row: endpoint is the URL path only (the host, often a short-lived tunnel,
    goes to metadata.base_host), device/tokens_per_sec come from the server's `timing` block when it sends one."""

    def __init__(self, url: str, model: str, dim: int, query_prefix: str = "", timeout_s: float = 10.0,
                 sink: Optional[LogSink] = None, cache_size: int = 256, transport: Any = None,
                 retries: int = 1, retry_delay_s: float = 0.5):
        import httpx2

        self.url = url.rstrip("/") + "/v1/embeddings"
        try:
            u = urlparse(self.url)
            self.endpoint, scheme = u.path or "/v1/embeddings", u.scheme or "http"
        except ValueError:
            self.endpoint, scheme = "/v1/embeddings", "http"
        self.base_host = _host(self.url)
        # what errors show: never the URL's user:password (an EmbeddingError reaches packet gaps and tool-call rows)
        self._shown_url = "%s://%s%s" % (scheme, self.base_host, self.endpoint) if self.base_host else self.endpoint
        self.model, self.dim, self.prefix = model, dim, query_prefix
        self._sink = sink or NullLogSink()
        self._cache: "OrderedDict[str, List[float]]" = OrderedDict()
        self._cache_size = cache_size
        self._client = httpx2.AsyncClient(timeout=timeout_s, transport=transport)
        self._timeout_s, self.retries, self.retry_delay_s = timeout_s, max(0, retries), retry_delay_s

    async def embed_queries(self, texts: List[str], request_id: Optional[str] = None, *,
                            flag_id: Optional[int] = None, action_id: Optional[str] = None) -> List[List[float]]:
        out: Dict[int, List[float]] = {}
        todo = []
        for i, t in enumerate(texts):
            hit = self._cache.get(t)
            if hit is not None:
                self._cache.move_to_end(t)
                out[i] = hit
            else:
                todo.append(i)
        if todo:
            vecs = await self._post([self.prefix + texts[i] for i in todo], request_id, flag_id, action_id)
            for i, v in zip(todo, vecs):
                out[i] = v
                self._cache[texts[i]] = v
                if len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
        return [out[i] for i in range(len(texts))]

    async def _post(self, inputs: List[str], request_id: Optional[str], flag_id: Optional[int] = None,
                    action_id: Optional[str] = None) -> List[List[float]]:
        """`_post_once`, retried (after retry_delay_s) on a retryable error while the first half of the timeout is
        not spent, so a retry stays inside the caller's budget. Every attempt is its own app.model_calls row."""
        start = time.perf_counter()
        for attempt in range(self.retries + 1):
            # a retry gets only what is left of the timeout, never a fresh full one
            left_s = None if attempt == 0 else max(0.5, self._timeout_s - (time.perf_counter() - start))
            try:
                return await self._post_once(inputs, request_id, flag_id, action_id, timeout_s=left_s)
            except EmbeddingError as exc:
                left = self._timeout_s / 2 - (time.perf_counter() - start) - self.retry_delay_s
                if not exc.retryable or attempt >= self.retries or left <= 0:
                    raise
            await asyncio.sleep(self.retry_delay_s)
        raise AssertionError("unreachable")

    async def _post_once(self, inputs: List[str], request_id: Optional[str], flag_id: Optional[int] = None,
                         action_id: Optional[str] = None, timeout_s: Optional[float] = None) -> List[List[float]]:
        import httpx2

        t0 = time.perf_counter()
        err: Optional[str] = None
        usage: Dict[str, Any] = {}
        timing: Dict[str, Any] = {}
        try:
            extra = {"timeout": timeout_s} if timeout_s is not None else {}
            r = await self._client.post(self.url, json={"model": self.model, "input": inputs}, **extra)
            if r.status_code != 200:
                raise EmbeddingError("embedding service returned HTTP %d: %s" % (r.status_code, r.text[:200]),
                                     retryable=r.status_code in RETRY_STATUS)
            body = r.json()
            usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
            timing = body.get("timing") if isinstance(body.get("timing"), dict) else {}
            data = sorted(body["data"], key=lambda d: d.get("index", 0))
            vecs = [d["embedding"] for d in data]
            if len(vecs) != len(inputs):
                raise EmbeddingError("embedding service returned %d vectors for %d inputs" % (len(vecs), len(inputs)))
            for v in vecs:
                if len(v) != self.dim:
                    raise EmbeddingError("embedding dimension %d != EMBED_DIM %d (wrong EMBED_MODEL?)" % (len(v), self.dim))
            return vecs
        except EmbeddingError as exc:
            err = str(exc)
            raise
        except Exception as exc:  # network errors, bad JSON
            err = "%s: %s" % (type(exc).__name__, exc)
            retryable = isinstance(exc, httpx2.TransportError) and not isinstance(exc, httpx2.TimeoutException)
            raise EmbeddingError("embedding service unreachable at %s (%s)" % (self._shown_url, err),
                                 retryable=retryable) from exc
        finally:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            tokens = usage.get("prompt_tokens") or usage.get("total_tokens")
            tps = _pos(timing.get("tokens_per_sec"))
            if tps is None and err is None and _pos(tokens) and latency_ms > 0:
                tps = round(float(tokens) / (latency_ms / 1000.0), 1)
            meta: Dict[str, Any] = {"n_inputs": len(inputs), "caller": "spot-mcp", "base_host": self.base_host}
            for k_out, k_in in (("server_ms", "ms"), ("dtype", "dtype")):
                if timing.get(k_in) is not None:
                    meta[k_out] = timing[k_in]
            self._sink.model_call({
                "request_id": request_id, "system": "embed", "model": self.model, "endpoint": self.endpoint,
                "flag_id": flag_id if isinstance(flag_id, int) and not isinstance(flag_id, bool) else None,
                "action_id": action_id if isinstance(action_id, str) and action_id else None,
                "input_tokens": tokens, "latency_ms": latency_ms, "tokens_per_sec": tps,
                "device": _text(timing.get("gpu"), timing.get("device")),
                "ok": err is None, "error": err, "metadata": meta,
            })

    async def close(self) -> None:
        await self._client.aclose()
