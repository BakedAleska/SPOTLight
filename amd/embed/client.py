"""Tiny client for the contract's OpenAI-compatible embedding API (contract §1).

    from client import embed
    vecs = embed(["pump bearing too hot"], kind="query")      # adds EMBED_QUERY_PREFIX
    vecs = embed(docs, kind="doc")                             # adds EMBED_DOC_PREFIX

Works against amd/embed/server.py, llama-server --embedding, TEI or Infinity. Vectors are returned as-is
(no client-side normalization; the search functions use cosine distance). `to_pgvector()` formats a vector
as a pgvector literal for `%s::vector` parameters.

Defaults match the backend and the MCP server (backend/app/config.py, amd/mcp/spot_mcp/config.py):
- EMBED_MODEL unset -> Qwen/Qwen3-Embedding-0.6B; set but empty -> the model the server reports (check_health).
- EMBED_QUERY_PREFIX unset -> the Qwen3 instruction ending in "Query:" (no trailing space); set but empty -> none.
- EMBED_DOC_PREFIX unset or empty -> none.
- EMBED_CLIENT_BATCH texts per request (default 16; the SSH bridge to the AMD box drops long requests).
- EMBED_WAIT_S: how long wait_ready() polls /health (default 900). An unknown host name fails at once.
Each batch is retried on transport errors and HTTP 429/502/503/504 (5 attempts, backoff 1, 2, 4, 8 s).
"""
import os
import socket
import sys
import time
from typing import Callable, List, Optional, Sequence

import httpx

DEFAULT_MODEL = "Qwen/Qwen3-Embedding-0.6B"
# Same text as DEFAULT_QUERY_PREFIX in backend/app/config.py and amd/mcp/spot_mcp/config.py (contract §1).
DEFAULT_QUERY_PREFIX = ("Instruct: Given an operator question about a SPOT robot inspection, retrieve relevant "
                        "inspection events, anomaly notes, operator notes, and Spot manual passages\nQuery:")
DEFAULT_BATCH = 16
DEFAULT_WAIT_S = 900.0
ENDPOINT = "/v1/embeddings"
RETRY_STATUS = frozenset({429, 502, 503, 504})
BACKOFF_S = (1, 2, 4, 8)              # sleeps between the 5 attempts of one batch


def _env_prefix(name: str, default: str = "") -> str:
    # Unset -> default; set (even to "") -> that value. .env files can't hold a real newline easily: accept a
    # literal "\n" and turn it into one.
    v = os.environ.get(name)
    return default if v is None else v.replace("\\n", "\n")


def _dns_failure(exc: BaseException) -> bool:
    """True if a connect error comes from an unknown host name (fail fast instead of polling for 15 minutes)."""
    fatal = {getattr(socket, n) for n in ("EAI_NONAME", "EAI_NODATA") if hasattr(socket, n)}
    seen = set()
    e: Optional[BaseException] = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, socket.gaierror):
            return e.errno in fatal
        e = e.__cause__ or e.__context__
    msg = str(exc).lower()
    return any(s in msg for s in ("name or service not known", "nodename nor servname", "no address associated"))


