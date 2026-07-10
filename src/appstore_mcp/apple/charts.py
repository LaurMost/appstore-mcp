"""Legacy per-genre iTunes RSS charts.

Live-verified 2026-07-10: this undocumented feed still serves current,
genre-filtered results (the newer rss.marketingtools.apple.com feed dropped
genre filtering). Apple offers no uptime guarantee, so callers surface a
best-effort warning with every response.
"""

from typing import Any, Literal

import httpx

from appstore_mcp.apple.fetch import get_json
from appstore_mcp.cache import CacheEntry, TTLCache
from appstore_mcp.errors import InvalidInputError

SOURCE_NAME = "apple_itunes_rss_charts"

ChartName = Literal["top-free", "top-paid", "top-grossing"]

CHART_FEEDS: dict[str, str] = {
    "top-free": "topfreeapplications",
    "top-paid": "toppaidapplications",
    "top-grossing": "topgrossingapplications",
}

# Public App Store genre IDs (https://itunes.apple.com/WebObjects/MZStoreServices.woa/ws/genres)
GENRE_IDS: dict[str, str] = {
    "books": "6018",
    "business": "6000",
    "developer-tools": "6026",
    "education": "6017",
    "entertainment": "6016",
    "finance": "6015",
    "food-drink": "6023",
    "games": "6014",
    "graphics-design": "6027",
    "health-fitness": "6013",
    "lifestyle": "6012",
    "magazines-newspapers": "6021",
    "medical": "6020",
    "music": "6011",
    "navigation": "6010",
    "news": "6009",
    "photo-video": "6008",
    "productivity": "6007",
    "reference": "6006",
    "shopping": "6024",
    "social-networking": "6005",
    "sports": "6004",
    "stickers": "6025",
    "travel": "6003",
    "utilities": "6002",
    "weather": "6001",
}


def resolve_genre_id(category: str) -> str:
    if category.isdigit():
        return category
    slug = (
        category.strip()
        .lower()
        .replace(" & ", "-")
        .replace(" and ", "-")
        .replace(" ", "-")
    )
    if slug in GENRE_IDS:
        return GENRE_IDS[slug]
    raise InvalidInputError(
        f"Unknown category {category!r}. Pass a numeric App Store genre ID or "
        f"one of: {', '.join(sorted(GENRE_IDS))}."
    )


def chart_url(country: str, chart: str, *, limit: int, genre_id: str | None) -> str:
    feed = CHART_FEEDS[chart]
    genre_part = f"/genre={genre_id}" if genre_id else ""
    return f"https://itunes.apple.com/{country}/rss/{feed}/limit={limit}{genre_part}/json"


class ChartsClient:
    def __init__(self, http: httpx.AsyncClient, cache: TTLCache[Any] | None = None) -> None:
        self._http = http
        self._cache = cache if cache is not None else TTLCache()

    async def fetch(
        self, *, country: str, chart: str, limit: int, genre_id: str | None
    ) -> CacheEntry[Any]:
        url = chart_url(country, chart, limit=limit, genre_id=genre_id)

        async def fetch() -> Any:
            return await get_json(self._http, url, source=SOURCE_NAME)

        return await self._cache.get_or_fetch(f"charts:{url}", fetch)
