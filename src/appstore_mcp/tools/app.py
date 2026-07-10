"""get_app_store_app: orchestration body, ctx-free."""

import asyncio
import contextlib
from typing import Any

from pydantic import ValidationError

from appstore_mcp.apple import itunes as itunes_mod
from appstore_mcp.apple import page as page_mod
from appstore_mcp.apple.ids import parse_app_ref, validate_country
from appstore_mcp.apple.itunes import ITunesClient, lookup_url
from appstore_mcp.apple.normalize import profile_from_lookup
from appstore_mcp.apple.page import AppPageClient, PageParseError, enrichment_from_html
from appstore_mcp.errors import AppNotFoundError, AppStoreMCPError
from appstore_mcp.models import GetAppResult, Meta, Source
from appstore_mcp.runtime import Warner

_DEFAULT_COUNTRY = "us"


async def _reap(task: "asyncio.Task[Any]") -> None:
    """Cancel an in-flight companion task and retrieve its outcome so the
    event loop never logs 'exception was never retrieved'."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


async def get_app_store_app(
    itunes: ITunesClient,
    app_page: AppPageClient,
    app_id_or_url: str,
    country: str | None = None,
    include_page_data: bool = True,
    include_raw: bool = False,
    warner: Warner | None = None,
) -> GetAppResult:
    """Fetch the full public App Store profile for one app by numeric ID or
    apps.apple.com URL. Page-sourced fields (subtitle, has_iap, privacy) are
    best-effort; set include_page_data=false to skip that second request.
    Set include_raw=true to also get Apple's unmodified lookup payload
    (large - only when normalized fields are not enough).

    Args:
        itunes: Client for Apple's iTunes Search/Lookup API.
        app_page: Client for the public App Store web page.
        app_id_or_url: Numeric App Store app ID (e.g. '570060128') or a
            full apps.apple.com URL.
        country: ISO 3166-1 alpha-2 storefront code, e.g. 'us', 'de', 'jp'.
            Defaults to the country in the URL if one was passed, else 'us'.
        include_page_data: Also fetch subtitle, has_iap, and privacy labels
            from the public App Store page (best-effort, one extra request).
        include_raw: Also return Apple's unmodified lookup payload under
            `raw` (large - only when normalized fields are not enough).
        warner: Callback for logging deep-fallback-failure warnings (e.g.
            `ctx.warning`). Optional - when omitted, page-enrichment
            failures still degrade with an in-band `meta.warnings` entry,
            just without the side-channel log.
    """
    ref = parse_app_ref(app_id_or_url)
    resolved_country = validate_country(country or ref.country or _DEFAULT_COUNTRY)

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
            if warner is not None:
                await warner(
                    f"page enrichment failed for app {ref.app_id}: {exc}",
                    extra={
                        "app_id": ref.app_id,
                        "country": resolved_country,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
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
