"""FastMCP server: thin tools over the apple/ clients."""

import asyncio
import contextlib
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from typing import Annotated, Any, Literal

import httpx
from fastmcp import FastMCP
from fastmcp.dependencies import CurrentContext
from fastmcp.server.context import Context
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.middleware.logging import StructuredLoggingMiddleware
from fastmcp.server.middleware.timing import DetailedTimingMiddleware
from fastmcp.tools import ToolResult
from fastmcp.utilities.logging import get_logger
from fastmcp.utilities.types import Image
from mcp.types import Icon, SamplingMessage, TextContent
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
    screenshot_urls_from_html,
)
from appstore_mcp.apple.reviews import (
    MAX_FEED_PAGES,
    ReviewsClient,
    ReviewSort,
    entries_from_feed,
    review_feed_url,
)
from appstore_mcp.cache import TTLCache
from appstore_mcp.digest import DIGEST_SYSTEM_PROMPT, build_digest_prompt, parse_digest
from appstore_mcp.errors import AppNotFoundError, AppStoreMCPError, InvalidInputError
from appstore_mcp.models import (
    AppError,
    ChartName,
    ChartsResult,
    CompareAppsResult,
    DigestReviewsResult,
    GetAppResult,
    Meta,
    Review,
    ReviewsResult,
    SearchAppsResult,
    Source,
)


@dataclass
class _ReviewCollection:
    reviews: list[Review]
    sources: list[Source]
    warnings: list[str]
    retrieved_at: datetime
    fresh: bool


def _sampling_fallback_handler() -> Any | None:
    """Optional server-side sampling fallback for clients without sampling.

    Activated only when the user has set an API key AND installed the matching
    optional dependency (appstore-mcp[anthropic] / appstore-mcp[openai]).
    Without both, sampling requests go to the client as usual.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            from fastmcp.client.sampling.handlers.anthropic import (
                AnthropicSamplingHandler,
            )

            return AnthropicSamplingHandler(
                default_model=os.environ.get(
                    "APPSTORE_MCP_SAMPLING_MODEL", "claude-sonnet-5"
                )
            )
        except ImportError:
            return None
    if os.environ.get("OPENAI_API_KEY"):
        try:
            from fastmcp.client.sampling.handlers.openai import OpenAISamplingHandler

            return OpenAISamplingHandler(
                default_model=os.environ.get(
                    "APPSTORE_MCP_SAMPLING_MODEL", "gpt-4o-mini"
                )
            )
        except ImportError:
            return None
    return None


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
For reviews: get_app_store_reviews returns raw reviews; prefer
digest_app_store_reviews when you want themes/sentiment from many reviews
without loading them all (uses MCP sampling). get_app_store_screenshots
returns actual screenshot images for visual analysis.
"""

_COUNTRY_RE = re.compile(r"^[a-zA-Z]{2}$")

DEFAULT_COUNTRY = "us"

WEBSITE_URL = "https://github.com/LaurMost/appstore-mcp"


def _load_icon() -> Icon:
    """Embed the packaged SVG icon as a data URI.

    A data URI (rather than a hosted URL) matches how this project ships:
    stdio-only via `uvx`, with no domain of its own to host a static asset on.
    """
    svg_bytes = resources.files("appstore_mcp").joinpath("assets/icon.svg").read_bytes()
    data_uri = Image(data=svg_bytes, format="svg+xml").to_data_uri()
    return Icon(src=data_uri, mimeType="image/svg+xml", sizes=["any"])


