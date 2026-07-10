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
  `idempotentHint: true` (no side effects, repeated calls are safe),
  `openWorldHint: true` (required by Anthropic review criteria; read-only lets
  Claude auto-run without per-call confirmation).
- Every tool has an explicit `timeout` (25-30s) so a slow/hanging upstream
  fails with a clean MCP error instead of an unbounded wait — matters most for
  `get_app_store_reviews`, which can chain up to 10 sequential feed requests.
- Numeric `limit` params are `Annotated[int, Field(ge=1, le=N)]`, not manual
  clamping — out-of-range values are rejected with a clear validation error
  and the real bound is visible in the tool's JSON schema, instead of being
  silently truncated with no feedback.
- Every tool docstring has a Google-style `Args:` section so FastMCP (3.2.4+)
  generates a per-parameter description in the schema, not just a single
  whole-function description.
- Server-level `instructions`: public-data-only scope, no downloads/revenue,
  **apps are per-storefront — pass `country`**.
- Server-level `website_url` points at the GitHub repo (`[project.urls].Homepage`
  in `pyproject.toml`, kept in sync manually).
- Server, every tool, and the one prompt all share a single `icons=[Icon(...)]`
  (FastMCP 2.13.0+/`mcp.types.Icon`) — one brand icon reused everywhere, not
  distinct icons per tool, since this is one small server rather than a
  multi-product suite. The icon lives at `src/appstore_mcp/assets/icon.svg`
  (packaged by hatchling automatically — verified via `uv build --wheel`) and
  is loaded once in `create_server()` via `_load_icon()`, which embeds it as a
  `data:image/svg+xml;base64,...` URI rather than a hosted URL — this project
  ships stdio-only via `uvx` with no domain of its own to host a static asset
  on, so a data URI (not a `https://` `src`) is the correct fit per FastMCP's
  Icons docs.

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

### FastMCP `Context` usage (reviewed 2026-07-10 against FastMCP docs)
- **Adopted**: `ctx: Context = CurrentContext()` (v2.14+ preferred injection
  style) on `get_app_store_app`, `get_app_store_reviews`, and
  `digest_app_store_reviews` only, for two narrow uses:
  - `ctx.report_progress` inside the shared `_collect_reviews` helper's
    up-to-10-page sequential feed loop — the one real long-running,
    multi-step operation in the server. Progress is reported as a
    `progress_start`/`progress_end`-scaled percentage of `total=100` (not raw
    page counts), with a terminal tick at `progress_end` guaranteed on every
    exit path (full pages, early break once `limit` is satisfied, or the
    page-fallback succeeding/failing) so a client's progress indicator never
    stalls below 100% with no "done" signal. `get_app_store_reviews` uses the
    full 0-100 range; `digest_app_store_reviews` reserves 0-50 for review
    harvesting and 50-100 for its LLM sampling stage (with a 75% tick before
    the one-shot invalid-output retry) — this keeps progress moving
    continuously across both stages instead of going silent during the
    often-slower sampling call, under its 120s tool timeout.
  - `ctx.warning` alongside (not instead of) the existing `meta.warnings`
    string at the two deepest fallback points (page-enrichment failure in
    `get_app_store_app`; page-fallback-also-failed in `_collect_reviews`)
    — cheap extra observability for anyone tailing client-side logs, on top
    of the structured warning the response already carries. Both calls pass
    `extra={app_id, country, error_type, error_message, ...}` (reviewed
    2026-07-10 against the FastMCP "Client Logging" docs) so the structured
    fields are queryable in the client-side log stream, not just embedded in
    the message string. `main()` also raises the
    `fastmcp.server.context.to_client` logger to `DEBUG` so these are visible
    in the server's own stderr output locally/in CI, not only to a listening
    client — a stdlib `logging.Logger.setLevel` call, so it never touches
    stdout and can't violate the stdio JSON-RPC-only constraint.
- **Deliberately not adopted** (each has a real conflict or no motivating
  case here, not just unused-by-omission):
  - *Session state* (`ctx.get_state`/`set_state`) — would conflict with the
    shared, cross-session `TTLCache` (see Caching & politeness above), which
    exists specifically to reduce load on Apple's endpoints *across* all
    callers. Per-session isolation would defeat that.
  - *Resources* (`ctx.list_resources`/`read_resource`) — no `@mcp.resource`
    exists; nothing to enumerate.
  - *Prompt access* (`ctx.list_prompts`/`get_prompt`) — only one prompt
    exists and no tool needs to look it up programmatically.
  - *LLM sampling* (`ctx.sample`) — would reintroduce the pre-baked
    "analysis" `compare_app_store_apps` explicitly avoids (see Tool surface
    above: "Analysis is the LLM's job").
  - *Elicitation* (`ctx.elicit`) — every tool takes explicit typed params;
    disambiguation (e.g. app name → ID) is left to the calling agent via
    `search_app_store`, not an interactive mid-call prompt.
  - *Session visibility / change notifications* — the tool/prompt set is
    fixed at startup with no auth/tiering concept and nothing toggled at
    runtime.
  - *`ctx.transport`* — `main()` hard-codes `stdio`; there's no shipped code
    path where it would ever read anything else. Revisit only if a tested
    HTTP self-host mode ships.