class EmbedClient:
    def __init__(self, url: Optional[str] = None, model: Optional[str] = None, dim: Optional[int] = None,
                 query_prefix: Optional[str] = None, doc_prefix: Optional[str] = None,
                 batch_size: Optional[int] = None, timeout: float = 300.0,
                 on_call: Optional[Callable[[dict], None]] = None):
        self.url = (url or os.environ.get("EMBED_URL", "http://localhost:8081")).rstrip("/")
        env_model = os.environ.get("EMBED_MODEL")
        self.model = model if model is not None else (DEFAULT_MODEL if env_model is None else env_model.strip())
        self.dim = dim or int(os.environ.get("EMBED_DIM") or 1024)
        self.query_prefix = _env_prefix("EMBED_QUERY_PREFIX", DEFAULT_QUERY_PREFIX) if query_prefix is None \
            else query_prefix
        self.doc_prefix = _env_prefix("EMBED_DOC_PREFIX") if doc_prefix is None else doc_prefix
        self.batch_size = max(1, int(batch_size or os.environ.get("EMBED_CLIENT_BATCH") or DEFAULT_BATCH))
        self.backoff = BACKOFF_S
        self.on_call = on_call            # called once per HTTP batch with timing (for app.model_calls)
        self.http = httpx.Client(timeout=timeout)

    def health(self) -> dict:
        r = self.http.get(self.url + "/health")
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else {"status": r.status_code}

    def wait_ready(self, timeout_s: Optional[float] = None) -> dict:
        if timeout_s is None:
            timeout_s = float(os.environ.get("EMBED_WAIT_S") or DEFAULT_WAIT_S)
        t0 = time.time()
        while True:
            try:
                h = self.health()
                if h.get("status") == "ok":
                    return h
                if h.get("status") == "error":
                    raise RuntimeError("embedding server failed: %s" % h)
            except httpx.TransportError as e:
                if _dns_failure(e):
                    raise RuntimeError("embedding server host in EMBED_URL does not resolve (%s); set EMBED_URL "
                                       "to a reachable server" % e) from e
            if time.time() - t0 > timeout_s:
                raise TimeoutError("embedding server at %s not ready after %ds (EMBED_WAIT_S)" % (self.url, timeout_s))
            time.sleep(2)

    def check_health(self, health: dict) -> dict:
        """Before any write: the server must run EMBED_MODEL at EMBED_DIM. Adopts the server's model when EMBED_MODEL
        is set but empty. Exits (SystemExit) on a mismatch, so vectors of two models never share one label."""
        served = (health or {}).get("model")
        if not self.model:
            if not served:
                raise SystemExit("EMBED_MODEL is empty and the embedding server's /health reports no model; "
                                 "set EMBED_MODEL")
            self.model = served
        elif served and served != self.model:
            raise SystemExit("EMBED_MODEL=%s but the embedding server runs %s; fix EMBED_MODEL or EMBED_URL"
                             % (self.model, served))
        dim = (health or {}).get("dim")
        if dim is not None and int(dim) != self.dim:
            raise SystemExit("EMBED_DIM=%d but the embedding server (%s) returns %s dims; fix EMBED_DIM"
                             % (self.dim, served or self.model, dim))
        return health

    def _post(self, batch: List[str]) -> httpx.Response:
        """One batch, retried on transport errors and HTTP 429/502/503/504."""
        attempts = len(self.backoff) + 1
        for attempt in range(1, attempts + 1):
            try:
                r = self.http.post(self.url + ENDPOINT, json={"model": self.model, "input": batch})
                if r.status_code not in RETRY_STATUS:
                    return r
                why = "HTTP %d" % r.status_code
            except httpx.TransportError as e:
                if attempt == attempts:
                    raise
                why = type(e).__name__
            if attempt == attempts:
                return r
            wait = self.backoff[attempt - 1]
            print("warning: embedding batch of %d failed (%s), attempt %d of %d; retrying in %ss"
                  % (len(batch), why, attempt, attempts, wait), file=sys.stderr)
            time.sleep(wait)
        raise AssertionError("unreachable")

    def embed(self, texts: Sequence[str], kind: str = "doc") -> List[List[float]]:
        if kind not in ("query", "doc"):
            raise ValueError("kind must be 'query' or 'doc'")
        prefix = self.query_prefix if kind == "query" else self.doc_prefix
        out: List[List[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = [prefix + t for t in texts[i:i + self.batch_size]]
            t0 = time.perf_counter()
            r = self._post(batch)
            ms = (time.perf_counter() - t0) * 1000
            if r.status_code != 200:
                raise RuntimeError("embedding request failed (%d): %s" % (r.status_code, r.text[:500]))
            body = r.json()
            vecs = [d["embedding"] for d in sorted(body["data"], key=lambda d: d["index"])]
            if len(vecs) != len(batch):
                raise ValueError("embedding server returned %d vectors for %d inputs" % (len(vecs), len(batch)))
            bad = sorted({len(v) for v in vecs if len(v) != self.dim})
            if bad:
                raise ValueError("embedding has %s dims but EMBED_DIM=%d (model %s); fix EMBED_DIM or the model"
                                 % ("/".join(map(str, bad)), self.dim, body.get("model")))
            out.extend(vecs)
            if self.on_call:
                usage = body.get("usage") or {}
                timing = body.get("timing") or {}
                self.on_call({"model": body.get("model") or self.model, "endpoint": ENDPOINT,
                              "n": len(batch), "kind": kind, "latency_ms": int(ms),
                              "input_tokens": usage.get("prompt_tokens"), "server_ms": timing.get("ms"),
                              "device": timing.get("device"), "gpu": timing.get("gpu")})
        return out


_default: Optional[EmbedClient] = None


def embed(texts: Sequence[str], kind: str = "query") -> List[List[float]]:
    """Module-level convenience using env config. kind='query' adds EMBED_QUERY_PREFIX, 'doc' EMBED_DOC_PREFIX."""
    global _default
    if _default is None:
        _default = EmbedClient()
    return _default.embed(list(texts), kind=kind)


def to_pgvector(vec: Sequence[float]) -> str:
    return "[" + ",".join("%.7g" % x for x in vec) + "]"