async def _reap(task: "asyncio.Task[Any]") -> None:
    """Cancel an in-flight companion task and retrieve its outcome so the
    event loop never logs 'exception was never retrieved'."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


def _validate_country(country: str) -> str:
    if not _COUNTRY_RE.match(country):
        raise InvalidInputError(
            f"Invalid country {country!r}. Pass a two-letter ISO 3166-1 code "
            f"like 'us', 'de', or 'jp'."
        )
    return country.lower()


async def _log_fallback_failure(
    ctx: Context, message: str, exc: Exception, **fields: Any
) -> None:
    """Shared shape for the two deep-fallback-failure warnings (page
    enrichment in get_app_store_app; page fallback in _collect_reviews):
    mirrors the exception to the client/local log with a consistent
    structured `extra` payload, alongside (not instead of) the caller's own
    `meta.warnings` entry.
    """
    await ctx.warning(
        message,
        extra={**fields, "error_type": type(exc).__name__, "error_message": str(exc)},
    )


class UnexpectedErrorLoggingMiddleware(Middleware):
    """Logs tool-call exceptions that aren't already an agent-facing
    AppStoreMCPError, so upstream format drift (e.g. `apple/normalize.py`
    breaking on a changed Apple response shape) is visible on the server's
    own log between the weekly live-CI runs (see PLAN.md's "Testing"
    section) instead of silently surfacing as a bare, untraced error.

    AppStoreMCPError (and subclasses - AppNotFoundError, InvalidInputError,
    RateLimitedError, UpstreamError) are deliberately excluded: those are
    the expected "tool error" tier from PLAN.md's failure-semantics design,
    already written for agent recovery - logging every not-found/rate-limit
    call at ERROR level would just be noise, not a signal.

    Never transforms or swallows the exception (per FastMCP's "log and
    re-raise" guidance for custom middleware error handling) - only adds a
    server-side log line before it propagates unchanged.
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or get_logger("appstore_mcp.errors")

    async def on_call_tool(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        try:
            return await call_next(context)
        except AppStoreMCPError:
            raise
        except Exception:
            tool_name = getattr(context.message, "name", "unknown")
            self._logger.exception("unexpected error in tool %s", tool_name)
            raise


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

    sampling_handler = _sampling_fallback_handler()
    icon = _load_icon()
    mcp: FastMCP = FastMCP(
        name="appstore-mcp",
        instructions=INSTRUCTIONS,
        website_url=WEBSITE_URL,
        icons=[icon],
        sampling_handler=sampling_handler,
        sampling_handler_behavior="fallback",
    )

    # Order matters (see FastMCP's middleware docs): error handling first so
    # it wraps everything on the way in and catches exceptions from the
    # middleware below it too; timing/logging last so they observe the
    # actual post-processed outcome. All three log via `logging` (nested
    # under the "fastmcp" logger, see get_logger), never `sys.stdout`, so
    # they can't violate the stdio JSON-RPC-only constraint documented in
    # main() below.
    mcp.add_middleware(UnexpectedErrorLoggingMiddleware())
    mcp.add_middleware(
        DetailedTimingMiddleware(logger=get_logger("appstore_mcp.timing"))
    )
    mcp.add_middleware(
        StructuredLoggingMiddleware(
            logger=get_logger("appstore_mcp.calls"),
            include_payload_length=True,
            methods=["tools/call"],
        )
    )

    @mcp.tool(
        annotations={
            "title": "Search the Apple App Store",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        icons=[icon],
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
            search_result_from_lookup(item) for item in entry.value.get("results", [])
        ]
        return SearchAppsResult(
            meta=Meta(
                country=country, retrieved_at=entry.retrieved_at, fresh=entry.fresh
            ),
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
        icons=[icon],
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
            asyncio.create_task(
                app_page.fetch_html(ref.app_id, country=resolved_country)
            )
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
                    f"page enrichment failed; subtitle/has_iap/privacy "
                    f"unavailable ({exc})"
                )
                await _log_fallback_failure(
                    ctx,
                    f"page enrichment failed for app {ref.app_id}: {exc}",
                    exc,
                    app_id=ref.app_id,
                    country=resolved_country,
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
            "idempotentHint": True,
            "openWorldHint": True,
        },
        icons=[icon],
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
            raise InvalidInputError(
                "Pass at least one app ID or App Store URL in `apps`."
            )

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
            str(item.get("trackId")): item for item in entry.value.get("results", [])
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
                f"'{country}': " + "; ".join(f"{e.app}: {e.reason}" for e in errors)
            )

        warnings = (
            [
                f"{len(errors)} of {len(apps)} requested apps could not be "
                f"fetched; see errors"
            ]
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
        icons=[icon],
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
                Source(
                    name=charts_mod.SOURCE_NAME,
                    url=url,
                    retrieved_at=entry.retrieved_at,
                )
            ],
        )

    @mcp.tool(
        annotations={
            "title": "Get App Store reviews",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        icons=[icon],
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
        collection = await _collect_reviews(
            ref.app_id, country=resolved_country, sort=sort, limit=limit, ctx=ctx
        )
        return ReviewsResult(
            meta=Meta(
                country=resolved_country,
                retrieved_at=collection.retrieved_at,
                fresh=collection.fresh,
                warnings=collection.warnings,
            ),
            app_id=ref.app_id,
            reviews=collection.reviews,
            sources=collection.sources,
        )

    async def _collect_reviews(
        app_id: str,
        *,
        country: str,
        sort: ReviewSort,
        limit: int,
        ctx: Context,
        progress_start: float = 0,
        progress_end: float = 100,
    ) -> _ReviewCollection:
        """Shared review harvesting: paginated feed, page fallback on empty.

        Progress is reported as a percentage of `total=100`, scaled into
        [progress_start, progress_end] so a caller doing further work
        afterwards (digest_app_store_reviews's LLM sampling stage) can
        reserve the remainder of the 0-100 range for its own progress. A
        terminal tick at progress_end is always emitted before returning,
        regardless of which exit path below is taken (full MAX_FEED_PAGES
        pages, an early break once `limit` is satisfied, or the page
        fallback succeeding/failing) - otherwise a client's progress
        indicator can stall below 100% with no "done" signal for this stage.
        """
        span = progress_end - progress_start

        async def _tick(fraction: float) -> None:
            await ctx.report_progress(
                progress=progress_start + span * fraction, total=100
            )

        collected: list[Review] = []
        warnings: list[str] = []
        sources: list[Source] = []
        first_entry = None
        for page_number in range(1, MAX_FEED_PAGES + 1):
            entry = await reviews_client.fetch_page(
                app_id, country=country, sort=sort, page=page_number
            )
            # Best-effort estimate: the loop below may break early once `limit`
            # is satisfied, so `MAX_FEED_PAGES` is an upper bound, not a promise.
            await _tick(page_number / MAX_FEED_PAGES)
            if first_entry is None:
                first_entry = entry
            page_entries = entries_from_feed(entry.value)
            if not page_entries:
                break
            sources.append(
                Source(
                    name=reviews_mod.SOURCE_NAME,
                    url=review_feed_url(
                        app_id, country=country, sort=sort, page=page_number
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
                f"the review feed returned no reviews for app {app_id} in "
                f"storefront '{country}'; falling back to the ~24 "
                f"'most helpful' reviews rendered on the public App Store page"
            )
            try:
                page_entry = await app_page.fetch_html(app_id, country=country)
                collected = reviews_from_html(page_entry.value)[:limit]
                sources.append(
                    Source(
                        name=page_mod.SOURCE_NAME,
                        url=page_mod.page_url(app_id, country),
                        retrieved_at=page_entry.retrieved_at,
                    )
                )
                # The page, not the empty feed, is what served these reviews.
                retrieved_at = page_entry.retrieved_at
                fresh = page_entry.fresh
            except (AppStoreMCPError, PageParseError, ValidationError) as exc:
                warnings.append(f"page fallback also failed ({exc})")
                await _log_fallback_failure(
                    ctx,
                    f"review page fallback also failed for app {app_id}: {exc}",
                    exc,
                    app_id=app_id,
                    country=country,
                    sort=sort,
                )

        await _tick(1.0)  # stage done regardless of which exit path was taken

        return _ReviewCollection(
            reviews=collected,
            sources=sources,
            warnings=warnings,
            retrieved_at=retrieved_at,
            fresh=fresh,
        )

    @mcp.tool(
        annotations={
            "title": "Digest App Store reviews",
            "readOnlyHint": True,
            "openWorldHint": True,
        },
        icons=[icon],
        timeout=120.0,
    )
    async def digest_app_store_reviews(
        app_id_or_url: str,
        country: str | None = None,
        limit: Annotated[int, Field(ge=10, le=500)] = 200,
        sort: ReviewSort = "most_recent",
        focus: str | None = None,
        ctx: Context = CurrentContext(),
    ) -> DigestReviewsResult:
        """Fetch up to `limit` reviews and compress them into a structured
        digest (themes, complaints, praise, sentiment) via MCP sampling, so
        hundreds of reviews never enter your context. Works across storefront
        languages - the digest is always English. Requires a client that
        supports MCP sampling (or a server-side API-key fallback); use
        get_app_store_reviews for the raw reviews instead.

        Args:
            app_id_or_url: Numeric App Store app ID or a full apps.apple.com
                URL.
            country: ISO 3166-1 alpha-2 storefront code, e.g. 'us', 'de', 'jp'.
                Defaults to the country in the URL if one was passed, else 'us'.
            limit: Max reviews to digest (10-500).
            sort: 'most_recent' or 'most_helpful'.
            focus: Optional steer for the digest, e.g. 'pricing complaints'
                or 'onboarding friction'.
        """
        ref = parse_app_ref(app_id_or_url)
        resolved_country = _validate_country(country or ref.country or DEFAULT_COUNTRY)
        # Review harvesting gets the first half of the progress range; LLM
        # digestion (below) gets the second half, so a client watching
        # progress sees continuous movement across both stages instead of
        # silence during the (often slower) sampling call.
        collection = await _collect_reviews(
            ref.app_id,
            country=resolved_country,
            sort=sort,
            limit=limit,
            ctx=ctx,
            progress_end=50,
        )
        if not collection.reviews:
            raise AppStoreMCPError(
                f"No reviews available to digest for app {ref.app_id} in "
                f"storefront '{resolved_country}'. Try another country or "
                f"check the app ID with get_app_store_app."
            )

        prompt = build_digest_prompt(
            ref.app_id, resolved_country, collection.reviews, focus
        )
        try:
            sampled = await ctx.sample(
                messages=prompt,
                system_prompt=DIGEST_SYSTEM_PROMPT,
                temperature=0.2,
                max_tokens=2000,
            )
        except Exception as exc:
            raise AppStoreMCPError(
                f"Review digestion needs LLM sampling, which this MCP client "
                f"does not support (or it failed: {exc}). Either use "
                f"get_app_store_reviews to fetch the raw reviews, or run the "
                f"server with an ANTHROPIC_API_KEY/OPENAI_API_KEY and the "
                f"matching optional dependency installed to enable the "
                f"server-side fallback."
            ) from exc

        warnings = list(collection.warnings)
        text = sampled.text or ""
        try:
            digest = parse_digest(text)
        except (ValueError, ValidationError) as first_error:
            await ctx.report_progress(progress=75, total=100)
            retry = await ctx.sample(
                messages=[
                    SamplingMessage(
                        role="user", content=TextContent(type="text", text=prompt)
                    ),
                    SamplingMessage(
                        role="assistant",
                        content=TextContent(type="text", text=text),
                    ),
                    SamplingMessage(
                        role="user",
                        content=TextContent(
                            type="text",
                            text=(
                                f"That response was invalid ({first_error}). "
                                f"Reply again with ONLY the corrected JSON object."
                            ),
                        ),
                    ),
                ],
                system_prompt=DIGEST_SYSTEM_PROMPT,
                temperature=0.0,
                max_tokens=2000,
            )
            digest = parse_digest(retry.text or "")
            warnings.append("digest required a retry after invalid LLM output")
        await ctx.report_progress(progress=100, total=100)
        warnings.append(
            "digest is LLM-generated from the reviews below; quotes may be "
            "translated or paraphrased"
        )

        return DigestReviewsResult(
            meta=Meta(
                country=resolved_country,
                retrieved_at=collection.retrieved_at,
                fresh=collection.fresh,
                warnings=warnings,
            ),
            app_id=ref.app_id,
            reviews_considered=len(collection.reviews),
            digest=digest,
            sources=collection.sources,
        )

    @mcp.tool(
        annotations={
            "title": "Get App Store screenshots",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        icons=[icon],
        timeout=60.0,
    )
    async def get_app_store_screenshots(
        app_id_or_url: str,
        country: str | None = None,
        device: Literal["iphone", "ipad"] = "iphone",
        limit: Annotated[int, Field(ge=1, le=8)] = 4,
    ) -> ToolResult:
        """Fetch an app's App Store screenshots as actual images, so you can
        analyze visual positioning, onboarding style, and paywall design
        directly. Returns up to `limit` screenshots as image content blocks.

        Args:
            app_id_or_url: Numeric App Store app ID or a full apps.apple.com
                URL.
            country: ISO 3166-1 alpha-2 storefront code, e.g. 'us', 'de', 'jp'.
                Defaults to the country in the URL if one was passed, else 'us'.
            device: Which screenshot set to fetch: 'iphone' or 'ipad'.
            limit: Max screenshots to return (1-8); each is a full image in
                context, so keep this small.
        """
        ref = parse_app_ref(app_id_or_url)
        resolved_country = _validate_country(country or ref.country or DEFAULT_COUNTRY)
        warnings: list[str] = []

        entry = await itunes.lookup([ref.app_id], country=resolved_country)
        items: list[dict[str, Any]] = entry.value.get("results", [])
        if not items:
            raise AppNotFoundError(ref.app_id, resolved_country)
        key = "screenshotUrls" if device == "iphone" else "ipadScreenshotUrls"
        urls: list[str] = list(items[0].get(key) or [])

        if not urls:
            # The lookup API sometimes omits screenshots entirely; the public
            # page's media shelves carry resizable artwork templates.
            try:
                page_entry = await app_page.fetch_html(
                    ref.app_id, country=resolved_country
                )
                urls = screenshot_urls_from_html(page_entry.value, device)
                warnings.append(
                    "screenshots sourced from the public App Store page "
                    "(lookup API returned none)"
                )
            except (AppStoreMCPError, PageParseError) as exc:
                raise AppStoreMCPError(
                    f"No {device} screenshots available for app {ref.app_id} "
                    f"in storefront '{resolved_country}' (page fallback "
                    f"failed: {exc})."
                ) from exc
        if not urls:
            raise AppStoreMCPError(
                f"App {ref.app_id} has no {device} screenshots in storefront "
                f"'{resolved_country}'. Try device='ipad' or another country."
            )

        urls = urls[:limit]

        async def fetch_image(url: str) -> tuple[bytes, str]:
            response = await http.get(url, follow_redirects=True)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            fmt = "png" if "png" in content_type or url.endswith(".png") else "jpeg"
            return response.content, fmt

        downloads = await asyncio.gather(
            *(fetch_image(url) for url in urls), return_exceptions=True
        )
        images = []
        fetched_urls = []
        for url, result in zip(urls, downloads, strict=False):
            if isinstance(result, BaseException):
                warnings.append(f"failed to fetch {url}: {result}")
                continue
            data, fmt = result
            images.append(Image(data=data, format=fmt))
            fetched_urls.append(url)
        if not images:
            raise AppStoreMCPError(
                f"All {len(urls)} screenshot downloads failed for app "
                f"{ref.app_id}: {'; '.join(warnings)}"
            )

        header = (
            f"{len(images)} {device} screenshot(s) for app {ref.app_id} "
            f"(storefront '{resolved_country}'), in store order:"
        )
        return ToolResult(
            content=[TextContent(type="text", text=header)]
            + [image.to_image_content() for image in images],
            structured_content={
                "app_id": ref.app_id,
                "country": resolved_country,
                "device": device,
                "count": len(images),
                "urls": fetched_urls,
                "warnings": warnings,
            },
        )

    @mcp.prompt(icons=[icon])
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
    #
    # ctx.warning(...) calls (see get_app_store_app, _collect_reviews) are
    # only mirrored to the server's own log at DEBUG if this logger is raised
    # explicitly - otherwise they're only visible to a client that's
    # listening for them. This just raises a stdlib logging.Logger's level;
    # it never touches sys.stdout, so it can't violate the constraint above.
    get_logger(name="fastmcp.server.context.to_client").setLevel(logging.DEBUG)
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
