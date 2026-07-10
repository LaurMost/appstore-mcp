"""Screenshot URL resolution and concurrent image download.

The lookup API's `screenshotUrls`/`ipadScreenshotUrls` are the primary
source; some apps (e.g. Duolingo) omit them entirely, so we fall back to the
public App Store page's `product_media_*` shelf templates (see
`apple.page.screenshot_urls_from_html`). Once URLs are resolved, images are
downloaded concurrently - a failed download only produces a warning, never a
hard failure, unless every single download fails.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from appstore_mcp.apple.itunes import ITunesClient
from appstore_mcp.apple.page import (
    AppPageClient,
    PageParseError,
    screenshot_urls_from_html,
)
from appstore_mcp.errors import AppNotFoundError, AppStoreMCPError


@dataclass(frozen=True)
class FetchedImage:
    data: bytes
    format: str


@dataclass(frozen=True)
class ScreenshotFetch:
    images: list[FetchedImage] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


async def fetch_screenshots(
    itunes: ITunesClient,
    app_page: AppPageClient,
    http: httpx.AsyncClient,
    app_id: str,
    *,
    country: str,
    device: Literal["iphone", "ipad"],
    limit: int,
    on_download: Callable[[], Awaitable[None]] | None = None,
) -> ScreenshotFetch:
    """Resolve an app's screenshot URLs and download them concurrently.

    Raises:
        AppNotFoundError: the lookup API has no listing for `app_id` in
            `country`.
        AppStoreMCPError: no screenshot URLs could be resolved at all (both
            the lookup API and the page fallback came up empty, or the page
            fallback itself failed), or every resolved URL failed to
            download.
    """
    warnings: list[str] = []

    entry = await itunes.lookup([app_id], country=country)
    items: list[dict[str, Any]] = entry.value.get("results", [])
    if not items:
        raise AppNotFoundError(app_id, country)

    key = "screenshotUrls" if device == "iphone" else "ipadScreenshotUrls"
    urls: list[str] = list(items[0].get(key) or [])

    if not urls:
        # The lookup API sometimes omits screenshots entirely; the public
        # page's media shelves carry resizable artwork templates.
        try:
            page_entry = await app_page.fetch_html(app_id, country=country)
            urls = screenshot_urls_from_html(page_entry.value, device)
            warnings.append(
                "screenshots sourced from the public App Store page "
                "(lookup API returned none)"
            )
        except (AppStoreMCPError, PageParseError) as exc:
            raise AppStoreMCPError(
                f"No {device} screenshots available for app {app_id} "
                f"in storefront '{country}' (page fallback failed: {exc})."
            ) from exc

    if not urls:
        raise AppStoreMCPError(
            f"App {app_id} has no {device} screenshots in storefront "
            f"'{country}'. Try device='ipad' or another country."
        )

    urls = urls[:limit]

    async def fetch_one(url: str) -> FetchedImage:
        try:
            response = await http.get(url, follow_redirects=True)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            fmt = "png" if "png" in content_type or url.endswith(".png") else "jpeg"
            return FetchedImage(data=response.content, format=fmt)
        finally:
            if on_download is not None:
                await on_download()

    downloads = await asyncio.gather(
        *(fetch_one(url) for url in urls), return_exceptions=True
    )

    images: list[FetchedImage] = []
    fetched_urls: list[str] = []
    for url, result in zip(urls, downloads, strict=False):
        if isinstance(result, BaseException):
            warnings.append(f"failed to fetch {url}: {result}")
            continue
        images.append(result)
        fetched_urls.append(url)

    if not images:
        raise AppStoreMCPError(
            f"All {len(urls)} screenshot downloads failed for app "
            f"{app_id}: {'; '.join(warnings)}"
        )

    return ScreenshotFetch(images=images, urls=fetched_urls, warnings=warnings)
