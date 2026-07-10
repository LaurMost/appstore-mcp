"""Live structural-invariant smoke tests against real Apple endpoints.

Excluded by default (see pyproject addopts); run explicitly with
`uv run pytest -m live`. A weekly CI cron runs these to catch Apple
format drift before users do. Assertions check shape, not exact values.
"""

import pytest
from fastmcp import Client

from appstore_mcp.server import create_server

pytestmark = pytest.mark.live

DUOLINGO = "570060128"


@pytest.fixture
async def live_client():
    async with Client(create_server()) as client:
        yield client


async def test_live_search(live_client: Client) -> None:
    result = await live_client.call_tool(
        "search_app_store", {"query": "language learning", "limit": 5}
    )
    data = result.structured_content
    assert data["results"], "search returns results"
    assert all(r["app_id"].isdigit() for r in data["results"])


async def test_live_profile_with_page_enrichment(live_client: Client) -> None:
    result = await live_client.call_tool("get_app_store_app", {"app_id_or_url": DUOLINGO})
    data = result.structured_content
    app = data["app"]
    assert app["name"]
    assert len(app["description"]) > 100
    assert app["rating"] and 0 < app["rating"] <= 5
    # Page-parser drift detector: these are page-sourced fields.
    assert app["subtitle"], f"page enrichment broke: {data['meta']['warnings']}"
    assert app["has_iap"] is not None


async def test_live_compare(live_client: Client) -> None:
    result = await live_client.call_tool(
        "compare_app_store_apps",
        {"apps": [DUOLINGO, "829587759", "379968583"]},
    )
    data = result.structured_content
    assert len(data["apps"]) == 3
    assert data["errors"] == []


async def test_live_charts_overall(live_client: Client) -> None:
    result = await live_client.call_tool("get_app_store_charts", {"limit": 10})
    entries = result.structured_content["entries"]
    assert len(entries) == 10
    assert all(e["app_id"].isdigit() for e in entries)


async def test_live_charts_category(live_client: Client) -> None:
    result = await live_client.call_tool(
        "get_app_store_charts",
        {"chart": "top-grossing", "category": "finance", "country": "de", "limit": 10},
    )
    entries = result.structured_content["entries"]
    assert len(entries) == 10, "genre feed drift: no category entries"


async def test_live_reviews(live_client: Client) -> None:
    result = await live_client.call_tool(
        "get_app_store_reviews", {"app_id_or_url": DUOLINGO, "limit": 10}
    )
    data = result.structured_content
    assert len(data["reviews"]) == 10, f"review feed drift: {data['meta']['warnings']}"
    assert all(r["body"] for r in data["reviews"])
