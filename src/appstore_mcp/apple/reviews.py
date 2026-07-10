"""Legacy iTunes customer-review RSS feed.

Live-verified 2026-07-10 (returned reviews <24h old for a high-traffic app),
but Apple gives no uptime guarantee and coverage varies per app/storefront.
Hard cap ~500 reviews (10 pages x 50). When the feed 200s with zero entries,
callers fall back to the ~24 server-rendered reviews on the public app page.
"""

from typing import Any, Literal

import httpx

from appstore_mcp.apple.fetch import get_json
from appstore_mcp.cache import CacheEntry, TTLCache

SOURCE_NAME = "apple_itunes_rss_reviews"

MAX_FEED_PAGES = 10
PAGE_SIZE = 50

ReviewSort = Literal["most_recent", "most_helpful"]

_SORT_VALUES: dict[str, str] = {
    "most_recent": "mostRecent",
    "most_helpful": "mostHelpful",
}


def review_feed_url(app_id: str, *, country: str, sort: str, page: int) -> str:
    return (
        f"https://itunes.apple.com/{country}/rss/customerreviews/"
        f"id={app_id}/sortBy={_SORT_VALUES[sort]}/page={page}/json"
    )


class ReviewsClient:
    def __init__(
        self, http: httpx.AsyncClient, cache: TTLCache[Any] | None = None
    ) -> None:
        self._http = http
        self._cache = cache if cache is not None else TTLCache()

    async def fetch_page(
        self, app_id: str, *, country: str, sort: str, page: int
    ) -> CacheEntry[Any]:
        url = review_feed_url(app_id, country=country, sort=sort, page=page)

        async def fetch() -> Any:
            return await get_json(self._http, url, source=SOURCE_NAME)

        return await self._cache.get_or_fetch(f"reviews:{url}", fetch)


def entries_from_feed(payload: Any) -> list[dict[str, Any]]:
    feed = payload.get("feed") if isinstance(payload, dict) else None
    entries = feed.get("entry") if isinstance(feed, dict) else None
    if isinstance(entries, dict):  # single-entry pages come back as a bare object
        entries = [entries]
    return entries if isinstance(entries, list) else []
