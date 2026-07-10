import json
from pathlib import Path

import httpx
import pytest

from appstore_mcp.errors import AppStoreMCPError, InvalidInputError
from appstore_mcp.tools.compare import compare_app_store_apps
from tools import build_clients

FIXTURES = Path(__file__).parent.parent / "fixtures"


def lookup_handler(hits: list[str] | None = None):
    all_results = json.loads((FIXTURES / "lookup_multi_us.json").read_text())["results"]

    def handler(request: httpx.Request) -> httpx.Response:
        if hits is not None:
            hits.append(str(request.url))
        requested = set(dict(request.url.params).get("id", "").split(","))
        results = [r for r in all_results if str(r["trackId"]) in requested]
        body = {"resultCount": len(results), "results": results}
        return httpx.Response(200, content=json.dumps(body).encode())

    return handler


async def test_compare_returns_profiles_in_one_lookup() -> None:
    hits: list[str] = []
    clients = build_clients(lookup_handler(hits))
    async with clients.http:
        result = await compare_app_store_apps(
            clients.itunes,
            [
                "570060128",
                "https://apps.apple.com/us/app/babbel/id829587759",
                "379968583",
            ],
        )
    assert [a.app_id for a in result.apps] == ["570060128", "829587759", "379968583"]
    assert result.errors == []
    assert len(hits) == 1, "batch compare must use a single multi-ID lookup"


async def test_compare_reports_partial_failures_in_band() -> None:
    clients = build_clients(lookup_handler())
    async with clients.http:
        result = await compare_app_store_apps(
            clients.itunes, ["570060128", "111111111", "not-a-valid-ref"]
        )
    assert [a.app_id for a in result.apps] == ["570060128"]
    reasons = {e.app: e.reason for e in result.errors}
    assert "not found" in reasons["111111111"]
    assert "not-a-valid-ref" in reasons
    # The invalid ref keeps the value the caller sent.
    assert result.meta.warnings


async def test_compare_raises_when_all_fail() -> None:
    clients = build_clients(lookup_handler())
    async with clients.http:
        with pytest.raises(AppStoreMCPError, match=r"[Nn]one of"):
            await compare_app_store_apps(clients.itunes, ["111111111", "222222222"])


async def test_compare_empty_list_is_invalid_input() -> None:
    clients = build_clients(lookup_handler())
    async with clients.http:
        with pytest.raises(InvalidInputError):
            await compare_app_store_apps(clients.itunes, [])
