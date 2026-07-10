# appstore-mcp — v1 Design Plan

An open-source MCP server for live Apple App Store competitor research. AI agents
search apps, fetch public metadata, compare competitors, and pull charts/reviews as
structured JSON. Public data only — no Apple developer account, no downloads/revenue
estimates, no database, no SaaS.

**Headline demo:** "Compare Duolingo, Babbel, and Busuu on the US App Store."

Not affiliated with or endorsed by Apple.

---

## Decisions (settled in design review, 2026-07-10)

### Language & stack
- **Python 3.12**, managed with `uv`.
- **`fastmcp>=3`** (jlowin's actively developed package — *not* the frozen FastMCP
  bundled in the official `mcp` SDK; drop the current `mcp[cli]` dependency).
- `httpx` for all fetching. No HTML-parser dependency unless embedded-JSON
  extraction turns out to be insufficient.
- Pydantic models for all tool inputs/outputs — FastMCP derives `outputSchema` and
  `structuredContent` from typed returns for free.

### Transport & distribution
- **stdio only in v1**, published to PyPI, run via `uvx appstore-mcp`.
- Rationale: zero infra, no auth needed, and each user hits Apple from their own IP
  (a hosted server would concentrate all traffic against Apple's ~20 req/min limit).
- Code stays transport-agnostic; `mcp.run(transport="streamable-http")` remains a
  one-line self-host option. No hosted mode shipped.

### Tool surface — 5 tools, all read-only
1. `search_app_store(query, country="us", limit=10)` → list of `SearchResult`
2. `get_app_store_app(app_id_or_url, country="us", include_page_data=True, include_raw=False)` → `AppProfile`
3. `compare_app_store_apps(apps: list[str], country="us")` → list of `AppProfile` + per-app errors
4. `get_app_store_reviews(app_id_or_url, country="us", limit=50, sort="most_recent")` → list of `Review`
5. `get_app_store_charts(country="us", chart="top-free", category=None, limit=50)` → list of `ChartEntry`

Design rules:
- `compare_app_store_apps` is a **pure batch-fetcher**: concurrent fetch (iTunes
  lookup supports multi-ID calls), profiles side by side, cheap facts only
  (`description_length`, `screenshot_count`). **No analysis object** — no
  `highestRated`/`positioningSignals`/verdicts. Analysis is the LLM's job.
- Reviews are a **separate tool**, not a flag on `get_app_store_app` — different
  (unofficial) source, different failure modes, heavy token cost.
- Every tool carries annotations: `title`, `readOnlyHint: true`,
  `openWorldHint: true` (required by Anthropic review criteria; read-only lets
  Claude auto-run without per-call confirmation).
- Server-level `instructions`: public-data-only scope, no downloads/revenue,
  **apps are per-storefront — pass `country`**.

### Response shape — compact by default, raw opt-in
The original "raw first, compact second" plan is **inverted**: raw iTunes blobs
(200+ `supportedDevices` strings, artwork variants, wrapper cruft) waste the
consumer's context window.

Envelope (no `compact` wrapper key — the normalized shape *is* the body):

```python
class GetAppResult(BaseModel):
    meta: Meta                 # store, country, language, retrieved_at, fresh, warnings
    app: AppProfile
    sources: list[Source]      # name, url, retrieved_at per source used
    raw: dict | None = None    # only when include_raw=True (get_app_store_app only)
```

Analogous: `results` for search, `apps` + `errors` for compare, `reviews`,
`entries` for charts.

Two schemas:
- **`SearchResult`** (slim): app_id, name, developer, bundle_id, url, price,
  currency, rating, rating_count, primary_genre. No description/screenshots.
- **`AppProfile`** (full): SearchResult fields + full `description` and
  `release_notes` (never truncated — they're the substance of competitor research),
  version, release_date, last_updated, min_os_version, content_rating, genres,
  languages, `icon_url` (512 only), `screenshot_urls` (iPhone set) +
  `ipad_screenshot_count`, `device_families` (derived), and page-sourced fields
  below. Deliberately dropped: `supportedDevices`, artwork size variants,
  `features`, GUID cruft — that's what `include_raw` is for.

### Data sources (fragility gradient)
1. **iTunes Search/Lookup API** — primary, reliable. Apple's docs say
   "~20 req/min" but that's a floor, not a ceiling: real-world reports see
   300-350 req/min before 429s kick in, and datacenter/hosted IPs get throttled
   far more aggressively than residential/office IPs — another point in favor
   of stdio-only, run-from-the-user's-machine distribution (see Transport).
2. **Public App Store page extraction** (`page.py`) — best-effort embedded-JSON
   extraction (the `serialized-server-data` / shoebox blob Apple's own web
   client hydrates from). Sole source of `subtitle`, `has_iap` (lookup API has
   no IAP field), privacy labels, **and the reviews fallback below**. Default
   **on** (`include_page_data=True`) but **fail-soft**: page failure never
   fails the tool — fields come back null + `meta.warnings` entry. Runs
   concurrently with the lookup.
   - Evaluated and rejected: `amp-api.apps.apple.com` (the richer, properly-
     typed JSON the web client actually calls). It 401s without a bearer
     token, and that token is minted by a signature the client-side JS
     computes — there's no documented way to reproduce it server-side without
     running a headless browser. Not worth the fragility for what it buys.
3. **Charts** — **live-verified 2026-07-10**: the "legacy" per-genre RSS feed
   (`itunes.apple.com/{cc}/rss/{chart}/limit={n}/genre={id}/json`, chart ∈
   `topfreeapplications`/`toppaidapplications`/`topgrossingapplications`) is
   *not* dead — it returned current, correct, genre-filtered results just now.
   Widely-repeated "genre RSS is dead" claims conflate it with the *newer*
   `rss.marketingtools.apple.com/api/v2/...` feed, which is the one that
   dropped genre filtering (and isn't CORS-open, i.e. server-side only — moot
   for us). **Use the legacy feed as the sole chart source** — overall charts
   and category charts and top-grossing all come from the same endpoint, no
   auth, no page-scraping. This drops the planned dependency of `charts.py` on
   `page.py` entirely. Keep it best-effort/warned anyway since Apple calls it
   "subject to change" and it's not a documented product.
4. **Reviews** — **live-verified 2026-07-10**: the legacy
   `itunes.apple.com/{cc}/rss/customerreviews/id={id}/sortBy={sort}/page={n}/json`
   feed returned reviews from the last 24h for a high-traffic app, so it is
   *not* uniformly dead as some 2026 scraper write-ups claim (those likely hit
   a stale client or an edge case, not a dead endpoint) — still verify
   empirically per-app/country during Phase 5, since Apple gives no uptime
   guarantee. Hard cap ~500 reviews (10 pages × 50) per country. **Plan B**
   (now concrete, not hypothetical): if the feed 200s with zero entries for a
   given app/country, fall back to the same `serialized-server-data` blob
   `page.py` already parses — the App Store page itself server-renders ~24
   "most helpful" reviews per country. Reuse `page.py`, don't add a second
   scraper module for it.

### Failure semantics (two tiers)
- **Tool error** (raised exception → `isError`): core promise broken — app not
  found, invalid input, rate-limited/unreachable. Messages written for agent
  recovery, e.g. not-found: *"App X not found in storefront 'de'. Apps are
  per-country — try country='us' or search_app_store."*
- **In-band degradation** (success + `warnings` / per-item `errors`): page
  enrichment failed, one app of five failed in compare (return the other four),
  empty review feed, category-chart parse broke.
- `compare_app_store_apps` raises only if *every* app fails or input is invalid.

### Caching & politeness
- In-memory async-safe TTL cache (~15 min), keyed `(endpoint, id/query, country)`,
  across all sources. Kills agent-loop refetches; no disk, no Redis.
- No proactive throttle. Apple 403/429 → structured "rate limited, retry shortly"
  error. Honest `User-Agent` identifying the tool. `meta.retrieved_at` = actual
  fetch time; `meta.fresh = False` on cache hits.

### Prompts & resources
- **One MCP prompt**: `compare_competitors(apps, country)` — expands to the
  headline demo instruction. No other prompts, no resources.

### Testing
1. **Fixture-based unit tests** (bulk): real recorded responses in
   `tests/fixtures/` (lookup JSON, search JSON, saved App Store HTML, charts page,
   RSS, review feed). All normalize/parse logic tested offline. No hand-written
   httpx mocks — fixtures test Apple's shapes, mocks test our assumptions.
2. **Live smoke suite**: `@pytest.mark.live`, excluded by default
   (`-m "not live"`), ~6 structural-invariant tests.
3. **Weekly scheduled CI run** of the live suite (GitHub Actions cron) — detects
   Apple format drift before users do.
- Use fastmcp's in-memory client to exercise tools without a subprocess.
- Before release: exercise every tool in MCP Inspector.

### Project structure
```
src/appstore_mcp/
  server.py        # FastMCP instance + 5 thin @mcp.tool functions + 1 prompt
  models.py        # Meta, Source, AppProfile, SearchResult, Review, ChartEntry,
                   #   per-tool result models
  errors.py        # error types + agent-facing messages
  cache.py         # TTL cache
  apple/
    itunes.py      # search + lookup client
    page.py        # shared page-fetch + embedded-JSON extraction
    charts.py      # legacy per-genre RSS (overall + category + top-grossing)
    reviews.py     # legacy review feed client; falls back to page.py on empty
    normalize.py   # raw payloads -> Pydantic models
    ids.py         # app ID / URL parsing
tests/
  fixtures/
  test_*.py, test_live.py
```
No `tools/` package — five thin tools live in `server.py`; logic lives in
`apple/`, testable without MCP machinery. Delete root `main.py`; entry point is
`[project.scripts] appstore-mcp = "appstore_mcp.server:main"`.

### Build phases (each ends shippable; fragility runs downhill)
1. **Foundation + core lookup** — src layout, dep swap to fastmcp, models, ids,
   itunes client, cache, errors; `search_app_store` + `get_app_store_app`
   (lookup-only), annotations, instructions, fixture tests.
2. **Compare** — batch fetch, multi-ID lookup, partial-failure semantics.
3. **Page enrichment** — `page.py`, fail-soft merge of subtitle/has_iap/privacy.
4. **Charts** — legacy per-genre RSS only; no page-scraping dependency needed.
5. **Reviews** — legacy feed, verified empirically; page.py fallback on empty.
6. **Polish + release** — `compare_competitors` prompt, weekly live CI,
   MCP Inspector pass, README, publish to PyPI.

### Identity & release
- Name **`appstore-mcp`** (verified free on PyPI 2026-07-10). License **MIT**.
- README: positioning per original plan + `uvx` config snippet for
  Claude Code/Desktop/Cursor + honest-limitations section (no downloads/revenue,
  per-storefront data, page-parsed fields best-effort) + "not affiliated with or
  endorsed by Apple."

```json
{
  "mcpServers": {
    "appstore": { "command": "uvx", "args": ["appstore-mcp"] }
  }
}
```

### Explicitly out of scope for v1
Google Play · download/revenue estimates · App Store Connect data · historical
tracking · paid providers · browser automation · hosted/HTTP mode · dashboard ·
database.
