import json
from pathlib import Path
from typing import Any, Callable

import httpx
from fastmcp import Client
from mcp.types import ImageContent

from appstore_mcp.apple.page import screenshot_urls_from_html
from appstore_mcp.server import create_server

FIXTURES = Path(__file__).parent / "fixtures"

FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def test_screenshot_urls_rendered_from_page_templates(
    load_fixture: Callable[[str], Any],
) -> None:
    html = load_fixture("page_duolingo_us.html")
    urls = screenshot_urls_from_html(html, "iphone")
    assert len(urls) == 10
    assert all(u.startswith("https://") for u in urls)
    # Templates rendered at capped width with height preserving aspect ratio.
    assert "/400x" in urls[0] and urls[0].endswith("bb.jpg")
    assert "{w}" not in urls[0]


def apple_transport() -> httpx.MockTransport:
    all_results = json.loads((FIXTURES / "lookup_multi_us.json").read_text())["results"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.endswith("mzstatic.com"):
            return httpx.Response(
                200, content=FAKE_JPEG, headers={"content-type": "image/jpeg"}
            )
        if request.url.host == "apps.apple.com":
            return httpx.Response(
                200, content=(FIXTURES / "page_duolingo_us.html").read_bytes()
            )
        if request.url.path == "/lookup":
            requested = set(dict(request.url.params).get("id", "").split(","))
            results = [r for r in all_results if str(r["trackId"]) in requested]
            return httpx.Response(
                200,
                content=json.dumps(
                    {"resultCount": len(results), "results": results}
                ).encode(),
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def test_screenshots_returned_as_image_content() -> None:
    # Babbel has real screenshotUrls in the recorded lookup fixture.
    http = httpx.AsyncClient(transport=apple_transport())
    async with Client(create_server(http=http)) as client:
        result = await client.call_tool(
            "get_app_store_screenshots",
            {"app_id_or_url": "829587759", "limit": 3},
        )
    images = [b for b in result.content if isinstance(b, ImageContent)]
    assert len(images) == 3
    assert images[0].mimeType == "image/jpeg"
    data = result.structured_content
    assert data["count"] == 3
    assert data["device"] == "iphone"
    assert len(data["urls"]) == 3


async def test_screenshots_fall_back_to_page_templates() -> None:
    # Duolingo's lookup has zero screenshot URLs; the page templates rescue it.
    http = httpx.AsyncClient(transport=apple_transport())
    async with Client(create_server(http=http)) as client:
        result = await client.call_tool(
            "get_app_store_screenshots",
            {"app_id_or_url": "570060128", "limit": 2},
        )
    images = [b for b in result.content if isinstance(b, ImageContent)]
    assert len(images) == 2
    warnings = result.structured_content["warnings"]
    assert any("public App Store page" in w for w in warnings)
