# Data sources: iTunes API primary, legacy feeds for charts/reviews, amp-api rejected

Four data sources back appstore-mcp's tools, each verified independently and
each with a different reliability tier:

1. **iTunes Search/Lookup API** — primary source for search and app
   profiles. Apple's docs say "~20 req/min" but that's a floor, not a
   ceiling: real-world reports see 300-350 req/min before 429s, with
   datacenter/hosted IPs throttled more aggressively than residential/office
   ones (see ADR-0003). Treated as reliable: its failure is a tool error,
   not a warning.
2. **Public App Store page extraction** (`page.py`) — best-effort parsing of
   the embedded-JSON (`serialized-server-data`/shoebox) blob Apple's own web
   client hydrates from. Sole source of `subtitle`, `has_iap` (the lookup
   API has no IAP field), and privacy labels, and doubles as the reviews
   fallback (below). Always fail-soft.
3. **Charts** — sourced from the *legacy* per-genre RSS feed
   (`itunes.apple.com/{cc}/rss/{chart}/limit={n}/genre={id}/json`), not the
   newer `rss.marketingtools.apple.com/api/v2/...` feed. Live-verified
   2026-07-10: the legacy feed returned current, correct, genre-filtered
   results, contradicting widely-repeated "genre RSS is dead" claims that
   actually describe the *newer* marketingtools feed (which dropped genre
   filtering and isn't CORS-open). Using the legacy feed means overall
   charts, category charts, and top-grossing all come from one endpoint
   with no auth and no page-scraping — `charts.py` never needs to depend on
   `page.py`.
4. **Reviews** — the legacy
   `itunes.apple.com/{cc}/rss/customerreviews/id={id}/sortBy={sort}/page={n}/json`
   feed, live-verified 2026-07-10 against a high-traffic app (reviews from
   the last 24h), contradicting reports that call it uniformly dead. Hard-
   capped at ~500 reviews (10 pages × 50) per storefront. When the feed
   200s with zero entries, the reviews tools fall back to the same page-
   extraction module (2) rather than a second scraper — the App Store page
   itself server-renders ~24 "most helpful" reviews per country.

## Considered options

- **`amp-api.apps.apple.com`** (the richer, properly-typed JSON Apple's own
  web client calls) — rejected: it 401s without a bearer token minted by
  client-side JS via a signature with no documented server-side
  reproduction. Not worth the fragility (would require a headless browser)
  for what it buys over page extraction.
- **`rss.marketingtools.apple.com/api/v2/...`** for charts — rejected: it
  dropped genre filtering (the exact thing category charts need) and isn't
  CORS-open, which is moot for a server-side client but doesn't buy
  anything the legacy feed doesn't already provide.
- **A second, dedicated review-scraper module for the empty-feed fallback**
  — rejected in favor of reusing `page.py`: the same embedded-JSON blob
  already carries a "most helpful" reviews shelf, so a second scraper would
  duplicate extraction logic for a rarely-hit path.
- **Charts depending on `page.py` for enrichment or fallback** (the
  original plan) — rejected once the legacy RSS feed was verified live: it
  fully covers overall, category, and top-grossing charts on its own, so
  the dependency was dropped entirely.

## Consequences

Every best-effort source above (2, 3, 4) is kept warned even where it
currently works reliably, because none of them is a documented,
contractually-stable Apple product — "subject to change" per Apple's own
framing. If Apple kills the legacy per-genre RSS feed or the customer-
reviews feed, `charts.py`/`reviews.py` are the only modules that need to
change; the fallback-to-page-extraction path for reviews should be reused
rather than duplicated if further review sources are ever added. Any future
proposal to call `amp-api.apps.apple.com` needs a real answer for
bearer-token acquisition before revisiting this rejection.
