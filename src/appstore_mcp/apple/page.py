"""Best-effort extraction from the public App Store web page.

The page embeds the JSON its own web client hydrates from in a
<script id="serialized-server-data"> tag. This is the sole public source for
subtitle, has_iap, and privacy labels (the lookup API has none of them), and
the fallback source for reviews. Everything here is fail-soft: callers catch
PageParseError/UpstreamError and degrade with a warning, never fail the tool.
"""

import json
import re
from dataclasses import dataclass
from typing import Any

import httpx

from appstore_mcp.apple.fetch import get_text
from appstore_mcp.cache import CacheEntry, TTLCache
from appstore_mcp.models import Review

SOURCE_NAME = "apple_app_store_page"

_SCRIPT_RE = re.compile(
    r'<script[^>]*id="serialized-server-data"[^>]*>(.*?)</script>', re.DOTALL
)


class PageParseError(Exception):
    """The page did not contain the embedded data we expected."""


@dataclass(frozen=True)
class PageEnrichment:
    subtitle: str | None
    has_iap: bool | None
    privacy: list[dict[str, Any]] | None


def page_url(app_id: str, country: str) -> str:
    return f"https://apps.apple.com/{country}/app/id{app_id}"


def _server_data(html: str) -> dict[str, Any]:
    match = _SCRIPT_RE.search(html)
    if not match:
        raise PageParseError("no serialized-server-data script tag found")
    try:
        payload = json.loads(match.group(1))
        data = payload["data"][0]["data"]
    except (ValueError, LookupError, TypeError) as exc:
        raise PageParseError(f"embedded page data had unexpected shape: {exc}") from exc
    if not isinstance(data, dict):
        raise PageParseError("embedded page data had unexpected shape: not an object")
    return data


def _normalize_privacy(shelf: Any) -> list[dict[str, Any]] | None:
    if not isinstance(shelf, dict):
        return None
    items = shelf.get("items")
    if not isinstance(items, list):
        return None
    cards: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        categories = [
            c.get("title")
            for c in item.get("categories") or []
            if isinstance(c, dict) and c.get("title")
        ]
        purposes = [
            p.get("title")
            for p in item.get("purposes") or []
            if isinstance(p, dict) and p.get("title")
        ]
        cards.append(
            {
                "identifier": item.get("identifier"),
                "title": item.get("title"),
                "detail": item.get("detail"),
                "categories": categories,
                "purposes": purposes,
            }
        )
    return cards or None


def enrichment_from_html(html: str) -> PageEnrichment:
    data = _server_data(html)
    lockup = data.get("lockup") or {}
    offer = lockup.get("offerDisplayProperties") or {}
    shelves = data.get("shelfMapping") or {}
    subtitle = lockup.get("subtitle")
    has_iap = offer.get("hasInAppPurchases")
    return PageEnrichment(
        subtitle=subtitle if isinstance(subtitle, str) else None,
        has_iap=has_iap if isinstance(has_iap, bool) else None,
        privacy=_normalize_privacy(shelves.get("privacyTypes")),
    )


def reviews_from_html(html: str) -> list[Review]:
    """Server-rendered 'most helpful' reviews - the fallback when the RSS
    review feed returns nothing for a storefront."""
    data = _server_data(html)
    shelves = data.get("shelfMapping") or {}
    shelf = shelves.get("userProductReviews") or {}
    items = shelf.get("items") if isinstance(shelf, dict) else None
    reviews: list[Review] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        review = item.get("review")
        if not isinstance(review, dict):
            continue
        body = review.get("contents") or ""
        if not body:
            continue
        if not isinstance(body, str):
            continue
        rating = review.get("rating")
        rating_int: int | None
        try:
            rating_int = int(rating) if rating is not None else None
        except (TypeError, ValueError):
            rating_int = None

        def _str_or_none(value: Any) -> str | None:
            return value if isinstance(value, str) else None

        reviews.append(
            Review(
                review_id=str(review.get("id")) if review.get("id") is not None else None,
                title=_str_or_none(review.get("title")),
                body=body,
                rating=rating_int,
                author=_str_or_none(review.get("reviewerName")),
                updated_at=_str_or_none(review.get("date")),
            )
        )
    return reviews


class AppPageClient:
    def __init__(self, http: httpx.AsyncClient, cache: TTLCache[str] | None = None) -> None:
        self._http = http
        self._cache = cache if cache is not None else TTLCache()

    async def fetch_html(self, app_id: str, *, country: str) -> CacheEntry[str]:
        url = page_url(app_id, country)
        key = f"page:{country}:{app_id}"

        async def fetch() -> str:
            return await get_text(self._http, url, source=SOURCE_NAME)

        return await self._cache.get_or_fetch(key, fetch)
