"""Client for Apple's iTunes Search/Lookup API - the primary, reliable source."""

from typing import Any

import httpx

from appstore_mcp.apple.fetch import get_json
from appstore_mcp.cache import CacheEntry, TTLCache

SEARCH_URL = "https://itunes.apple.com/search"
LOOKUP_URL = "https://itunes.apple.com/lookup"
SOURCE_NAME = "apple_itunes_api"


class ITunesClient:
    def __init__(self, http: httpx.AsyncClient, cache: TTLCache[Any] | None = None) -> None:
        self._http = http
        self._cache = cache if cache is not None else TTLCache()

    async def search(self, query: str, *, country: str, limit: int) -> CacheEntry[Any]:
        params = {
            "term": query,
            "country": country,
            "media": "software",
            "entity": "software",
            "limit": limit,
        }
        key = f"search:{country}:{limit}:{query.strip().lower()}"

        async def fetch() -> Any:
            return await get_json(self._http, SEARCH_URL, source=SOURCE_NAME, params=params)

        return await self._cache.get_or_fetch(key, fetch)

    async def lookup(self, app_ids: list[str], *, country: str) -> CacheEntry[Any]:
        joined = ",".join(app_ids)
        params = {"id": joined, "country": country}
        key = f"lookup:{country}:{joined}"

        async def fetch() -> Any:
            return await get_json(self._http, LOOKUP_URL, source=SOURCE_NAME, params=params)

        return await self._cache.get_or_fetch(key, fetch)
