"""Audit H3: admin endpoints need X-Admin-Token; with ADMIN_TOKEN unset they are closed (503); CORS is restricted."""
import httpx
import pytest

from .conftest import ADMIN_TOKEN, laya_answers, replay_event, system1_route

ADMIN_ROUTES = [
    ("POST", "/api/v1/events", {"json": None}),            # body filled in per test (a real replay event)
    ("POST", "/api/v1/events/batch", {"json": {"events": []}}),
    ("POST", "/api/v1/replay/start", {"json": {"speed": 0, "limit": 1}}),
    ("POST", "/api/v1/replay/stop", {}),
    ("POST", "/api/v1/replay/step", {"json": {}}),
    ("GET", "/api/v1/replay/status", {}),
    ("POST", "/api/v1/ops/reset-demo", {"json": {"confirm": True}}),
    ("GET", "/api/v1/ops/model-calls", {}),
    ("GET", "/api/v1/ops/jobs", {}),
]


def _client(h, headers=None):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url="http://backend", headers=headers)


def _kw(kw):
    if "json" in kw and kw["json"] is None:
        return {"json": replay_event("E00019")}
    return kw


@pytest.mark.parametrize("method,url,kw", ADMIN_ROUTES)
@pytest.mark.parametrize("headers", [None, {"X-Admin-Token": "wrong"}, {"X-Admin-Token": ""}])
async def test_admin_routes_reject_missing_or_wrong_token(make_harness, method, url, kw, headers):
    h = make_harness()
    async with _client(h, headers) as c:
        r = await c.request(method, url, **_kw(kw))
    assert r.status_code == 401, (url, r.text)
    assert r.json()["error"] == "unauthorized"
    assert not h.repo.events and not h.repo.flags and h.sink.jobs == []      # nothing ran


@pytest.mark.parametrize("method,url,kw", ADMIN_ROUTES)
async def test_admin_routes_closed_when_admin_token_unset(make_harness, method, url, kw):
    h = make_harness(admin_token=None)
    async with _client(h, {"X-Admin-Token": "anything"}) as c:     # no bypass, whatever the header says
        r = await c.request(method, url, **_kw(kw))
    assert r.status_code == 503, (url, r.text)
    assert r.json()["error"] == "admin_token_unset"


async def test_admin_route_accepts_the_token(make_harness, mock):
    h = make_harness()
    system1_route(mock, lambda it: laya_answers(it["kind"], event=it["event"]))
    async with _client(h, {"X-Admin-Token": ADMIN_TOKEN}) as c:
        r = await c.post("/api/v1/events", json=replay_event("E00019"))
        assert r.status_code == 200 and r.json()["flag"]["flag_id"]
        assert (await c.post("/api/v1/ops/reset-demo", json={})).status_code == 400   # past auth, to the confirm check
        assert (await c.get("/api/v1/replay/status")).status_code == 200


async def test_read_and_operator_routes_stay_open(make_harness, mock):
    """The dashboard's read paths and the operator's ack/explain do not need the admin token."""
    h = make_harness()
    system1_route(mock, lambda it: laya_answers(it["kind"], event=it["event"]))
    fid = (await h.client.post("/api/v1/events", json=replay_event("E00019"))).json()["flag"]["flag_id"]
    mock.route(host="mcp.test").mock(side_effect=httpx.ConnectError("down"))
    mock.route(host="embed.test").mock(side_effect=httpx.ConnectError("down"))
    async with _client(h) as c:
        for url in ("/health", "/api/v1/events?limit=5", "/api/v1/flags", "/api/v1/flags/%d" % fid):
            assert (await c.get(url)).status_code == 200, url
        assert (await c.post("/api/v1/flags/%d/ack" % fid, json={"action": "ACKNOWLEDGE"})).status_code == 200
        assert (await c.post("/api/v1/flags/%d/explain" % fid)).status_code == 200


@pytest.mark.parametrize("origin,allowed", [("http://localhost:3000", True), ("http://evil.example", False)])
async def test_cors_only_allows_configured_origins(make_harness, origin, allowed):
    h = make_harness()                                  # Settings default: CORS_ORIGINS=http://localhost:3000
    async with _client(h) as c:
        r = await c.options("/api/v1/ops/reset-demo", headers={
            "Origin": origin, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-admin-token,content-type"})
    assert (r.headers.get("access-control-allow-origin") == origin) is allowed
    if not allowed:
        assert r.status_code == 400


def test_cors_default_from_env(monkeypatch):
    from app.config import Settings
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    s = Settings.from_env()
    assert s.cors_origins == ["http://localhost:3000"] and s.admin_token is None
    monkeypatch.setenv("CORS_ORIGINS", "http://a:3000, https://b.example")
    monkeypatch.setenv("ADMIN_TOKEN", "t0k")
    s = Settings.from_env()
    assert s.cors_origins == ["http://a:3000", "https://b.example"] and s.admin_token == "t0k"
