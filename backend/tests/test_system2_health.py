"""GET /health `system2.model_status`: is the model behind System 2 answering? (contract: "Model probe")."""
import httpx
import pytest

from .conftest import S2


def s2_health(**body):
    return httpx.Response(200, json={"status": "ok", "contract_version": "2026-10-10.backend.2", **body})


@pytest.mark.parametrize("body,status,model_status", [
    ({"mode": "live", "model": "m", "model_status": "up", "model_checked_s_ago": 3}, "up", "up"),
    ({"mode": "live", "model": "m", "model_status": "unknown", "model_checked_s_ago": None}, "up", "unknown"),
    ({"mode": "live", "model": "m", "model_status": "down", "model_error": "timeout"}, "degraded", "down"),
    ({"mode": "stub"}, "up", "stub"),
])
async def test_system2_model_status(make_harness, mock, body, status, model_status):
    h = make_harness(system2_url=S2)
    mock.get(S2 + "/health").mock(return_value=s2_health(**body))
    s2 = (await h.client.get("/health")).json()["system2"]
    assert s2["status"] == status and s2["model_status"] == model_status
    if model_status == "down":
        assert s2["error"] == "model: timeout"
    else:
        assert "error" not in s2


async def test_older_system2_without_model_status(make_harness, mock):
    h = make_harness(system2_url=S2)
    mock.get(S2 + "/health").mock(return_value=s2_health(mode="live", model="m"))
    s2 = (await h.client.get("/health")).json()["system2"]
    assert s2["status"] == "up" and "model_status" not in s2


async def test_system2_unreachable_stays_down(make_harness, mock):
    h = make_harness(system2_url=S2)
    mock.get(S2 + "/health").mock(side_effect=httpx.ConnectError("refused"))
    s2 = (await h.client.get("/health")).json()["system2"]
    assert s2["status"] == "down" and "model_status" not in s2