### FastMCP `Middleware` usage (reviewed 2026-07-10 against FastMCP's Middleware docs)
- **Adopted**: three middleware, registered in `create_server()` in the order
  the docs recommend (error handling first so it wraps everything on the way
  in; timing/logging last so they observe the actual post-processed
  outcome):
  - `UnexpectedErrorLoggingMiddleware` (custom, `server.py`) — logs any
    `on_call_tool` exception that is *not* an `AppStoreMCPError` (i.e. a real
    bug — e.g. `apple/normalize.py` breaking on an Apple format change) with
    a traceback, then re-raises it unchanged; the weekly live-CI suite (see
    Testing below) is otherwise the only thing that would ever notice this
    class of failure. Deliberately *not* FastMCP's built-in
    `ErrorHandlingMiddleware`: its `transform_errors` (default `True`) wraps
    every non-`McpError` exception's message as generic `"Internal error:
    ..."`, and `ToolError` (the base of `AppStoreMCPError`) is *not* an
    `McpError` subclass — verified directly against the installed
    `fastmcp` package — so it would rewrite `AppNotFoundError`/
    `RateLimitedError`/etc.'s carefully-worded agent-recovery messages.
    Separately, its logging isn't gated by exception type, so it would flag
    every expected not-found/rate-limited call as `ERROR`-level noise
    (see Failure semantics above — those are the "tool error" tier working
    as designed, not bugs).
  - `DetailedTimingMiddleware` — per-tool `"Tool 'X' completed/failed in
    Yms"` log lines. Not the plain `TimingMiddleware`: it only logs the
    generic `"tools/call"` method name, not which of the 6 tools ran.
  - `StructuredLoggingMiddleware(include_payload_length=True,
    methods=["tools/call"])` — JSON `request_start`/`request_success`/
    `request_error` lines scoped to tool calls. `include_payloads` stays at
    its `False` default so arguments aren't duplicated into the log.
  - All three log via the stdlib `logging` module, nested under FastMCP's
    `"fastmcp"` logger namespace (`get_logger`) — never `sys.stdout` — the
    same constraint `main()` already documents for `ctx.warning` visibility.
  - The two duplicate `ctx.warning(..., extra={...})` call sites above were
    also consolidated into one `_log_fallback_failure` helper; behavior is
    unchanged, just no longer copy-pasted.
- **Deliberately not adopted** (each conflicts with or duplicates an
  existing, deliberate design decision — not just unused-by-omission):
  - *`ResponseCachingMiddleware`* — would sit *above* the tool as a second,
    argument-keyed cache layer alongside the existing `TTLCache`
    (`cache.py`), which is keyed at the HTTP-client layer by
    `(endpoint, id/query, country)` specifically so e.g. `get_app_store_app`
    and `compare_app_store_apps` can share one cache entry. A middleware
    cache hit would also report `meta.fresh=True` from inside the tool
    regardless of the middleware's own cache state — silently undermining
    the one field whose whole purpose is telling the agent when data might
    be stale.
  - *`RateLimitingMiddleware` / `SlidingWindowRateLimitingMiddleware`* —
    throttle the wrong direction: they protect the server from the client,
    but stdio means server and client share one trust boundary (see
    "No proactive throttle" under Caching & politeness above). The real
    constraint is server-to-Apple, already handled by honestly surfacing
    Apple's own 403/429 as `RateLimitedError`.
  - *`RetryMiddleware`* — its default `retry_exceptions` never fire
    (`fetch.py` already converts raw `httpx`/connection errors into
    `UpstreamError`/`RateLimitedError` before middleware would ever see
    them), and retrying a `RateLimitedError` specifically would be exactly
    the proactive throttle-fighting this project avoids.
  - *`PingMiddleware`* — keeps stateful HTTP connections alive; no effect on
    stdio. Same rationale as not touching `ctx.transport` above.
  - *`ResponseLimitingMiddleware`* — on overflow it discards all non-text
    content and keeps only truncated text, which for
    `get_app_store_screenshots` would silently drop every `Image` block —
    the tool's entire declared purpose. That tool already bounds size via
    `limit` (≤8) and per-image `return_exceptions=True` handling instead.
  - *Session state, server composition/mounting, `on_initialize`, list-
    filtering/component-metadata hooks* — no session-scoped data (would
    conflict with the cross-session cache, as above), no mounted servers,
    no auth/tiering concept; same reasoning as the adjacent Context-usage
    entries above.

### FastMCP `Lifespan`/`Depends()` usage (reviewed 2026-07-10 against FastMCP's
Dependency Injection and Lifespans docs)
- **Adopted**: `create_server()` uses `@lifespan` (`fastmcp.server.lifespan`)
  solely to close the shared `httpx.AsyncClient` on server shutdown, and only
  when `create_server()` built that client itself (`http=None`, the
  production/`main()` path) — never when a caller injects their own client
  (`create_server(http=...)`, what every test does to install a
  `MockTransport`), since that client is the caller's resource to manage.
  Without this, the process-lifetime client was never `.aclose()`'d.
- **Deliberately not adopted**: `Depends()` for the shared `httpx.AsyncClient`,
  `TTLCache`, and the four `apple/` client wrappers built once in
  `create_server()` and closed over by every tool. `Depends()` dependencies
  are cached per-*request*, not per-server-lifetime — wrapping any of these in
  `Depends()` would either reconstruct them on every call (breaking connection
  pooling and the point of the shared `TTLCache`, see Caching & politeness
  above) or just return the same already-built singleton, which is exactly
  what the existing closures already do with zero indirection. `Depends()`
  is the right tool for genuinely per-request or cheaply-recomputed values;
  none of this server's shared infrastructure fits that shape.

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
