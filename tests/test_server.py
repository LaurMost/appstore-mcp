from pathlib import Path

import httpx
import pytest
from fastmcp import Client

from appstore_mcp.server import create_server

FIXTURES = Path(__file__).parent / "fixtures"


def apple_transport(page_status: int = 200) -> httpx.MockTransport:
    """Route requests to the recorded fixture matching the real Apple endpoint."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        if request.url.host == "apps.apple.com":
            if page_status != 200:
                return httpx.Response(page_status)
            return httpx.Response(
                200, content=(FIXTURES / "page_duolingo_us.html").read_bytes()
            )
        if path == "/search":
            return httpx.Response(
                200,
                content=(FIXTURES / "search_language_learning_us.json").read_bytes(),
            )
        if path == "/lookup":
            ids = params.get("id", "")
            if ids == "570060128":
                name = "lookup_duolingo_us.json"
            elif "," in ids:
                name = "lookup_multi_us.json"
            else:
                name = "lookup_notfound.json"
            return httpx.Response(200, content=(FIXTURES / name).read_bytes())
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def server_client() -> Client:
    http = httpx.AsyncClient(transport=apple_transport())
    return Client(create_server(http=http))


async def test_search_tool_returns_slim_results(server_client: Client) -> None:
    async with server_client as client:
        result = await client.call_tool(
            "search_app_store", {"query": "language learning", "limit": 10}
        )
    data = result.structured_content
    assert data["meta"]["country"] == "us"
    assert data["meta"]["store"] == "apple_app_store"
    first = data["results"][0]
    assert set(first) >= {"app_id", "name", "developer", "rating", "price"}
    # Slim shape: no description or screenshots in search results.
    assert "description" not in first
    source = data["sources"][0]
    assert source["name"] == "apple_itunes_api"
    # The reported URL must reproduce the actual request, entity param included.
    assert "entity=software" in source["url"]


async def test_get_app_returns_full_profile_without_raw(server_client: Client) -> None:
    async with server_client as client:
        result = await client.call_tool(
            "get_app_store_app", {"app_id_or_url": "570060128"}
        )
    data = result.structured_content
    app = data["app"]
    assert app["app_id"] == "570060128"
    assert len(app["description"]) > 500
    assert app["description_length"] == len(app["description"])
    assert data["raw"] is None


async def test_get_app_include_raw_returns_lookup_payload(
    server_client: Client,
) -> None:
    async with server_client as client:
        result = await client.call_tool(
            "get_app_store_app", {"app_id_or_url": "570060128", "include_raw": True}
        )
    raw = result.structured_content["raw"]
    assert raw["itunes_lookup"]["trackId"] == 570060128


async def test_get_app_uses_country_from_url(server_client: Client) -> None:
    async with server_client as client:
        result = await client.call_tool(
            "get_app_store_app",
            {"app_id_or_url": "https://apps.apple.com/de/app/x/id570060128"},
        )
    assert result.structured_content["meta"]["country"] == "de"


async def test_get_app_merges_page_enrichment_by_default(server_client: Client) -> None:
    async with server_client as client:
        result = await client.call_tool(
            "get_app_store_app", {"app_id_or_url": "570060128"}
        )
    data = result.structured_content
    app = data["app"]
    assert app["subtitle"] == "Languages, Math, Music & Chess"
    assert app["has_iap"] is True
    assert app["privacy"] is not None
    assert data["meta"]["warnings"] == []
    assert {s["name"] for s in data["sources"]} == {
        "apple_itunes_api",
        "apple_app_store_page",
    }


async def test_get_app_page_failure_degrades_with_warning() -> None:
    http = httpx.AsyncClient(transport=apple_transport(page_status=500))
    async with Client(create_server(http=http)) as client:
        result = await client.call_tool(
            "get_app_store_app", {"app_id_or_url": "570060128"}
        )
    data = result.structured_content
    assert data["app"]["app_id"] == "570060128"
    assert data["app"]["subtitle"] is None
    assert data["app"]["has_iap"] is None
    assert any("enrichment" in w for w in data["meta"]["warnings"])


async def test_get_app_can_skip_page_enrichment() -> None:
    hits: list[str] = []
    base = apple_transport()

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.host)
        return base.handler(request)  # type: ignore[attr-defined]

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with Client(create_server(http=http)) as client:
        await client.call_tool(
            "get_app_store_app",
            {"app_id_or_url": "570060128", "include_page_data": False},
        )
    assert "apps.apple.com" not in hits


async def test_get_app_not_found_is_agent_recoverable_error(
    server_client: Client,
) -> None:
    async with server_client as client:
        with pytest.raises(Exception, match="per-country"):
            await client.call_tool(
                "get_app_store_app", {"app_id_or_url": "999999999999"}
            )


async def test_tools_carry_required_annotations(server_client: Client) -> None:
    async with server_client as client:
        tools = await client.list_tools()
    assert tools, "server exposes tools"
    for tool in tools:
        assert tool.annotations is not None, tool.name
        assert tool.annotations.readOnlyHint is True, tool.name
        assert tool.annotations.openWorldHint is True, tool.name
        assert tool.annotations.title, tool.name
