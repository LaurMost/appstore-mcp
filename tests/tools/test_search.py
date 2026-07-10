from pathlib import Path

import httpx
import pytest

from appstore_mcp.errors import InvalidInputError
from appstore_mcp.tools.search import search_app_store
from tools import build_clients

FIXTURES = Path(__file__).parent.parent / "fixtures"


def search_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/search":
        return httpx.Response(
            200, content=(FIXTURES / "search_language_learning_us.json").read_bytes()
        )
    return httpx.Response(404)


async def test_search_returns_slim_results() -> None:
    clients = build_clients(search_handler)
    async with clients.http:
        result = await search_app_store(
            clients.itunes, "language learning", country="us", limit=10
        )
    assert result.query == "language learning"
    assert result.meta.country == "us"
    assert result.results
    assert result.results[0].app_id == "570060128"
    assert "entity=software" in result.sources[0].url


async def test_search_validates_country() -> None:
    clients = build_clients(search_handler)
    async with clients.http:
        with pytest.raises(InvalidInputError):
            await search_app_store(clients.itunes, "x", country="usa")


async def test_search_lowercases_country() -> None:
    clients = build_clients(search_handler)
    async with clients.http:
        result = await search_app_store(clients.itunes, "x", country="US")
    assert result.meta.country == "us"
