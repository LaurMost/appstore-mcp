from typing import Any, Callable

from appstore_mcp.apple.normalize import profile_from_lookup, search_result_from_lookup


def test_search_result_from_real_lookup_payload(
    load_fixture: Callable[[str], Any],
) -> None:
    item = load_fixture("lookup_duolingo_us.json")["results"][0]
    result = search_result_from_lookup(item)
    assert result.app_id == "570060128"
    assert result.name.startswith("Duolingo")
    assert result.developer == "Duolingo"
    assert result.bundle_id == "com.duolingo.DuolingoMobile"
    assert result.url.startswith("https://apps.apple.com/us/app/")
    assert result.price == 0.0
    assert result.currency == "USD"
    assert result.rating is not None and 0 < result.rating <= 5
    assert result.rating_count is not None and result.rating_count > 100_000
    assert result.primary_genre == "Education"


def test_profile_from_real_lookup_payload(
    load_fixture: Callable[[str], Any],
) -> None:
    item = load_fixture("lookup_duolingo_us.json")["results"][0]
    profile = profile_from_lookup(item)
    assert profile.app_id == "570060128"
    assert len(profile.description) > 500
    assert profile.description_length == len(profile.description)
    assert profile.version == item["version"]
    assert profile.last_updated == item["currentVersionReleaseDate"]
    assert profile.min_os_version == item["minimumOsVersion"]
    assert profile.content_rating == "4+"
    assert "EN" in profile.languages
    assert profile.device_families[:2] == ["iphone", "ipad"]
    assert profile.icon_url and "512" in profile.icon_url
    # Apple's lookup returned no screenshots for this app; must not blow up.
    assert profile.screenshot_urls == []
    assert profile.screenshot_count == 0
    # Page-sourced fields default to null before enrichment.
    assert profile.subtitle is None
    assert profile.has_iap is None


def test_profile_tolerates_minimal_payload() -> None:
    profile = profile_from_lookup({"trackId": 1, "trackName": "X", "artistName": "Y"})
    assert profile.app_id == "1"
    assert profile.description == ""
    assert profile.genres == []
    assert profile.device_families == []
