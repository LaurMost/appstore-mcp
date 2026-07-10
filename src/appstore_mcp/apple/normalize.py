"""Normalize raw Apple payloads into our Pydantic models."""

from typing import Any

from appstore_mcp.models import AppProfile, ChartEntry, Review, SearchResult

_DEVICE_FAMILY_MARKERS = {
    "iphone": "iphone",
    "ipad": "ipad",
    "ipod": "ipod",
    "appletv": "appletv",
    "watch": "watch",
    "mac": "mac",
}


def _device_families(supported_devices: list[str]) -> list[str]:
    found: set[str] = set()
    for device in supported_devices:
        lowered = device.lower()
        for marker, family in _DEVICE_FAMILY_MARKERS.items():
            if lowered.startswith(marker):
                found.add(family)
    # Stable, meaningful order rather than set order.
    order = ["iphone", "ipad", "ipod", "appletv", "watch", "mac"]
    return [f for f in order if f in found]


def search_result_from_lookup(item: dict[str, Any]) -> SearchResult:
    return SearchResult(
        app_id=str(item["trackId"]),
        name=item.get("trackName", ""),
        developer=item.get("artistName", ""),
        bundle_id=item.get("bundleId"),
        url=item.get("trackViewUrl"),
        price=item.get("price"),
        currency=item.get("currency"),
        rating=item.get("averageUserRating"),
        rating_count=item.get("userRatingCount"),
        primary_genre=item.get("primaryGenreName"),
    )


def profile_from_lookup(item: dict[str, Any]) -> AppProfile:
    base = search_result_from_lookup(item)
    return AppProfile(
        **base.model_dump(),
        description=item.get("description", ""),
        release_notes=item.get("releaseNotes"),
        version=item.get("version"),
        release_date=item.get("releaseDate"),
        last_updated=item.get("currentVersionReleaseDate"),
        min_os_version=item.get("minimumOsVersion"),
        content_rating=item.get("contentAdvisoryRating"),
        genres=item.get("genres", []),
        languages=item.get("languageCodesISO2A", []),
        device_families=_device_families(item.get("supportedDevices", [])),
        icon_url=item.get("artworkUrl512"),
        screenshot_urls=item.get("screenshotUrls", []),
        ipad_screenshot_count=len(item.get("ipadScreenshotUrls", [])),
    )


def _label(node: Any) -> str | None:
    """Feed values look like {"label": "..."}; missing nodes are common."""
    if isinstance(node, dict):
        value = node.get("label")
        return value if isinstance(value, str) else None
    return None


def review_from_feed_entry(entry: dict[str, Any]) -> Review:
    rating_text = _label(entry.get("im:rating"))
    vote_count = _label(entry.get("im:voteCount"))
    vote_sum = _label(entry.get("im:voteSum"))
    author = entry.get("author", {})
    return Review(
        review_id=_label(entry.get("id")),
        title=_label(entry.get("title")),
        body=_label(entry.get("content")) or "",
        rating=int(rating_text) if rating_text else None,
        author=_label(author.get("name")) if isinstance(author, dict) else None,
        app_version=_label(entry.get("im:version")),
        updated_at=_label(entry.get("updated")),
        vote_count=int(vote_count) if vote_count else None,
        vote_sum=int(vote_sum) if vote_sum else None,
    )


def chart_entry_from_feed(entry: dict[str, Any], rank: int) -> ChartEntry:
    id_node = entry.get("id", {})
    attrs = id_node.get("attributes", {}) if isinstance(id_node, dict) else {}
    category = entry.get("category", {})
    category_attrs = (
        category.get("attributes", {}) if isinstance(category, dict) else {}
    )
    images = entry.get("im:image", [])
    icon = _label(images[-1]) if isinstance(images, list) and images else None
    return ChartEntry(
        rank=rank,
        app_id=str(attrs.get("im:id", "")),
        name=_label(entry.get("im:name")) or "",
        developer=_label(entry.get("im:artist")),
        bundle_id=attrs.get("im:bundleId"),
        url=_label(id_node),
        price_label=_label(entry.get("im:price")),
        category=category_attrs.get("label"),
        release_date=_label(entry.get("im:releaseDate")),
        icon_url=icon,
    )
