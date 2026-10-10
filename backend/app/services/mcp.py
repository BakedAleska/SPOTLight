"""A minimal MCP client over the streamable HTTP transport (JSON-RPC 2.0), enough to call one tool.

Per call: `initialize` -> `notifications/initialized` -> `tools/call` -> DELETE the session. Responses may be
plain JSON or an SSE stream (`data: {...}` lines); both are handled. Kept dependency-free (httpx only) so
the backend image stays small. The backend only calls `get_flag_context` (Tier 1, contract §5).
"""
from __future__ import annotations

import itertools
import json
import time
from typing import Any, Dict, Optional

import httpx

from .http import Breaker, ServiceUnavailable, short_error

PROTOCOL_VERSION = "2025-06-18"
_ids = itertools.count(1)


def _parse_body(r: httpx.Response, want_id: int) -> Dict[str, Any]:
    ctype = r.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        for line in r.text.splitlines():
            if line.startswith("data:"):
                try:
                    msg = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("id") == want_id:
                    return msg
        raise ServiceUnavailable("mcp", "no JSON-RPC response for id %d in SSE stream" % want_id)
    return r.json()


class MCPClient:
    name = "mcp"

    def __init__(self, url: str, http: httpx.AsyncClient, timeout_s: float = 15.0, breaker: Optional[Breaker] = None,
                 auth_token: Optional[str] = None):
        self.url = url
        self.auth_token = auth_token
        self.http = http
        self.timeout_s = timeout_s
        self.breaker = breaker or Breaker()

    def _session_headers(self, session: Optional[str], request_id: Optional[str]) -> Dict[str, str]:
        """Auth + session headers, shared by every POST and the closing DELETE (the server checks auth on both)."""
        headers = {"X-Request-ID": request_id or ""}
        if self.auth_token:
            headers["Authorization"] = "Bearer " + self.auth_token
        if session:
            headers["Mcp-Session-Id"] = session
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
        return headers

    async def _rpc(self, method: str, params: Dict[str, Any], session: Optional[str],
                   request_id: Optional[str], notify: bool = False) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                   **self._session_headers(session, request_id)}
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        rid = None
        if not notify:
            rid = next(_ids)
            msg["id"] = rid
        r = await self.http.post(self.url, json=msg, headers=headers, timeout=self.timeout_s)
        if r.status_code >= 400:
            raise ServiceUnavailable(self.name, "%s -> HTTP %d: %s" % (method, r.status_code, r.text[:200]))
        new_session = r.headers.get("mcp-session-id") or session
        if notify:
            return None, new_session
        body = _parse_body(r, rid)  # type: ignore[arg-type]
        if "error" in body:
            raise ServiceUnavailable(self.name, "%s error: %s" % (method, body["error"]))
        return body.get("result") or {}, new_session

    async def call_tool(self, tool: str, arguments: Dict[str, Any], request_id: Optional[str] = None) -> Dict[str, Any]:
        """Call one tool; returns its structured result (structuredContent, or the JSON text content)."""
        if not self.url:
            raise ServiceUnavailable(self.name, "MCP_URL is not set")
        if not self.breaker.allow():
            raise ServiceUnavailable(self.name, "circuit open after: %s" % self.breaker.last_error)
        t0 = time.perf_counter()
        session: Optional[str] = None
        try:
            _, session = await self._rpc("initialize", {
                "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                "clientInfo": {"name": "spot-backend", "version": "1.0"}}, None, request_id)
            await self._rpc("notifications/initialized", {}, session, request_id, notify=True)
            result, _ = await self._rpc("tools/call", {"name": tool, "arguments": arguments,
                                                       "_meta": {"request_id": request_id}}, session, request_id)
        except ServiceUnavailable as exc:
            self.breaker.failure(exc.reason)
            raise
        except (httpx.HTTPError, ValueError) as exc:
            err = short_error(exc)
            self.breaker.failure(err)
            raise ServiceUnavailable(self.name, err) from exc
        finally:
            if session:
                try:
                    await self.http.delete(self.url, headers=self._session_headers(session, request_id), timeout=2.0)
                except httpx.HTTPError:
                    pass
        self.breaker.success()
        result = result or {}
        if result.get("isError"):
            texts = [c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict)]
            raise ServiceUnavailable(self.name, "tool %s failed: %s" % (tool, " ".join(texts)[:300]))
        data = result.get("structuredContent")
        if isinstance(data, dict) and set(data) == {"result"} and isinstance(data["result"], (dict, list)):
            data = data["result"]
        if data is None:
            for c in result.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "text":
                    try:
                        data = json.loads(c["text"])
                        break
                    except ValueError:
                        data = {"text": c["text"]}
        if not isinstance(data, dict):
            data = {"result": data}
        data.setdefault("_latency_ms", int((time.perf_counter() - t0) * 1000))
        return data

    async def health(self) -> dict:
        """GET <origin>/healthz (the MCP server's open health route)."""
        if not self.url:
            return {"status": "unset"}
        u = httpx.URL(self.url)
        try:
            r = await self.http.get(str(u.copy_with(path="/healthz", query=None)), timeout=2.0)
            return {"status": "up" if r.status_code == 200 else "degraded", "http": r.status_code}
        except httpx.HTTPError as exc:
            return {"status": "down", "error": short_error(exc)}
