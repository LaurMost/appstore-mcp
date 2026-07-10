"""Shared HTTP fetch helpers: map Apple/network failures onto our error types."""

import json
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import httpx

from appstore_mcp.errors import RateLimitedError, UpstreamError

try:
    _VERSION = version("appstore-mcp")
except PackageNotFoundError:  # running from a source tree without install
    _VERSION = "dev"

USER_AGENT = (
    f"appstore-mcp/{_VERSION} "
    f"(open-source MCP server; +https://pypi.org/project/appstore-mcp/)"
)


async def get_text(
    http: httpx.AsyncClient,
    url: str,
    *,
    source: str,
    params: dict[str, Any] | None = None,
) -> str:
    try:
        response = await http.get(url, params=params, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise UpstreamError(source, str(exc)) from exc
    if response.status_code in (403, 429):
        raise RateLimitedError(source)
    if response.status_code >= 400:
        raise UpstreamError(source, f"HTTP {response.status_code}")
    return response.text


async def get_json(
    http: httpx.AsyncClient,
    url: str,
    *,
    source: str,
    params: dict[str, Any] | None = None,
) -> Any:
    # The iTunes API serves JSON with a text/javascript content type, so parse
    # the body ourselves rather than trusting response.json() content sniffing.
    text = await get_text(http, url, source=source, params=params)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise UpstreamError(source, "response was not valid JSON") from exc
