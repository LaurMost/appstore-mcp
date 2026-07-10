"""FastMCP server: thin tools over the apple/ clients.

Server construction and MCP wire-shaping only. Each tool's orchestration body
lives in `appstore_mcp.tools.*`; the 3 ctx-free tools delegate straight to
their `tools/` function, and the 4 ctx-using ones get a thin wrapper here that
unpacks FastMCP's request-scoped `Context`/`Progress` into the narrow
`runtime` protocols (`Warner`/`Sampler`/`DualChannelProgressReporter`) before
delegating. Icon loading, the sampling fallback handler, and the middleware
classes stay here too - all server wiring, not orchestration.
"""

import logging
import os
from collections.abc import AsyncIterator
from datetime import timedelta
from importlib import resources
from typing import Annotated, Any, Literal

import httpx
from fastmcp import FastMCP
from fastmcp.dependencies import CurrentContext, Progress
from fastmcp.server.context import Context
from fastmcp.server.lifespan import lifespan
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.middleware.logging import StructuredLoggingMiddleware
from fastmcp.server.middleware.timing import DetailedTimingMiddleware
from fastmcp.server.tasks import TaskConfig
from fastmcp.tools import ToolResult
from fastmcp.utilities.logging import get_logger
from fastmcp.utilities.types import Image
from mcp.types import Icon, TextContent
from pydantic import Field

from appstore_mcp.apple.charts import ChartsClient
from appstore_mcp.apple.fetch import USER_AGENT
from appstore_mcp.apple.itunes import ITunesClient
from appstore_mcp.apple.page import AppPageClient
from appstore_mcp.apple.reviews import ReviewsClient, ReviewSort
from appstore_mcp.cache import TTLCache
from appstore_mcp.errors import AppStoreMCPError
from appstore_mcp.models import (
    ChartName,
    ChartsResult,
    CompareAppsResult,
    DigestReviewsResult,
    GetAppResult,
    ReviewsResult,
    SearchAppsResult,
)
from appstore_mcp.runtime import DualChannelProgressReporter
from appstore_mcp.tools.app import get_app_store_app as _get_app_store_app
from appstore_mcp.tools.charts import get_app_store_charts as _get_app_store_charts
from appstore_mcp.tools.compare import compare_app_store_apps as _compare_app_store_apps
from appstore_mcp.tools.reviews import (
    digest_app_store_reviews as _digest_app_store_reviews,
)
from appstore_mcp.tools.reviews import get_app_store_reviews as _get_app_store_reviews
from appstore_mcp.tools.screenshots import (
    get_app_store_screenshots as _get_app_store_screenshots,
)
from appstore_mcp.tools.search import search_app_store as _search_app_store


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

DEFAULT_COUNTRY = "us"

WEBSITE_URL = "https://github.com/LaurMost/appstore-mcp"

# Shared by the three tools whose sequential/blocking work (up to 10 review
# feed pages, LLM sampling, concurrent screenshot downloads) makes them
# candidates for MCP background task execution (SEP-1686). `mode="optional"`
# preserves today's synchronous behavior for clients that don't request a
# task, and only changes behavior for task-aware clients (graceful
# degradation - see FastMCP's Background Tasks docs).
_BACKGROUND_TASK = TaskConfig(mode="optional", poll_interval=timedelta(seconds=5))


def _load_icon() -> Icon:
    """Embed the packaged PNG icon as a data URI.

    A data URI (rather than a hosted URL) matches how this project ships:
    stdio-only via `uvx`, with no domain of its own to host a static asset on.

    PNG, not the SVG it is rendered from (assets/icon.svg): the MCP spec only
    obligates clients to render PNG/JPEG icons, and Claude Desktop drops
    image/svg+xml data URIs on the floor. Regenerate after editing the SVG:
    mcpb/build.sh step 3 has the headless-Chrome recipe (128px here).
    """
    png_bytes = resources.files("appstore_mcp").joinpath("assets/icon.png").read_bytes()
    data_uri = Image(data=png_bytes, format="png").to_data_uri()
    return Icon(src=data_uri, mimeType="image/png", sizes=["128x128"])


