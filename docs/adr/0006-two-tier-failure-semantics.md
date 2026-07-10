# Two-tier failure semantics: tool error vs in-band degradation

Every tool distinguishes exactly two kinds of failure. A **tool error**
(raised exception, `isError` in MCP terms) means the core promise of the
call is broken: app not found, invalid input, rate-limited/unreachable
upstream. These messages are written for agent recovery (e.g. "App X not
found in storefront 'de'. Apps are per-country — try country='us' or
search_app_store."). **In-band degradation** (success response, with
`meta.warnings` or a per-item `errors` list) covers everything the tool can
route around: page enrichment failing, one app of five failing in
`compare_app_store_apps`, an empty review feed, a category chart that
failed to parse. `compare_app_store_apps` specifically raises only when
*every* requested app fails or the input is invalid outright — a partial
batch is a successful (if degraded) result, not a tool error.

## Considered options

- **Treat any upstream fetch failure as a tool error** — rejected: would
  turn `compare_app_store_apps`'s entire batch into a hard failure whenever
  a single app in the batch is unavailable, defeating the point of a
  batch-comparison tool.
- **Treat all degradation as silent (null fields, no warnings)** —
  rejected: an agent acting on a profile with a silently-null `subtitle` or
  an empty chart has no signal that the data is incomplete versus
  genuinely absent.
- **A single generic "partial success" error type** — rejected in favor of
  structured `meta.warnings`/`errors`: callers need to know *what* degraded
  and *why*, not just that something did.

## Consequences

Every new best-effort field or source (see ADR-0005) must degrade via
`meta.warnings` (or a per-item error, for batch tools) rather than raising,
and every genuinely-broken-promise case (not-found, invalid input,
rate-limited) must raise rather than silently returning empty/null data.
`compare_app_store_apps`'s `errors` list means a caller must check `errors`
even on a "successful" (non-raising) response to know whether every app it
asked for actually came back.
