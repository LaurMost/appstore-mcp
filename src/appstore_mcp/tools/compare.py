"""compare_app_store_apps: orchestration body, ctx-free."""

from appstore_mcp.apple import itunes as itunes_mod
from appstore_mcp.apple.ids import parse_app_ref, validate_country
from appstore_mcp.apple.itunes import ITunesClient, lookup_url
from appstore_mcp.apple.normalize import profile_from_lookup
from appstore_mcp.errors import AppStoreMCPError, InvalidInputError
from appstore_mcp.models import AppError, CompareAppsResult, Meta, Source


async def compare_app_store_apps(
    itunes: ITunesClient,
    apps: list[str],
    country: str = "us",
) -> CompareAppsResult:
    """Fetch full profiles for multiple apps (IDs or apps.apple.com URLs) in
    one batch for side-by-side competitor comparison. Returns the profiles
    plus per-app errors; apps that fail do not fail the whole call.

    Args:
        itunes: Client for Apple's iTunes Search/Lookup API.
        apps: App IDs or apps.apple.com URLs to compare, e.g.
            ['570060128', 'https://apps.apple.com/us/app/babbel/id829587759'].
        country: ISO 3166-1 alpha-2 storefront all apps are compared on,
            e.g. 'us', 'de', 'jp'. One call always uses a single storefront.
    """
    country = validate_country(country)
    if not apps:
        raise InvalidInputError("Pass at least one app ID or App Store URL in `apps`.")

    errors: list[AppError] = []
    ordered_ids: list[str] = []
    original_ref: dict[str, str] = {}  # app_id -> value the caller sent
    for value in apps:
        try:
            ref = parse_app_ref(value)
        except InvalidInputError as exc:
            errors.append(AppError(app=value, reason=str(exc)))
            continue
        if ref.app_id not in ordered_ids:
            ordered_ids.append(ref.app_id)
            original_ref[ref.app_id] = value

    if not ordered_ids:
        raise InvalidInputError(
            "None of the provided values could be parsed as an app ID or "
            "App Store URL."
        )

    entry = await itunes.lookup(ordered_ids, country=country)
    by_id = {
        str(item.get("trackId")): item for item in entry.value.get("results", [])
    }
    profiles = []
    for app_id in ordered_ids:
        item = by_id.get(app_id)
        if item is None:
            errors.append(
                AppError(
                    app=original_ref[app_id],
                    reason=f"app {app_id} not found in storefront '{country}' "
                    f"- it may exist in another country",
                )
            )
            continue
        profiles.append(profile_from_lookup(item))

    if not profiles:
        raise AppStoreMCPError(
            f"None of the requested apps could be fetched in storefront "
            f"'{country}': " + "; ".join(f"{e.app}: {e.reason}" for e in errors)
        )

    warnings = (
        [
            f"{len(errors)} of {len(apps)} requested apps could not be "
            f"fetched; see errors"
        ]
        if errors
        else []
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
        sources=[
            Source(
                name=itunes_mod.SOURCE_NAME,
                url=lookup_url(ordered_ids, country=country),
                retrieved_at=entry.retrieved_at,
            )
        ],
    )
