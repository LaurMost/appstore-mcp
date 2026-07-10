"""search_app_store: orchestration body, ctx-free."""

from appstore_mcp.apple import itunes as itunes_mod
from appstore_mcp.apple.ids import validate_country
from appstore_mcp.apple.itunes import ITunesClient, search_url
from appstore_mcp.apple.normalize import search_result_from_lookup
from appstore_mcp.models import Meta, SearchAppsResult, Source


async def search_app_store(
    itunes: ITunesClient,
    query: str,
    country: str = "us",
    limit: int = 10,
) -> SearchAppsResult:
    """Search Apple App Store apps by keyword. Returns slim results
    (id, name, developer, rating, price) - use get_app_store_app for the
    full profile of a specific app.

    Args:
        itunes: Client for Apple's iTunes Search/Lookup API.
        query: Keyword(s) to search for, e.g. 'language learning'.
        country: ISO 3166-1 alpha-2 storefront code, e.g. 'us', 'de', 'jp'.
        limit: Max results to return (1-50).
    """
    country = validate_country(country)
    entry = await itunes.search(query, country=country, limit=limit)
    results = [
        search_result_from_lookup(item) for item in entry.value.get("results", [])
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
