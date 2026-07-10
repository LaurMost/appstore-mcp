"""FastMCP server: thin tools over the apple/ clients."""

import asyncio
import re
from typing import Any

import httpx
from fastmcp import FastMCP

from appstore_mcp.apple import charts as charts_mod
from appstore_mcp.apple import page as page_mod
from appstore_mcp.apple.charts import ChartName, ChartsClient, chart_url, resolve_genre_id
from appstore_mcp.apple.fetch import USER_AGENT
from appstore_mcp.apple.ids import parse_app_ref
from appstore_mcp.apple.itunes import LOOKUP_URL, SEARCH_URL, SOURCE_NAME, ITunesClient
from appstore_mcp.apple.page import AppPageClient, PageParseError, enrichment_from_html
from appstore_mcp.apple.normalize import (
    chart_entry_from_feed,
    profile_from_lookup,
    review_from_feed_entry,
    search_result_from_lookup,
)
from appstore_mcp.apple.page import reviews_from_html
from appstore_mcp.apple import reviews as reviews_mod
from appstore_mcp.apple.reviews import (
    MAX_FEED_PAGES,
    ReviewSort,
    ReviewsClient,
    entries_from_feed,
    review_feed_url,
)
from appstore_mcp.errors import AppNotFoundError, AppStoreMCPError, InvalidInputError
from appstore_mcp.models import (
    AppError,
    ChartsResult,
    CompareAppsResult,
    GetAppResult,
    Meta,
    Review,
    ReviewsResult,
    SearchAppsResult,
    Source,
)

INSTRUCTIONS = """\
Live public Apple App Store data for competitor research: search, app profiles,
side-by-side comparisons, reviews, and charts.

Scope: public data only. Download counts, revenue, and keyword rankings are NOT
available from public Apple endpoints - do not infer them from this data.
Apps are listed per-storefront: an app may exist in one country and not another,
and ratings/reviews differ per country. Pass `country` (ISO 3166-1 alpha-2)
explicitly when the user cares about a specific market.
Fields sourced from the public App Store web page (subtitle, has_iap, privacy)
are best-effort; when unavailable they are null and meta.warnings explains why.
"""

_COUNTRY_RE = re.compile(r"^[a-zA-Z]{2}$")

DEFAULT_COUNTRY = "us"


def _validate_country(country: str) -> str:
    if not _COUNTRY_RE.match(country):
        raise InvalidInputError(
            f"Invalid country {country!r}. Pass a two-letter ISO 3166-1 code "
            f"like 'us', 'de', or 'jp'."
        )
    return country.lower()


