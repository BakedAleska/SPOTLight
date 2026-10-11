"""Vercel entrypoint (project Root Directory `amd/mcp`): the HTTP app `python -m spot_mcp --http` serves.

Vercel's Python runtime finds this file by name, serves its top-level `app` and runs the ASGI lifespan, which starts
the MCP session manager. Config is env vars only (README "Deploy on Vercel"). The URL is public, so MCP_AUTH_TOKEN
is required here; the database connection opens lazily on the first tool call.

On serverless (SERVERLESS=1, or VERCEL set by the runtime) every HTTP request waits up to SERVERLESS_LOG_DRAIN_S
for its app.mcp_tool_calls / app.model_calls inserts before the response completes: they are fire-and-forget, and a
frozen instance would lose them.
"""
import logging
import os

from spot_mcp.server import create_app, service_from_env

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("spot_mcp.asgi")

SERVERLESS_LOG_DRAIN_S = 2.0


def _truthy(v):
    return (v or "").strip().lower() not in ("", "0", "false", "no", "off")


def serverless_from_env() -> bool:
    """SERVERLESS=1/0 wins; unset -> on when Vercel's runtime env (VERCEL) is present (same rule as the backend)."""
    v = os.environ.get("SERVERLESS")
    return _truthy(v) if v not in (None, "") else _truthy(os.environ.get("VERCEL"))


class DrainLogs:
    """ASGI wrapper: each HTTP request awaits the log sink's pending inserts (at most `timeout_s`) just BEFORE its
    final response chunk goes out, like the backend's middleware: the instance may freeze as soon as the response
    is complete, so draining after it would not be reliable. The tool call has finished by then (its row is already
    spawned). A request that ends without a final chunk (an error) drains on the way out. Lifespan and other scope
    types pass straight through."""

    def __init__(self, app, sink, timeout_s: float = SERVERLESS_LOG_DRAIN_S):
        self.app, self.sink, self.timeout_s = app, sink, timeout_s

    async def _drain(self) -> None:
        drain = getattr(self.sink, "drain", None)
        if drain is not None:
            try:
                await drain(self.timeout_s)
            except Exception as exc:  # noqa: BLE001 - logging must never break a response
                log.warning("log drain failed: %s", exc)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        drained = False

        async def send_after_drain(message):
            nonlocal drained
            if message.get("type") == "http.response.body" and not message.get("more_body", False) and not drained:
                drained = True
                await self._drain()
            await send(message)

        try:
            await self.app(scope, receive, send_after_drain)
        finally:
            if not drained:
                await self._drain()


_token = os.environ.get("MCP_AUTH_TOKEN")
if not _token:
    raise RuntimeError("MCP_AUTH_TOKEN is not set: refusing to serve the database on a public URL")

_service = service_from_env()
app = create_app(_service, host="0.0.0.0", token=_token)
if serverless_from_env():
    app = DrainLogs(app, _service.sink)
