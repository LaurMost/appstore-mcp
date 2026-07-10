import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from appstore_mcp.apple.itunes import ITunesClient
from appstore_mcp.apple.page import AppPageClient
from appstore_mcp.apple.screenshots import fetch_screenshots
from appstore_mcp.cache import TTLCache
from appstore_mcp.errors import AppNotFoundError, AppStoreMCPError

FIXTURES = Path(__file__).parent.parent / "fixtures"

FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def make_handler(fail_images: str = "none"):
    """fail_images: 'none' | 'first' | 'all'."""
    all_results = json.loads((FIXTURES / "lookup_multi_us.json").read_text())["results"]
    seen = {"images": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.endswith("mzstatic.com"):
            seen["images"] += 1
            if fail_images == "all" or (
                fail_images == "first" and seen["images"] == 1
            ):
                return httpx.Response(500)
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

    return handler


def build(handler: Any) -> tuple[httpx.AsyncClient, ITunesClient, AppPageClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache: TTLCache[Any] = TTLCache()
    return http, ITunesClient(http, cache), AppPageClient(http, cache)


async def test_resolves_urls_from_lookup() -> None:
    http, itunes, app_page = build(make_handler())
    async with http:
        fetch = await fetch_screenshots(
            itunes, app_page, http, "829587759", country="us", device="iphone", limit=3
        )
    assert len(fetch.images) == 3
    assert len(fetch.urls) == 3
    assert fetch.warnings == []
    assert all(img.format in {"jpeg", "png"} for img in fetch.images)


async def test_page_template_fallback_when_lookup_empty() -> None:
    # Duolingo's lookup carries no screenshotUrls; the page templates rescue it.
    http, itunes, app_page = build(make_handler())
    async with http:
        fetch = await fetch_screenshots(
            itunes, app_page, http, "570060128", country="us", device="iphone", limit=2
        )
    assert len(fetch.images) == 2
    assert any("public App Store page" in w for w in fetch.warnings)


async def test_partial_download_failure_warns_but_succeeds() -> None:
    http, itunes, app_page = build(make_handler(fail_images="first"))
    async with http:
        fetch = await fetch_screenshots(
            itunes, app_page, http, "829587759", country="us", device="iphone", limit=3
        )
    assert len(fetch.images) == 2
    assert len(fetch.urls) == 2
    assert any("failed to fetch" in w for w in fetch.warnings)


async def test_all_downloads_failing_raises() -> None:
    http, itunes, app_page = build(make_handler(fail_images="all"))
    async with http:
        with pytest.raises(AppStoreMCPError, match="downloads failed"):
            await fetch_screenshots(
                itunes,
                app_page,
                http,
                "829587759",
                country="us",
                device="iphone",
                limit=3,
            )


async def test_unknown_app_raises_not_found() -> None:
    http, itunes, app_page = build(make_handler())
    async with http:
        with pytest.raises(AppNotFoundError):
            await fetch_screenshots(
                itunes,
                app_page,
                http,
                "111111111",
                country="us",
                device="iphone",
                limit=3,
            )


async def test_on_download_callback_fires_per_url() -> None:
    http, itunes, app_page = build(make_handler())
    ticks = {"n": 0}

    async def on_download() -> None:
        ticks["n"] += 1

    async with http:
        fetch = await fetch_screenshots(
            itunes,
            app_page,
            http,
            "829587759",
            country="us",
            device="iphone",
            limit=3,
            on_download=on_download,
        )
    assert ticks["n"] == len(fetch.urls)
