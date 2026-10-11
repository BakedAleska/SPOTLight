"""EmbedClient retries once on a dropped connection or a 429/502/503/504 (RE-04): one tunnel hiccup must not skip
retrieval. A timeout or another 4xx/5xx is not retried; the breaker opens only on the final failure."""
import httpx
import pytest

from app.services.embed import EmbedClient
from app.services.http import ServiceUnavailable

from .conftest import EMBED
from .fakes import RecordingSink


def _client(first):
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1 or first == "always503":
            if first == "disconnect":
                raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=req)
            if first == "timeout":
                raise httpx.ReadTimeout("slow", request=req)
            return httpx.Response(503 if first == "always503" else first, text="busy")
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1] * 4}]})
    sink = RecordingSink()
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return EmbedClient(EMBED, "m", 4, "Query:", "", http, sink, retry_delay_s=0.01), sink, calls


@pytest.mark.parametrize("first", ["disconnect", 503, 502, 429])
async def test_embed_query_retries_once_and_keeps_the_breaker_closed(first):
    c, sink, calls = _client(first)
    assert len(await c.embed_query("q")) == 4
    assert len(calls) == 2 and [m["ok"] for m in sink.model_calls] == [False, True]
    assert c.breaker.state == "closed"


@pytest.mark.parametrize("first", ["timeout", 400, 500])
async def test_embed_query_does_not_retry_a_timeout_or_other_errors(first):
    c, sink, calls = _client(first)
    with pytest.raises(ServiceUnavailable):
        await c.embed_query("q")
    assert len(calls) == 1 and [m["ok"] for m in sink.model_calls] == [False]
    assert c.breaker.state == "open"


async def test_embed_query_gives_up_after_one_retry_and_opens_the_breaker():
    c, sink, calls = _client("always503")
    with pytest.raises(ServiceUnavailable) as exc:
        await c.embed_query("q")
    assert "HTTP 503" in exc.value.reason and type(exc.value) is ServiceUnavailable
    assert len(calls) == 2 and [m["ok"] for m in sink.model_calls] == [False, False]
    assert c.breaker.state == "open"
