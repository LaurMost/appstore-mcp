from pathlib import Path
from typing import Any

import httpx
import pytest

from appstore_mcp.apple.itunes import ITunesClient
from appstore_mcp.errors import RateLimitedError

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_transport(fixture_name: str, status_code: int = 200) -> httpx.MockTransport:
    body = (FIXTURES / fixture_name).read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=body)

    return httpx.MockTransport(handler)


def client_with(transport: httpx.MockTransport) -> ITunesClient:
    return ITunesClient(httpx.AsyncClient(transport=transport))


async def test_search_returns_parsed_results() -> None:
    client = client_with(fixture_transport("search_language_learning_us.json"))
    entry = await client.search("language learning", country="us", limit=10)
    assert entry.fresh is True
    assert entry.value["resultCount"] > 0
    assert any(r["trackId"] == 570060128 for r in entry.value["results"])


async def test_lookup_accepts_multiple_ids() -> None:
    client = client_with(fixture_transport("lookup_multi_us.json"))
    entry = await client.lookup(["570060128", "829587759", "379968583"], country="us")
    assert entry.value["resultCount"] == 3


async def test_repeat_lookup_is_served_from_cache() -> None:
    hits: list[Any] = []
    body = (FIXTURES / "lookup_duolingo_us.json").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url)
        return httpx.Response(200, content=body)

    client = client_with(httpx.MockTransport(handler))
    first = await client.lookup(["570060128"], country="us")
    second = await client.lookup(["570060128"], country="us")
    assert len(hits) == 1
    assert first.fresh is True and second.fresh is False


async def test_429_maps_to_rate_limited_error() -> None:
    client = client_with(fixture_transport("lookup_duolingo_us.json", status_code=429))
    with pytest.raises(RateLimitedError, match="retry"):
        await client.lookup(["570060128"], country="us")
