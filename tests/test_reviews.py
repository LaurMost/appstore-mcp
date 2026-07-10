import json
from pathlib import Path
from typing import Any, Callable

import httpx
from fastmcp import Client

from appstore_mcp.apple.normalize import review_from_feed_entry
from appstore_mcp.apple.page import reviews_from_html
from appstore_mcp.server import create_server

FIXTURES = Path(__file__).parent / "fixtures"


def test_review_from_real_feed_entry(load_fixture: Callable[[str], Any]) -> None:
    entry = load_fixture("reviews_duolingo_us_p1.json")["feed"]["entry"][0]
    review = review_from_feed_entry(entry)
    assert review.review_id == "14249584399"
    assert review.rating == 5
    assert review.title
    assert len(review.body) > 10
    assert review.author == "ariacreates✨✨"
    assert review.app_version == "7.129.0"
    assert review.updated_at is not None


def test_fallback_reviews_from_real_page(load_fixture: Callable[[str], Any]) -> None:
    reviews = reviews_from_html(load_fixture("page_duolingo_us.html"))
    assert len(reviews) >= 5
    assert all(r.body for r in reviews)


def reviews_transport(empty_feed: bool = False) -> tuple[httpx.MockTransport, list[str]]:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if "/rss/customerreviews/" in request.url.path:
            if empty_feed:
                return httpx.Response(200, content=json.dumps({"feed": {}}).encode())
            return httpx.Response(
                200, content=(FIXTURES / "reviews_duolingo_us_p1.json").read_bytes()
            )
        if request.url.host == "apps.apple.com":
            return httpx.Response(
                200, content=(FIXTURES / "page_duolingo_us.html").read_bytes()
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler), paths


async def test_reviews_tool_respects_limit() -> None:
    transport, _ = reviews_transport()
    async with Client(create_server(http=httpx.AsyncClient(transport=transport))) as client:
        result = await client.call_tool(
            "get_app_store_reviews", {"app_id_or_url": "570060128", "limit": 10}
        )
    data = result.structured_content
    assert data["app_id"] == "570060128"
    assert len(data["reviews"]) == 10
    assert data["reviews"][0]["rating"] in {1, 2, 3, 4, 5}


async def test_reviews_tool_paginates_beyond_one_feed_page() -> None:
    transport, paths = reviews_transport()
    async with Client(create_server(http=httpx.AsyncClient(transport=transport))) as client:
        result = await client.call_tool(
            "get_app_store_reviews", {"app_id_or_url": "570060128", "limit": 60}
        )
    assert len(result.structured_content["reviews"]) == 60
    feed_pages = [p for p in paths if "/rss/customerreviews/" in p]
    assert len(feed_pages) == 2


async def test_reviews_tool_falls_back_to_page_reviews_when_feed_empty() -> None:
    transport, paths = reviews_transport(empty_feed=True)
    async with Client(create_server(http=httpx.AsyncClient(transport=transport))) as client:
        result = await client.call_tool(
            "get_app_store_reviews", {"app_id_or_url": "570060128"}
        )
    data = result.structured_content
    assert len(data["reviews"]) >= 5
    assert any("fallback" in w or "feed" in w for w in data["meta"]["warnings"])
    assert any(p for p in paths if "570060128" in p and "customerreviews" not in p)
    assert data["sources"][-1]["name"] == "apple_app_store_page"
