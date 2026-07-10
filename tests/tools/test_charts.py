import json
from pathlib import Path

import httpx

from appstore_mcp.tools.charts import get_app_store_charts
from tools import build_clients

FIXTURES = Path(__file__).parent.parent / "fixtures"


def charts_handler(request: httpx.Request) -> httpx.Response:
    assert "/rss/topfreeapplications/" in request.url.path
    name = (
        "charts_topfree_education_us.json"
        if "genre=6017" in request.url.path
        else "charts_topfree_us.json"
    )
    return httpx.Response(200, content=(FIXTURES / name).read_bytes())


async def test_charts_returns_ranked_entries_with_category() -> None:
    clients = build_clients(charts_handler)
    async with clients.http:
        result = await get_app_store_charts(
            clients.charts, chart="top-free", category="education", limit=10
        )
    assert result.chart == "top-free"
    assert result.category == "education"
    ranks = [e.rank for e in result.entries]
    assert ranks == list(range(1, len(ranks) + 1))
    assert result.entries[0].app_id == "570060128"
    assert any("undocumented" in w for w in result.meta.warnings)


async def test_charts_empty_feed_adds_warning() -> None:
    def empty_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps({"feed": {}}).encode())

    clients = build_clients(empty_handler)
    async with clients.http:
        result = await get_app_store_charts(clients.charts, chart="top-free")
    assert result.entries == []
    assert any("no entries" in w for w in result.meta.warnings)
