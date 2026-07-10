from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from appstore_mcp.errors import AppNotFoundError
from appstore_mcp.tools.app import get_app_store_app
from tools import FakeWarner, build_clients

FIXTURES = Path(__file__).parent.parent / "fixtures"


def app_handler(page_status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "apps.apple.com":
            if page_status != 200:
                return httpx.Response(page_status)
            return httpx.Response(
                200, content=(FIXTURES / "page_duolingo_us.html").read_bytes()
            )
        if request.url.path == "/lookup":
            ids = dict(request.url.params).get("id", "")
            name = (
                "lookup_duolingo_us.json"
                if ids == "570060128"
                else "lookup_notfound.json"
            )
            return httpx.Response(200, content=(FIXTURES / name).read_bytes())
        return httpx.Response(404)

    return handler


async def test_app_merges_page_enrichment() -> None:
    clients = build_clients(app_handler())
    async with clients.http:
        result = await get_app_store_app(
            clients.itunes, clients.app_page, "570060128"
        )
    assert result.app.app_id == "570060128"
    assert result.app.subtitle == "Languages, Math, Music & Chess"
    assert result.app.has_iap is True
    assert result.meta.warnings == []
    assert {s.name for s in result.sources} == {
        "apple_itunes_api",
        "apple_app_store_page",
    }


async def test_app_page_failure_degrades_and_warns() -> None:
    clients = build_clients(app_handler(page_status=500))
    warner = FakeWarner()
    async with clients.http:
        result = await get_app_store_app(
            clients.itunes, clients.app_page, "570060128", warner=warner
        )
    # In-band degradation: profile still returned, page fields null.
    assert result.app.subtitle is None
    assert result.app.has_iap is None
    assert any("enrichment" in w for w in result.meta.warnings)
    # Side-channel: the Warner callback fired with structured context.
    assert len(warner.calls) == 1
    message, extra = warner.calls[0]
    assert "570060128" in message
    assert extra is not None
    assert extra["app_id"] == "570060128"
    assert extra["error_type"]


async def test_app_page_failure_without_warner_still_degrades() -> None:
    clients = build_clients(app_handler(page_status=500))
    async with clients.http:
        result = await get_app_store_app(
            clients.itunes, clients.app_page, "570060128", warner=None
        )
    assert any("enrichment" in w for w in result.meta.warnings)
    assert result.app.subtitle is None


async def test_app_can_skip_page_enrichment() -> None:
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.host)
        return app_handler()(request)

    clients = build_clients(handler)
    async with clients.http:
        result = await get_app_store_app(
            clients.itunes,
            clients.app_page,
            "570060128",
            include_page_data=False,
        )
    assert "apps.apple.com" not in hits
    assert result.app.subtitle is None


async def test_app_not_found_raises() -> None:
    clients = build_clients(app_handler())
    async with clients.http:
        with pytest.raises(AppNotFoundError):
            await get_app_store_app(clients.itunes, clients.app_page, "999999999999")