def create_server(http: httpx.AsyncClient | None = None) -> FastMCP:
    if http is None:
        http = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=20.0,
        )
    itunes = ITunesClient(http)
    app_page = AppPageClient(http)
    charts = ChartsClient(http)
    reviews_client = ReviewsClient(http)

    mcp: FastMCP = FastMCP(name="appstore-mcp", instructions=INSTRUCTIONS)

    @mcp.tool(
        annotations={
            "title": "Search the Apple App Store",
            "readOnlyHint": True,
            "openWorldHint": True,
        }
    )
    async def search_app_store(
        query: str,
        country: str = DEFAULT_COUNTRY,
        limit: int = 10,
    ) -> SearchAppsResult:
        """Search Apple App Store apps by keyword. Returns slim results
        (id, name, developer, rating, price) - use get_app_store_app for the
        full profile of a specific app."""
        country = _validate_country(country)
        limit = max(1, min(limit, 50))
        entry = await itunes.search(query, country=country, limit=limit)
        results = [
            search_result_from_lookup(item)
            for item in entry.value.get("results", [])
        ]
        url = str(
            httpx.URL(
                SEARCH_URL,
                params={
                    "term": query,
                    "country": country,
                    "media": "software",
                    "limit": limit,
                },
            )
        )
        return SearchAppsResult(
            meta=Meta(country=country, retrieved_at=entry.retrieved_at, fresh=entry.fresh),
            query=query,
            results=results,
            sources=[Source(name=SOURCE_NAME, url=url, retrieved_at=entry.retrieved_at)],
        )

    @mcp.tool(
        annotations={
            "title": "Get App Store app profile",
            "readOnlyHint": True,
            "openWorldHint": True,
        }
    )
    async def get_app_store_app(
        app_id_or_url: str,
        country: str | None = None,
        include_page_data: bool = True,
        include_raw: bool = False,
    ) -> GetAppResult:
        """Fetch the full public App Store profile for one app by numeric ID or
        apps.apple.com URL. Page-sourced fields (subtitle, has_iap, privacy) are
        best-effort; set include_page_data=false to skip that second request.
        Set include_raw=true to also get Apple's unmodified lookup payload
        (large - only when normalized fields are not enough)."""
        ref = parse_app_ref(app_id_or_url)
        resolved_country = _validate_country(country or ref.country or DEFAULT_COUNTRY)

        lookup_task = asyncio.create_task(
            itunes.lookup([ref.app_id], country=resolved_country)
        )
        page_task = (
            asyncio.create_task(app_page.fetch_html(ref.app_id, country=resolved_country))
            if include_page_data
            else None
        )

        try:
            entry = await lookup_task
        except BaseException:
            if page_task is not None:
                page_task.cancel()
            raise
        items: list[dict[str, Any]] = entry.value.get("results", [])
        if not items:
            if page_task is not None:
                page_task.cancel()
            raise AppNotFoundError(ref.app_id, resolved_country)
        item = items[0]
        profile = profile_from_lookup(item)

        warnings: list[str] = []
        sources = [
            Source(
                name=SOURCE_NAME,
                url=str(
                    httpx.URL(
                        LOOKUP_URL, params={"id": ref.app_id, "country": resolved_country}
                    )
                ),
                retrieved_at=entry.retrieved_at,
            )
        ]
        if page_task is not None:
            try:
                page_entry = await page_task
                enrichment = enrichment_from_html(page_entry.value)
            except (AppStoreMCPError, PageParseError) as exc:
                warnings.append(
                    f"page enrichment failed; subtitle/has_iap/privacy unavailable ({exc})"
                )
            else:
                profile.subtitle = enrichment.subtitle
                profile.has_iap = enrichment.has_iap
                profile.privacy = enrichment.privacy
                sources.append(
                    Source(
                        name=page_mod.SOURCE_NAME,
                        url=page_mod.page_url(ref.app_id, resolved_country),
                        retrieved_at=page_entry.retrieved_at,
                    )
                )

        return GetAppResult(
            meta=Meta(
                country=resolved_country,
                retrieved_at=entry.retrieved_at,
                fresh=entry.fresh,
                warnings=warnings,
            ),
            app=profile,
            sources=sources,
            raw={"itunes_lookup": item} if include_raw else None,
        )

    @mcp.tool(
        annotations={
            "title": "Compare App Store apps",
            "readOnlyHint": True,
            "openWorldHint": True,
        }
    )
    async def compare_app_store_apps(
        apps: list[str],
        country: str = DEFAULT_COUNTRY,
    ) -> CompareAppsResult:
        """Fetch full profiles for multiple apps (IDs or apps.apple.com URLs) in
        one batch for side-by-side competitor comparison. Returns the profiles
        plus per-app errors; apps that fail do not fail the whole call."""
        country = _validate_country(country)
        if not apps:
            raise InvalidInputError("Pass at least one app ID or App Store URL in `apps`.")

        errors: list[AppError] = []
        ordered_ids: list[str] = []
        for value in apps:
            try:
                ref = parse_app_ref(value)
            except InvalidInputError as exc:
                errors.append(AppError(app=value, reason=str(exc)))
                continue
            if ref.app_id not in ordered_ids:
                ordered_ids.append(ref.app_id)

        if not ordered_ids:
            raise InvalidInputError(
                "None of the provided values could be parsed as an app ID or "
                "App Store URL."
            )

        entry = await itunes.lookup(ordered_ids, country=country)
        by_id = {
            str(item.get("trackId")): item
            for item in entry.value.get("results", [])
        }
        profiles = []
        for app_id in ordered_ids:
            item = by_id.get(app_id)
            if item is None:
                errors.append(
                    AppError(
                        app=app_id,
                        reason=f"not found in storefront '{country}' - it may "
                        f"exist in another country",
                    )
                )
                continue
            profiles.append(profile_from_lookup(item))

        if not profiles:
            raise AppStoreMCPError(
                f"None of the requested apps could be fetched in storefront "
                f"'{country}': "
                + "; ".join(f"{e.app}: {e.reason}" for e in errors)
            )

        warnings = (
            [f"{len(errors)} of {len(apps)} requested apps could not be fetched; see errors"]
            if errors
            else []
        )
        url = str(
            httpx.URL(LOOKUP_URL, params={"id": ",".join(ordered_ids), "country": country})
        )
        return CompareAppsResult(
            meta=Meta(
                country=country,
                retrieved_at=entry.retrieved_at,
                fresh=entry.fresh,
                warnings=warnings,
            ),
            apps=profiles,
            errors=errors,
            sources=[Source(name=SOURCE_NAME, url=url, retrieved_at=entry.retrieved_at)],
        )

    @mcp.tool(
        annotations={
            "title": "Get App Store charts",
            "readOnlyHint": True,
            "openWorldHint": True,
        }
    )
    async def get_app_store_charts(
        country: str = DEFAULT_COUNTRY,
        chart: ChartName = "top-free",
        category: str | None = None,
        limit: int = 50,
    ) -> ChartsResult:
        """Fetch ranked top-chart apps for a storefront: top-free, top-paid, or
        top-grossing, optionally filtered to a category (name like 'finance' or
        numeric App Store genre ID). Best-effort: sourced from an undocumented
        Apple RSS feed."""
        country = _validate_country(country)
        limit = max(1, min(limit, 100))
        genre_id = resolve_genre_id(category) if category else None
        entry = await charts.fetch(
            country=country, chart=chart, limit=limit, genre_id=genre_id
        )
        feed = entry.value.get("feed") or {}
        raw_entries = feed.get("entry") or []
        if isinstance(raw_entries, dict):  # Apple returns a bare object for limit=1
            raw_entries = [raw_entries]
        entries = [
            chart_entry_from_feed(item, rank=index)
            for index, item in enumerate(raw_entries, start=1)
        ]
        url = chart_url(country, chart, limit=limit, genre_id=genre_id)
        warnings = [
            "chart data comes from an undocumented Apple RSS feed and may "
            "change or break without notice"
        ]
        if not entries:
            warnings.append(
                f"the feed returned no entries for chart='{chart}' "
                f"category={category!r} in storefront '{country}'"
            )
        return ChartsResult(
            meta=Meta(
                country=country,
                retrieved_at=entry.retrieved_at,
                fresh=entry.fresh,
                warnings=warnings,
            ),
            chart=chart,
            category=category,
            entries=entries,
            sources=[
                Source(name=charts_mod.SOURCE_NAME, url=url, retrieved_at=entry.retrieved_at)
            ],
        )

    @mcp.tool(
        annotations={
            "title": "Get App Store reviews",
            "readOnlyHint": True,
            "openWorldHint": True,
        }
    )
    async def get_app_store_reviews(
        app_id_or_url: str,
        country: str | None = None,
        limit: int = 50,
        sort: ReviewSort = "most_recent",
    ) -> ReviewsResult:
        """Fetch recent public customer reviews for an app (numeric ID or
        apps.apple.com URL). Best-effort: sourced from an undocumented Apple
        feed capped at ~500 reviews per storefront, with a small page-sourced
        fallback when the feed is empty. Reviews are per-country."""
        ref = parse_app_ref(app_id_or_url)
        resolved_country = _validate_country(country or ref.country or DEFAULT_COUNTRY)
        limit = max(1, min(limit, 500))

        collected: list[Review] = []
        warnings: list[str] = []
        sources: list[Source] = []
        first_entry = None
        for page_number in range(1, MAX_FEED_PAGES + 1):
            entry = await reviews_client.fetch_page(
                ref.app_id, country=resolved_country, sort=sort, page=page_number
            )
            if first_entry is None:
                first_entry = entry
            page_entries = entries_from_feed(entry.value)
            if not page_entries:
                break
            sources.append(
                Source(
                    name=reviews_mod.SOURCE_NAME,
                    url=review_feed_url(
                        ref.app_id, country=resolved_country, sort=sort, page=page_number
                    ),
                    retrieved_at=entry.retrieved_at,
                )
            )
            collected.extend(review_from_feed_entry(item) for item in page_entries)
            if len(collected) >= limit:
                break
        collected = collected[:limit]

        if not collected:
            warnings.append(
                f"the review feed returned no reviews for app {ref.app_id} in "
                f"storefront '{resolved_country}'; falling back to the ~24 "
                f"'most helpful' reviews rendered on the public App Store page"
            )
            try:
                page_entry = await app_page.fetch_html(
                    ref.app_id, country=resolved_country
                )
                collected = reviews_from_html(page_entry.value)[:limit]
                sources.append(
                    Source(
                        name=page_mod.SOURCE_NAME,
                        url=page_mod.page_url(ref.app_id, resolved_country),
                        retrieved_at=page_entry.retrieved_at,
                    )
                )
            except (AppStoreMCPError, PageParseError) as exc:
                warnings.append(f"page fallback also failed ({exc})")

        assert first_entry is not None
        return ReviewsResult(
            meta=Meta(
                country=resolved_country,
                retrieved_at=first_entry.retrieved_at,
                fresh=first_entry.fresh,
                warnings=warnings,
            ),
            app_id=ref.app_id,
            reviews=collected,
            sources=sources,
        )

    return mcp


def main() -> None:
    create_server().run()


if __name__ == "__main__":
    main()
