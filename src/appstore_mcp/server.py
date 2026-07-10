"""FastMCP server: thin tools over the apple/ clients."""

import asyncio
import re
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from fastmcp.dependencies import CurrentContext
from fastmcp.server.context import Context
from pydantic import Field, ValidationError

from appstore_mcp.apple import charts as charts_mod
from appstore_mcp.apple import itunes as itunes_mod
from appstore_mcp.apple import page as page_mod
from appstore_mcp.apple import reviews as reviews_mod
from appstore_mcp.apple.charts import (
    ChartsClient,
    chart_url,
    entries_from_chart_feed,
    resolve_genre_id,
)
from appstore_mcp.apple.fetch import USER_AGENT
from appstore_mcp.apple.ids import parse_app_ref
from appstore_mcp.apple.itunes import ITunesClient, lookup_url, search_url
from appstore_mcp.apple.normalize import (
    chart_entry_from_feed,
    profile_from_lookup,
    review_from_feed_entry,
    search_result_from_lookup,
)
from appstore_mcp.apple.page import (
    AppPageClient,
    PageParseError,
    enrichment_from_html,
    reviews_from_html,
)
from appstore_mcp.apple.reviews import (
    MAX_FEED_PAGES,
    ReviewSort,
    ReviewsClient,
    entries_from_feed,
    review_feed_url,
)
from appstore_mcp.cache import TTLCache
from appstore_mcp.errors import AppNotFoundError, AppStoreMCPError, InvalidInputError
from appstore_mcp.models import (
    AppError,
    ChartName,
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


async def _reap(task: "asyncio.Task[Any]") -> None:
    """Cancel an in-flight companion task and retrieve its outcome so the
    event loop never logs 'exception was never retrieved'."""
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


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
    cache: TTLCache[Any] = TTLCache()  # one cache across all sources
    itunes = ITunesClient(http, cache)
    app_page = AppPageClient(http, cache)
    charts = ChartsClient(http, cache)
    reviews_client = ReviewsClient(http, cache)

    mcp: FastMCP = FastMCP(name="appstore-mcp", instructions=INSTRUCTIONS)

    @mcp.tool(
        annotations={
            "title": "Search the Apple App Store",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        timeout=25.0,
    )
    async def search_app_store(
        query: str,
        country: str = DEFAULT_COUNTRY,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> SearchAppsResult:
        """Search Apple App Store apps by keyword. Returns slim results
        (id, name, developer, rating, price) - use get_app_store_app for the
        full profile of a specific app.

        Args:
            query: Keyword(s) to search for, e.g. 'language learning'.
            country: ISO 3166-1 alpha-2 storefront code, e.g. 'us', 'de', 'jp'.
            limit: Max results to return (1-50).
        """
        country = _validate_country(country)
        entry = await itunes.search(query, country=country, limit=limit)
        results = [
            search_result_from_lookup(item)
            for item in entry.value.get("results", [])
        ]
        return SearchAppsResult(
            meta=Meta(country=country, retrieved_at=entry.retrieved_at, fresh=entry.fresh),
            query=query,
            results=results,
            sources=[
                Source(
                    name=itunes_mod.SOURCE_NAME,
                    url=search_url(query, country=country, limit=limit),
                    retrieved_at=entry.retrieved_at,
                )
            ],
        )

    @mcp.tool(
        annotations={
            "title": "Get App Store app profile",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        timeout=25.0,
    )
    async def get_app_store_app(
        app_id_or_url: str,
        country: str | None = None,
        include_page_data: bool = True,
        include_raw: bool = False,
        ctx: Context = CurrentContext(),
    ) -> GetAppResult:
        """Fetch the full public App Store profile for one app by numeric ID or
        apps.apple.com URL. Page-sourced fields (subtitle, has_iap, privacy) are
        best-effort; set include_page_data=false to skip that second request.
        Set include_raw=true to also get Apple's unmodified lookup payload
        (large - only when normalized fields are not enough).

        Args:
            app_id_or_url: Numeric App Store app ID (e.g. '570060128') or a
                full apps.apple.com URL.
            country: ISO 3166-1 alpha-2 storefront code, e.g. 'us', 'de', 'jp'.
                Defaults to the country in the URL if one was passed, else 'us'.
            include_page_data: Also fetch subtitle, has_iap, and privacy labels
                from the public App Store page (best-effort, one extra request).
            include_raw: Also return Apple's unmodified lookup payload under
                `raw` (large - only when normalized fields are not enough).
        """
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
                await _reap(page_task)
            raise
        items: list[dict[str, Any]] = entry.value.get("results", [])
        if not items:
            if page_task is not None:
                await _reap(page_task)
            raise AppNotFoundError(ref.app_id, resolved_country)
        item = items[0]
        profile = profile_from_lookup(item)

        warnings: list[str] = []
        sources = [
            Source(
                name=itunes_mod.SOURCE_NAME,
                url=lookup_url([ref.app_id], country=resolved_country),
                retrieved_at=entry.retrieved_at,
            )
        ]
        if page_task is not None:
            try:
                page_entry = await page_task
                enrichment = enrichment_from_html(page_entry.value)
            except (AppStoreMCPError, PageParseError, ValidationError) as exc:
                warnings.append(
                    f"page enrichment failed; subtitle/has_iap/privacy unavailable ({exc})"
                )
                await ctx.warning(f"page enrichment failed for app {ref.app_id}: {exc}")
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
            "idempotentHint": True,
            "openWorldHint": True,
        },
        timeout=25.0,
    )
    async def compare_app_store_apps(
        apps: list[str],
        country: str = DEFAULT_COUNTRY,
    ) -> CompareAppsResult:
        """Fetch full profiles for multiple apps (IDs or apps.apple.com URLs) in
        one batch for side-by-side competitor comparison. Returns the profiles
        plus per-app errors; apps that fail do not fail the whole call.

        Args:
            apps: App IDs or apps.apple.com URLs to compare, e.g.
                ['570060128', 'https://apps.apple.com/us/app/babbel/id829587759'].
            country: ISO 3166-1 alpha-2 storefront all apps are compared on,
                e.g. 'us', 'de', 'jp'. One call always uses a single storefront.
        """
        country = _validate_country(country)
        if not apps:
            raise InvalidInputError("Pass at least one app ID or App Store URL in `apps`.")

        errors: list[AppError] = []
        ordered_ids: list[str] = []
        original_ref: dict[str, str] = {}  # app_id -> value the caller sent
        for value in apps:
            try:
                ref = parse_app_ref(value)
            except InvalidInputError as exc:
                errors.append(AppError(app=value, reason=str(exc)))
                continue
            if ref.app_id not in ordered_ids:
                ordered_ids.append(ref.app_id)
                original_ref[ref.app_id] = value

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
                        app=original_ref[app_id],
                        reason=f"app {app_id} not found in storefront '{country}' "
                        f"- it may exist in another country",
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
        return CompareAppsResult(
            meta=Meta(
                country=country,
                retrieved_at=entry.retrieved_at,
                fresh=entry.fresh,
                warnings=warnings,
            ),
            apps=profiles,
            errors=errors,
            sources=[
                Source(
                    name=itunes_mod.SOURCE_NAME,
                    url=lookup_url(ordered_ids, country=country),
                    retrieved_at=entry.retrieved_at,
                )
            ],
        )

    @mcp.tool(
        annotations={
            "title": "Get App Store charts",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        timeout=25.0,
    )
    async def get_app_store_charts(
        country: str = DEFAULT_COUNTRY,
        chart: ChartName = "top-free",
        category: str | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> ChartsResult:
        """Fetch ranked top-chart apps for a storefront. Best-effort: sourced
        from an undocumented Apple RSS feed.

        Args:
            country: ISO 3166-1 alpha-2 storefront code, e.g. 'us', 'de', 'jp'.
            chart: Which chart to fetch: 'top-free', 'top-paid', or
                'top-grossing'.
            category: Optional filter - a numeric App Store genre ID, or one
                of: books, business, developer-tools, education,
                entertainment, finance, food-drink, games, graphics-design,
                health-fitness, lifestyle, magazines-newspapers, medical,
                music, navigation, news, photo-video, productivity,
                reference, shopping, social-networking, sports, stickers,
                travel, utilities, weather. Omit for the overall chart.
            limit: Max entries to return (1-100).
        """
        country = _validate_country(country)
        genre_id = resolve_genre_id(category) if category else None
        entry = await charts.fetch(
            country=country, chart=chart, limit=limit, genre_id=genre_id
        )
        entries = [
            chart_entry_from_feed(item, rank=index)
            for index, item in enumerate(entries_from_chart_feed(entry.value), start=1)
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
            "idempotentHint": True,
            "openWorldHint": True,
        },
        timeout=30.0,
    )
    async def get_app_store_reviews(
        app_id_or_url: str,
        country: str | None = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
        sort: ReviewSort = "most_recent",
        ctx: Context = CurrentContext(),
    ) -> ReviewsResult:
        """Fetch recent public customer reviews for an app. Best-effort:
        sourced from an undocumented Apple feed capped at ~500 reviews per
        storefront, with a small page-sourced fallback when the feed is
        empty. Reviews are per-country.

        Args:
            app_id_or_url: Numeric App Store app ID or a full apps.apple.com
                URL.
            country: ISO 3166-1 alpha-2 storefront code, e.g. 'us', 'de', 'jp'.
                Defaults to the country in the URL if one was passed, else
                'us'.
            limit: Max reviews to return (1-500; Apple caps the underlying
                feed at ~500 per storefront regardless of this value).
            sort: 'most_recent' or 'most_helpful'.
        """
        ref = parse_app_ref(app_id_or_url)
        resolved_country = _validate_country(country or ref.country or DEFAULT_COUNTRY)

        collected: list[Review] = []
        warnings: list[str] = []
        sources: list[Source] = []
        first_entry = None
        for page_number in range(1, MAX_FEED_PAGES + 1):
            entry = await reviews_client.fetch_page(
                ref.app_id, country=resolved_country, sort=sort, page=page_number
            )
            # Best-effort estimate: the loop below may break early once `limit`
            # is satisfied, so `MAX_FEED_PAGES` is an upper bound, not a promise.
            await ctx.report_progress(progress=page_number, total=MAX_FEED_PAGES)
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

        assert first_entry is not None
        retrieved_at = first_entry.retrieved_at
        fresh = first_entry.fresh
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
                # The page, not the empty feed, is what served these reviews.
                retrieved_at = page_entry.retrieved_at
                fresh = page_entry.fresh
            except (AppStoreMCPError, PageParseError, ValidationError) as exc:
                warnings.append(f"page fallback also failed ({exc})")
                await ctx.warning(
                    f"review page fallback also failed for app {ref.app_id}: {exc}"
                )

        return ReviewsResult(
            meta=Meta(
                country=resolved_country,
                retrieved_at=retrieved_at,
                fresh=fresh,
                warnings=warnings,
            ),
            app_id=ref.app_id,
            reviews=collected,
            sources=sources,
        )

    @mcp.prompt
    def compare_competitors(apps: str, country: str = DEFAULT_COUNTRY) -> str:
        """Run a competitor comparison for the given apps on one storefront.

        Args:
            apps: Names, IDs, or App Store URLs to compare, comma-separated.
            country: ISO 3166-1 alpha-2 storefront code, e.g. 'us', 'de', 'jp'.
        """
        return (
            f"Compare these apps on the '{country}' Apple App Store storefront: "
            f"{apps}.\n\n"
            "If any of them are names rather than IDs/URLs, resolve them with "
            "search_app_store first. Then fetch them side by side with "
            "compare_app_store_apps and analyze: positioning (name, subtitle, "
            "description angle), pricing model, rating level vs. volume, update "
            "cadence (last_updated), category overlap, and localization reach "
            "(languages). Note any limitations reported in meta.warnings, and "
            "remember download counts and revenue are not available from public "
            "App Store data."
        )

    return mcp


def main() -> None:
    # Stdio transport: stdout carries JSON-RPC only; never print to stdout here
    # (fastmcp's own banner and logging go to stderr).
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
