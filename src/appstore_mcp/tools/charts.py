"""get_app_store_charts: orchestration body, ctx-free."""

from appstore_mcp.apple import charts as charts_mod
from appstore_mcp.apple.charts import (
    ChartsClient,
    chart_url,
    entries_from_chart_feed,
    resolve_genre_id,
)
from appstore_mcp.apple.ids import validate_country
from appstore_mcp.apple.normalize import chart_entry_from_feed
from appstore_mcp.models import ChartName, ChartsResult, Meta, Source


async def get_app_store_charts(
    charts_client: ChartsClient,
    country: str = "us",
    chart: ChartName = "top-free",
    category: str | None = None,
    limit: int = 50,
) -> ChartsResult:
    """Fetch ranked top-chart apps for a storefront. Best-effort: sourced
    from an undocumented Apple RSS feed.

    Args:
        charts_client: Client for Apple's legacy per-genre iTunes RSS charts.
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
    country = validate_country(country)
    genre_id = resolve_genre_id(category) if category else None
    entry = await charts_client.fetch(
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
