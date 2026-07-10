"""get_app_store_reviews / digest_app_store_reviews: orchestration bodies.

`harvest_reviews` is the shared pagination+fallback helper both tools call
(moved from server.py's `_collect_reviews`/`_ReviewCollection`, renamed public
since it now crosses a module boundary). It lives here rather than in
`apple/reviews.py` because it orchestrates across two clients (the review
feed and the page fallback), mirroring how tools/app.py orchestrates
itunes+page.
"""

from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError

from appstore_mcp import digest as digest_mod
from appstore_mcp.apple import page as page_mod
from appstore_mcp.apple import reviews as reviews_mod
from appstore_mcp.apple.ids import parse_app_ref, validate_country
from appstore_mcp.apple.normalize import review_from_feed_entry
from appstore_mcp.apple.page import AppPageClient, PageParseError, reviews_from_html
from appstore_mcp.apple.reviews import (
    MAX_FEED_PAGES,
    ReviewsClient,
    ReviewSort,
    entries_from_feed,
    review_feed_url,
)
from appstore_mcp.errors import AppStoreMCPError
from appstore_mcp.models import DigestReviewsResult, Meta, Review, ReviewsResult, Source
from appstore_mcp.runtime import ProgressReporter, Sampler, Warner


@dataclass
class ReviewCollection:
    reviews: list[Review]
    sources: list[Source]
    warnings: list[str]
    retrieved_at: datetime
    fresh: bool


