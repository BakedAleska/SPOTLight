"""The HTTP app Vercel serves (asgi.py): lifespan, auth, /healthz and an MCP round-trip, in-process, no database.

The ASGI lifespan is driven by hand, the way uvicorn and Vercel's Python runtime drive it; requests go through
BearerAuth to the SDK's session manager, which only works once that lifespan has started it.
"""
from __future__ import annotations

import asyncio
import importlib.util
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
import pytest

pytestmark = pytest.mark.anyio

ASGI_PY = Path(__file__).resolve().parent.parent / "asgi.py"
TOKEN = "test-token"
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def env(monkeypatch):
    # A DATABASE_URL nothing connects to: the pool opens lazily, and initialize/tools/list never touch it.
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    monkeypatch.setenv("MCP_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("MCP_LOG_CALLS", "0")
    return monkeypatch


def load_asgi():
    """A fresh import of asgi.py (env is read at import; a session manager can only be started once)."""
    spec = importlib.util.spec_from_file_location("vercel_asgi_under_test", ASGI_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.app


@asynccontextmanager
async def lifespan(app):
    to_app, from_app = asyncio.Queue(), asyncio.Queue()
    task = asyncio.create_task(app({"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}},
                                   to_app.get, from_app.put))
    await to_app.put({"type": "lifespan.startup"})
    assert (await from_app.get())["type"] == "lifespan.startup.complete"
    try:
        yield
    finally:
        await to_app.put({"type": "lifespan.shutdown"})
        assert (await from_app.get())["type"] == "lifespan.shutdown.complete"
        await task


@asynccontextmanager
async def client(app):
    # A non-loopback Host, as on Vercel: the SDK's DNS-rebinding guard must not answer 421.
    async with lifespan(app), httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                                 base_url="https://spot-mcp.vercel.app") as c:
        yield c


async def test_healthz_is_open(env):
    async with client(load_asgi()) as c:
        r = await c.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["server"] == "spot-context"


@pytest.mark.parametrize("auth", [None, "Bearer wrong", TOKEN, "bearer " + TOKEN])
async def test_mcp_rejects_missing_or_wrong_token(env, auth):
    hdrs = dict(HEADERS, **({"Authorization": auth} if auth else {}))
    async with client(load_asgi()) as c:
        r = await c.post("/mcp", json=INIT, headers=hdrs)
    assert r.status_code == 401 and r.json() == {"error": "unauthorized"}


async def test_initialize_and_tools_list_round_trip(env):
    hdrs = dict(HEADERS, Authorization="Bearer " + TOKEN)
    async with client(load_asgi()) as c:
        init = await c.post("/mcp", json=INIT, headers=hdrs)
        note = await c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=hdrs)
        tools = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=hdrs)
    assert init.status_code == 200 and init.headers["content-type"].startswith("application/json")
    assert init.json()["result"]["serverInfo"]["name"] == "spot-context"
    assert "mcp-session-id" not in init.headers                       # stateless: any instance answers any POST
    assert note.status_code == 202
    names = {t["name"] for t in tools.json()["result"]["tools"]}
    assert len(names) == 17 and {"get_flag_context", "search_graph"} <= names


def test_refuses_to_start_without_token(env):
    env.delenv("MCP_AUTH_TOKEN")
    with pytest.raises(RuntimeError, match="MCP_AUTH_TOKEN"):
        load_asgi()
