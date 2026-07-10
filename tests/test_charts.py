from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client

from appstore_mcp.apple.charts import chart_url, resolve_genre_id
from appstore_mcp.apple.normalize import chart_entry_from_feed
from appstore_mcp.errors import InvalidInputError
from appstore_mcp.server import create_server

FIXTURES = Path(__file__).parent / "fixtures"


def test_chart_entry_from_real_feed(load_fixture: Callable[[str], Any]) -> None:
    feed = load_fixture("charts_topfree_education_us.json")["feed"]
    entry = chart_entry_from_feed(feed["entry"][0], rank=1)
    assert entry.rank == 1
    assert entry.app_id == "570060128"
    assert entry.name.startswith("Duolingo")
    assert entry.bundle_id == "com.duolingo.DuolingoMobile"
    assert entry.developer == "Duolingo"
    assert entry.icon_url
    assert entry.icon_url.startswith("https://")


def test_chart_url_shapes() -> None:
    assert (
        chart_url("us", "top-free", limit=10, genre_id=None)
        == "https://itunes.apple.com/us/rss/topfreeapplications/limit=10/json"
    )
    assert (
        chart_url("de", "top-grossing", limit=50, genre_id="6015")
        == "https://itunes.apple.com/de/rss/topgrossingapplications/limit=50/genre=6015/json"
    )


def test_resolve_genre_accepts_names_and_ids() -> None:
    assert resolve_genre_id("finance") == "6015"
    assert resolve_genre_id("Health & Fitness") == "6013"
    assert resolve_genre_id("6017") == "6017"
    with pytest.raises(InvalidInputError, match="finance"):
        resolve_genre_id("banking")


def charts_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/rss/topfreeapplications/" in request.url.path
        name = (
            "charts_topfree_education_us.json"
            if "genre=6017" in request.url.path
            else "charts_topfree_us.json"
        )
        return httpx.Response(200, content=(FIXTURES / name).read_bytes())

    return httpx.MockTransport(handler)


async def test_charts_tool_returns_ranked_entries_with_category() -> None:
    http = httpx.AsyncClient(transport=charts_transport())
    async with Client(create_server(http=http)) as client:
        result = await client.call_tool(
            "get_app_store_charts",
            {"chart": "top-free", "category": "education", "limit": 10},
        )
    data = result.structured_content
    assert data["chart"] == "top-free"
    assert data["category"] == "education"
    ranks = [e["rank"] for e in data["entries"]]
    assert ranks == list(range(1, len(ranks) + 1))
    assert data["entries"][0]["app_id"] == "570060128"
    assert any("undocumented" in w for w in data["meta"]["warnings"])
