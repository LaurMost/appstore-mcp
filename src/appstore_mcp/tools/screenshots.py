"""get_app_store_screenshots: orchestration body, ctx-free except for
progress reporting."""

from dataclasses import dataclass
from typing import Literal

import httpx

from appstore_mcp.apple.ids import parse_app_ref, validate_country
from appstore_mcp.apple.itunes import ITunesClient
from appstore_mcp.apple.page import AppPageClient
from appstore_mcp.apple.screenshots import FetchedImage, fetch_screenshots
from appstore_mcp.runtime import ProgressReporter

_DEFAULT_COUNTRY = "us"


@dataclass(frozen=True)
class ScreenshotsResult:
    app_id: str
    country: str
    device: Literal["iphone", "ipad"]
    images: list[FetchedImage]
    urls: list[str]
    warnings: list[str]


async def get_app_store_screenshots(
    itunes: ITunesClient,
    app_page: AppPageClient,
    http: httpx.AsyncClient,
    app_id_or_url: str,
    country: str | None = None,
    device: Literal["iphone", "ipad"] = "iphone",
    limit: int = 4,
    progress: ProgressReporter | None = None,
) -> ScreenshotsResult:
    """Fetch an app's App Store screenshot images (URL resolution +
    page-template fallback + concurrent download, via
    `apple.screenshots.fetch_screenshots`).

    Args:
        itunes: Client for Apple's iTunes Search/Lookup API.
        app_page: Client for the public App Store web page (screenshot URL
            fallback when the lookup API omits them).
        http: Client used to download the resolved screenshot images.
        app_id_or_url: Numeric App Store app ID or a full apps.apple.com URL.
        country: ISO 3166-1 alpha-2 storefront code, e.g. 'us', 'de', 'jp'.
            Defaults to the country in the URL if one was passed, else 'us'.
        device: Which screenshot set to fetch: 'iphone' or 'ipad'.
        limit: Max screenshots to return.
        progress: Reporter for download progress. `fetch_screenshots`
            resolves URLs and downloads images in one call with no
            total-count known ahead of time, so this reports a single 1.0
            fraction once every download has completed, rather than one
            tick per download.
    """
    ref = parse_app_ref(app_id_or_url)
    resolved_country = validate_country(country or ref.country or _DEFAULT_COUNTRY)

    fetch = await fetch_screenshots(
        itunes,
        app_page,
        http,
        ref.app_id,
        country=resolved_country,
        device=device,
        limit=limit,
    )

    if progress is not None:
        await progress.report(1.0, f"{len(fetch.images)} screenshot(s) downloaded")

    return ScreenshotsResult(
        app_id=ref.app_id,
        country=resolved_country,
        device=device,
        images=fetch.images,
        urls=fetch.urls,
        warnings=fetch.warnings,
    )