async def harvest_reviews(
    reviews_client: ReviewsClient,
    app_page: AppPageClient,
    app_id: str,
    *,
    country: str,
    sort: ReviewSort,
    limit: int,
    warner: Warner | None = None,
    progress: ProgressReporter | None = None,
    progress_start: float = 0,
    progress_end: float = 100,
) -> ReviewCollection:
    """Shared review harvesting: paginated feed, page fallback on empty.

    `progress_start`/`progress_end` are percentage points (0-100), matching
    this function's own pre-refactor call sites (`digest_app_store_reviews`
    reserves 0-50 for this stage, leaving 50-100 for LLM digestion). Every
    tick here computes its fraction the same way the original `_tick`
    closure did - `page_number / MAX_FEED_PAGES`, etc. - then scales it into
    [progress_start, progress_end] before handing it to `progress.report`,
    whose own `fraction` parameter is a bare 0.0-1.0 of the *caller's*
    already-scoped reporter (see runtime.ProgressReporter). A terminal tick
    at `progress_end` is always emitted before returning, regardless of
    which exit path below is taken (full MAX_FEED_PAGES pages, an early
    break once `limit` is satisfied, or the page fallback succeeding/
    failing) - otherwise a client's progress indicator can stall with no
    "done" signal for this stage.
    """
    span = progress_end - progress_start

    async def _tick(fraction: float, message: str | None = None) -> None:
        if progress is not None:
            absolute = progress_start + span * fraction
            await progress.report(absolute / 100, message)

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
        await _tick(
            page_number / MAX_FEED_PAGES,
            f"Fetched review page {page_number}/{MAX_FEED_PAGES}",
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
            if warner is not None:
                await warner(
                    f"review page fallback also failed for app {app_id}: {exc}",
                    extra={
                        "app_id": app_id,
                        "country": country,
                        "sort": sort,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
                )

    # stage done regardless of which exit path was taken
    await _tick(1.0, "Review collection complete")

    return ReviewCollection(
        reviews=collected,
        sources=sources,
        warnings=warnings,
        retrieved_at=retrieved_at,
        fresh=fresh,
    )


async def get_app_store_reviews(
    reviews_client: ReviewsClient,
    app_page: AppPageClient,
    app_id_or_url: str,
    country: str | None = None,
    limit: int = 50,
    sort: ReviewSort = "most_recent",
    warner: Warner | None = None,
    progress: ProgressReporter | None = None,
) -> ReviewsResult:
    """Fetch recent public customer reviews for an app. Best-effort:
    sourced from an undocumented Apple feed capped at ~500 reviews per
    storefront, with a small page-sourced fallback when the feed is
    empty. Reviews are per-country.

    Args:
        reviews_client: Client for Apple's undocumented review RSS feed.
        app_page: Client for the public App Store page (fallback source).
        app_id_or_url: Numeric App Store app ID or a full apps.apple.com
            URL.
        country: ISO 3166-1 alpha-2 storefront code, e.g. 'us', 'de', 'jp'.
            Defaults to the country in the URL if one was passed, else
            'us'.
        limit: Max reviews to return (1-500; Apple caps the underlying
            feed at ~500 per storefront regardless of this value).
        sort: 'most_recent' or 'most_helpful'.
        warner: Optional sink for non-fatal fallback-failure diagnostics.
        progress: Optional progress sink, scoped to this call's full 0-100
            range.
    """
    ref = parse_app_ref(app_id_or_url)
    resolved_country = validate_country(country or ref.country or "us")
    collection = await harvest_reviews(
        reviews_client,
        app_page,
        ref.app_id,
        country=resolved_country,
        sort=sort,
        limit=limit,
        warner=warner,
        progress=progress,
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


async def digest_app_store_reviews(
    reviews_client: ReviewsClient,
    app_page: AppPageClient,
    app_id_or_url: str,
    country: str | None = None,
    limit: int = 200,
    sort: ReviewSort = "most_recent",
    focus: str | None = None,
    sampler: Sampler | None = None,
    warner: Warner | None = None,
    progress: ProgressReporter | None = None,
) -> DigestReviewsResult:
    """Fetch up to `limit` reviews and compress them into a structured
    digest (themes, complaints, praise, sentiment) via MCP sampling, so
    hundreds of reviews never enter the caller's context. Works across
    storefront languages - the digest is always English. Requires a
    `sampler` (or raises an agent-facing error explaining why).

    Args:
        reviews_client: Client for Apple's undocumented review RSS feed.
        app_page: Client for the public App Store page (fallback source).
        app_id_or_url: Numeric App Store app ID or a full apps.apple.com
            URL.
        country: ISO 3166-1 alpha-2 storefront code, e.g. 'us', 'de', 'jp'.
            Defaults to the country in the URL if one was passed, else 'us'.
        limit: Max reviews to digest (10-500).
        sort: 'most_recent' or 'most_helpful'.
        focus: Optional steer for the digest, e.g. 'pricing complaints'
            or 'onboarding friction'.
        sampler: LLM sampling callable (e.g. `ctx.sample`); required.
        warner: Optional sink for non-fatal fallback-failure diagnostics.
        progress: Optional progress sink, scoped to this call's full 0-100
            range - reviews get 0-50, digestion gets 50-100 (reserving the
            second half for the often-slower LLM sampling call, so a client
            watching progress sees continuous movement across both stages).
    """
    ref = parse_app_ref(app_id_or_url)
    resolved_country = validate_country(country or ref.country or "us")
    # Review harvesting gets the first half of the progress range; LLM
    # digestion (below) gets the second half.
    collection = await harvest_reviews(
        reviews_client,
        app_page,
        ref.app_id,
        country=resolved_country,
        sort=sort,
        limit=limit,
        warner=warner,
        progress=progress,
        progress_end=50,
    )
    if not collection.reviews:
        raise AppStoreMCPError(
            f"No reviews available to digest for app {ref.app_id} in "
            f"storefront '{resolved_country}'. Try another country or "
            f"check the app ID with get_app_store_app."
        )

    prompt = digest_mod.build_digest_prompt(
        ref.app_id, resolved_country, collection.reviews, focus
    )
    if sampler is None:
        raise AppStoreMCPError(
            "Review digestion needs LLM sampling, which this MCP client "
            "does not support. Either use get_app_store_reviews to fetch "
            "the raw reviews, or run the server with an "
            "ANTHROPIC_API_KEY/OPENAI_API_KEY and the matching optional "
            "dependency installed to enable the server-side fallback."
        )
    if progress is not None:
        # Same percentage harvest_reviews's terminal tick above already
        # reported (progress_end=50 -> fraction 0.5) - just an updated
        # message, no further advancement, matching today's bare
        # set_message call between the two stages.
        await progress.report(0.5, "Digesting reviews via LLM sampling")
    try:
        digest, retry_warnings = await digest_mod.digest_with_retry(sampler, prompt)
    except (ValueError, ValidationError):
        # A second invalid-JSON parse failure after the one built-in retry:
        # propagate uncaught, matching today's "no infinite retry" behavior.
        raise
    except Exception as exc:
        raise AppStoreMCPError(
            f"Review digestion needs LLM sampling, which this MCP client "
            f"does not support (or it failed: {exc}). Either use "
            f"get_app_store_reviews to fetch the raw reviews, or run the "
            f"server with an ANTHROPIC_API_KEY/OPENAI_API_KEY and the "
            f"matching optional dependency installed to enable the "
            f"server-side fallback."
        ) from exc

    warnings = list(collection.warnings) + retry_warnings
    if progress is not None:
        if retry_warnings:
            await progress.report(0.75, "Retrying after invalid LLM output")
        await progress.report(1.0, "Digest complete")
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