class UnexpectedErrorLoggingMiddleware(Middleware):
    """Logs tool-call exceptions that aren't already an agent-facing
    AppStoreMCPError, so upstream format drift (e.g. `apple/normalize.py`
    breaking on a changed Apple response shape) is visible on the server's
    own log between the weekly live-CI runs (see AGENTS.md's "Testing"
    note) instead of silently surfacing as a bare, untraced error.

    AppStoreMCPError (and subclasses - AppNotFoundError, InvalidInputError,
    RateLimitedError, UpstreamError) are deliberately excluded: those are
    the expected "tool error" tier from the failure-semantics design
    (docs/adr/0006-two-tier-failure-semantics.md), already written for agent
    recovery - logging every not-found/rate-limit call at ERROR level would
    just be noise, not a signal.

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
    # Only a client built here is ours to close: a caller-supplied `http`
    # (every test does this, to inject a MockTransport) is the caller's
    # resource, and may be reused or closed on their own schedule.
    owns_http = http is None
    if http is None:
        http = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=20.0,
        )

    @lifespan
    async def _close_owned_http(server: FastMCP) -> AsyncIterator[None]:
        try:
            yield None
        finally:
            if owns_http:
                await http.aclose()

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
        lifespan=_close_owned_http,
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
        return await _search_app_store(itunes, query, country=country, limit=limit)

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
        return await _get_app_store_app(
            itunes,
            app_page,
            app_id_or_url,
            country=country,
            include_page_data=include_page_data,
            include_raw=include_raw,
            warner=ctx.warning,
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
        return await _compare_app_store_apps(itunes, apps, country=country)

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
        return await _get_app_store_charts(
            charts, country=country, chart=chart, category=category, limit=limit
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
        task=_BACKGROUND_TASK,
    )
    async def get_app_store_reviews(
        app_id_or_url: str,
        country: str | None = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
        sort: ReviewSort = "most_recent",
        ctx: Context = CurrentContext(),
        progress: Progress = Progress(),
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
        await progress.set_total(100)
        reporter = DualChannelProgressReporter(ctx, progress)
        return await _get_app_store_reviews(
            reviews_client,
            app_page,
            app_id_or_url,
            country=country,
            limit=limit,
            sort=sort,
            warner=ctx.warning,
            progress=reporter,
        )

    @mcp.tool(
        annotations={
            "title": "Digest App Store reviews",
            "readOnlyHint": True,
            "openWorldHint": True,
        },
        icons=[icon],
        timeout=120.0,
        task=_BACKGROUND_TASK,
    )
    async def digest_app_store_reviews(
        app_id_or_url: str,
        country: str | None = None,
        limit: Annotated[int, Field(ge=10, le=500)] = 200,
        sort: ReviewSort = "most_recent",
        focus: str | None = None,
        ctx: Context = CurrentContext(),
        progress: Progress = Progress(),
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
        await progress.set_total(100)
        reporter = DualChannelProgressReporter(ctx, progress)
        return await _digest_app_store_reviews(
            reviews_client,
            app_page,
            app_id_or_url,
            country=country,
            limit=limit,
            sort=sort,
            focus=focus,
            sampler=ctx.sample,
            warner=ctx.warning,
            progress=reporter,
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
        task=_BACKGROUND_TASK,
    )
    async def get_app_store_screenshots(
        app_id_or_url: str,
        country: str | None = None,
        device: Literal["iphone", "ipad"] = "iphone",
        limit: Annotated[int, Field(ge=1, le=8)] = 4,
        ctx: Context = CurrentContext(),
        progress: Progress = Progress(),
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
        reporter = DualChannelProgressReporter(ctx, progress)
        result = await _get_app_store_screenshots(
            itunes,
            app_page,
            http,
            app_id_or_url,
            country=country,
            device=device,
            limit=limit,
            progress=reporter,
        )
        images = [Image(data=img.data, format=img.format) for img in result.images]
        header = (
            f"{len(images)} {result.device} screenshot(s) for app "
            f"{result.app_id} (storefront '{result.country}'), in store order:"
        )
        return ToolResult(
            content=[TextContent(type="text", text=header)]
            + [image.to_image_content() for image in images],
            structured_content={
                "app_id": result.app_id,
                "country": result.country,
                "device": result.device,
                "count": len(images),
                "urls": result.urls,
                "warnings": result.warnings,
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
    # ctx.warning(...) calls (see tools/app.py, tools/reviews.py) are only
    # mirrored to the server's own log at DEBUG if this logger is raised
    # explicitly - otherwise they're only visible to a client that's
    # listening for them. This just raises a stdlib logging.Logger's level;
    # it never touches sys.stdout, so it can't violate the constraint above.
    get_logger(name="fastmcp.server.context.to_client").setLevel(logging.DEBUG)
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
