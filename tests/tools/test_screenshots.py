import json
from pathlib import Path

import httpx

from appstore_mcp.tools.screenshots import get_app_store_screenshots
from tools import FakeProgressReporter, build_clients

FIXTURES = Path(__file__).parent.parent / "fixtures"

FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def screenshots_handler(request: httpx.Request) -> httpx.Response:
    all_results = json.loads((FIXTURES / "lookup_multi_us.json").read_text())["results"]
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
            content=json.dumps({"resultCount": len(results), "results": results}).encode(),
        )
    return httpx.Response(404)


async def test_screenshots_from_lookup_urls() -> None:
    # Babbel has real screenshotUrls in the recorded lookup fixture.
    clients = build_clients(screenshots_handler)
    progress = FakeProgressReporter()
    async with clients.http:
        result = await get_app_store_screenshots(
            clients.itunes,
            clients.app_page,
            clients.http,
            "829587759",
            limit=3,
            progress=progress,
        )
    assert result.app_id == "829587759"
    assert result.device == "iphone"
    assert len(result.images) == 3
    assert len(result.urls) == 3
    # Coarse progress: one 1.0 report after every download completes, not one
    # tick per download.
    assert [round(f, 3) for f, _ in progress.calls] == [1.0]


async def test_screenshots_fall_back_to_page_templates() -> None:
    # Duolingo's lookup has zero screenshot URLs; the page templates rescue it.
    clients = build_clients(screenshots_handler)
    async with clients.http:
        result = await get_app_store_screenshots(
            clients.itunes,
            clients.app_page,
            clients.http,
            "570060128",
            limit=2,
        )
    assert len(result.images) == 2
    assert any("public App Store page" in w for w in result.warnings)


async def test_screenshots_without_progress_is_ok() -> None:
    clients = build_clients(screenshots_handler)
    async with clients.http:
        result = await get_app_store_screenshots(
            clients.itunes,
            clients.app_page,
            clients.http,
            "829587759",
            limit=1,
            progress=None,
        )
    assert len(result.images) == 1
