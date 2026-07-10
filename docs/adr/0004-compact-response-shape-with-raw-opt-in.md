# Compact-by-default tool responses, raw opt-in

Early design planned "raw first, compact second" — returning Apple's iTunes
payload as the default response and layering a normalized view on top
later. That was inverted before v1 shipped: Apple's raw lookup payload
carries 200+ `supportedDevices` strings, artwork size variants, and other
wrapper cruft that burns a caller's context window for no benefit. Every
tool now returns a normalized, compact shape by default; `get_app_store_app`
accepts `include_raw=True` to also return Apple's unmodified payload under
`raw`, for the rare case where a normalized field isn't enough.

Two schemas capture the compact/full split: `SearchResult` (slim: id, name,
developer, bundle_id, url, price, currency, rating, rating_count,
primary_genre) for `search_app_store`, and `AppProfile` (SearchResult fields
plus the full `description`/`release_notes` — never truncated, since they're
the substance of competitor research — version, dates, screenshots, and
page-sourced enrichment fields) everywhere a full profile is needed.
Deliberately dropped from `AppProfile`: `supportedDevices`, artwork size
variants, `features`, GUID cruft — `include_raw` exists precisely for
callers who need those.

The same reasoning keeps reviews out of `get_app_store_app` entirely:
`get_app_store_reviews` is a separate tool, not a flag, because reviews come
from a different (unofficial) source, fail differently, and are heavy on
tokens — bundling them would force every profile fetch to pay that cost.
The same instinct dropped the `comparison`/analysis object from
`compare_app_store_apps` (see ADR-0001): the tool returns profiles side by
side plus cheap derived facts (`description_length`, `screenshot_count`),
never a verdict — interpretation is the calling LLM's job, not tool code's.

## Considered options

- **Raw iTunes payload as the default response body** (the original plan) —
  rejected: wastes the caller's context window on wrapper fields
  (`supportedDevices`, artwork variants) essentially no caller needs by
  default.
- **One schema for both search and full-profile results** — rejected:
  search results are returned in bulk (up to 50 per call) and don't need
  description/screenshots/dates; forcing `AppProfile`'s full shape onto
  every search result would multiply token cost for no benefit.
- **A `comparison`/analysis object bundled into `compare_app_store_apps`** —
  rejected (extends ADR-0001): the calling LLM already receives every
  compared profile and is better positioned to interpret them than a
  second, nested analysis step.
- **A `reviews` flag on `get_app_store_app`** — rejected: different source,
  different failure mode, and reviews are token-heavy enough that most
  profile fetches shouldn't pay for them.

## Consequences

`include_raw=True` is the only escape hatch back to Apple's original
payload, and it only exists on `get_app_store_app`. Any new tool or field
added later should default to the compact/normalized shape and require an
explicit opt-in for anything token-heavy or rarely needed, rather than the
reverse.
