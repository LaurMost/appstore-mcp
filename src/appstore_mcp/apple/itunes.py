"""Client for Apple's iTunes Search/Lookup API - the primary, reliable source."""

from typing import Any

import httpx

from appstore_mcp.apple.fetch import get_json
from appstore_mcp.cache import CacheEntry, TTLCache

SEARCH_URL = "https://itunes.apple.com/search"
LOOKUP_URL = "https://itunes.apple.com/lookup"
SOURCE_NAME = "apple_itunes_api"


def search_params(query: str, *, country: str, limit: int) -> dict[str, Any]:
    return {
        "term": query,
        "country": country,
        "media": "software",
        "entity": "software",
        "limit": limit,
    }


def search_url(query: str, *, country: str, limit: int) -> str:
    """The exact URL a search fetches - reported verbatim in Source entries."""
    return str(httpx.URL(SEARCH_URL, params=search_params(query, country=country, limit=limit)))


def lookup_params(app_ids: list[str], *, country: str) -> dict[str, Any]:
    return {"id": ",".join(app_ids), "country": country}


def lookup_url(app_ids: list[str], *, country: str) -> str:
    """The exact URL a lookup fetches - reported verbatim in Source entries."""
    return str(httpx.URL(LOOKUP_URL, params=lookup_params(app_ids, country=country)))


class ITunesClient:
    def __init__(self, http: httpx.AsyncClient, cache: TTLCache[Any] | None = None) -> None:
        self._http = http
        self._cache = cache if cache is not None else TTLCache()

    async def search(self, query: str, *, country: str, limit: int) -> CacheEntry[Any]:
        params = search_params(query, country=country, limit=limit)
        key = f"search:{country}:{limit}:{query.strip().lower()}"

        async def fetch() -> Any:
            return await get_json(self._http, SEARCH_URL, source=SOURCE_NAME, params=params)

        return await self._cache.get_or_fetch(key, fetch)

    async def lookup(self, app_ids: list[str], *, country: str) -> CacheEntry[Any]:
        params = lookup_params(app_ids, country=country)
        key = f"lookup:{country}:{params['id']}"

        async def fetch() -> Any:
            return await get_json(self._http, LOOKUP_URL, source=SOURCE_NAME, params=params)

        return await self._cache.get_or_fetch(key, fetch)
