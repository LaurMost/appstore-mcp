import json
from pathlib import Path

import httpx
import pytest
from fastmcp import Client

from appstore_mcp.server import create_server

FIXTURES = Path(__file__).parent / "fixtures"

KNOWN_IDS = {"570060128", "829587759", "379968583"}


def lookup_transport(hits: list[str] | None = None) -> httpx.MockTransport:
    """Serve the multi-lookup fixture filtered to whichever IDs were requested."""
    all_results = json.loads((FIXTURES / "lookup_multi_us.json").read_text())["results"]

    def handler(request: httpx.Request) -> httpx.Response:
        if hits is not None:
            hits.append(str(request.url))
        requested = set(dict(request.url.params).get("id", "").split(","))
        results = [r for r in all_results if str(r["trackId"]) in requested]
        body = {"resultCount": len(results), "results": results}
        return httpx.Response(200, content=json.dumps(body).encode())

    return httpx.MockTransport(handler)


async def test_compare_returns_profiles_side_by_side_in_one_call() -> None:
    hits: list[str] = []
    client = Client(
        create_server(http=httpx.AsyncClient(transport=lookup_transport(hits)))
    )
    async with client:
        result = await client.call_tool(
            "compare_app_store_apps",
            {
                "apps": [
                    "570060128",
                    "https://apps.apple.com/us/app/babbel/id829587759",
                    "379968583",
                ]
            },
        )
    data = result.structured_content
    assert [a["app_id"] for a in data["apps"]] == [
        "570060128",
        "829587759",
        "379968583",
    ]
    assert data["errors"] == []
    for app in data["apps"]:
        assert app["description_length"] > 0
        assert "screenshot_count" in app
    assert len(hits) == 1, "batch compare must use a single multi-ID lookup call"


async def test_compare_reports_partial_failures_in_band() -> None:
    client = Client(create_server(http=httpx.AsyncClient(transport=lookup_transport())))
    async with client:
        result = await client.call_tool(
            "compare_app_store_apps",
            {"apps": ["570060128", "111111111", "not-a-valid-ref"]},
        )
    data = result.structured_content
    assert [a["app_id"] for a in data["apps"]] == ["570060128"]
    reasons = {e["app"]: e["reason"] for e in data["errors"]}
    assert "111111111" in reasons
    assert "not found" in reasons["111111111"]
    assert "not-a-valid-ref" in reasons
    assert data["meta"]["warnings"], "partial failure surfaces a warning"


async def test_compare_not_found_error_reports_the_value_the_caller_sent() -> None:
    client = Client(create_server(http=httpx.AsyncClient(transport=lookup_transport())))
    missing_url = "https://apps.apple.com/us/app/gone/id111111111"
    async with client:
        result = await client.call_tool(
            "compare_app_store_apps", {"apps": ["570060128", missing_url]}
        )
    errors = result.structured_content["errors"]
    assert errors[0]["app"] == missing_url


async def test_compare_raises_only_when_every_app_fails() -> None:
    client = Client(create_server(http=httpx.AsyncClient(transport=lookup_transport())))
    async with client:
        with pytest.raises(Exception, match=r"[Nn]one of"):
            await client.call_tool(
                "compare_app_store_apps", {"apps": ["111111111", "222222222"]}
            )
