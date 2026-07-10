"""FastMCP server: thin tools over the apple/ clients."""

import re
from typing import Any

import httpx
from fastmcp import FastMCP

from appstore_mcp.apple.fetch import USER_AGENT
from appstore_mcp.apple.ids import parse_app_ref
from appstore_mcp.apple.itunes import LOOKUP_URL, SEARCH_URL, SOURCE_NAME, ITunesClient
from appstore_mcp.apple.normalize import profile_from_lookup, search_result_from_lookup
from appstore_mcp.errors import AppNotFoundError, AppStoreMCPError, InvalidInputError
from appstore_mcp.models import (
    AppError,
    CompareAppsResult,
    GetAppResult,
    Meta,
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
        include_raw: bool = False,
    ) -> GetAppResult:
        """Fetch the full public App Store profile for one app by numeric ID or
        apps.apple.com URL. Set include_raw=true to also get Apple's unmodified
        lookup payload (large - only when normalized fields are not enough)."""
        ref = parse_app_ref(app_id_or_url)
        resolved_country = _validate_country(country or ref.country or DEFAULT_COUNTRY)
        entry = await itunes.lookup([ref.app_id], country=resolved_country)
        items: list[dict[str, Any]] = entry.value.get("results", [])
        if not items:
            raise AppNotFoundError(ref.app_id, resolved_country)
        item = items[0]
        profile = profile_from_lookup(item)
        url = str(httpx.URL(LOOKUP_URL, params={"id": ref.app_id, "country": resolved_country}))
        return GetAppResult(
            meta=Meta(
                country=resolved_country,
                retrieved_at=entry.retrieved_at,
                fresh=entry.fresh,
            ),
            app=profile,
            sources=[Source(name=SOURCE_NAME, url=url, retrieved_at=entry.retrieved_at)],
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

    return mcp


def main() -> None:
    create_server().run()


if __name__ == "__main__":
    main()
