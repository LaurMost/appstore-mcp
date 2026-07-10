# FastMCP `Context` usage: progress and warnings only

Reviewed 2026-07-10 against FastMCP's `Context` docs. Only two `Context`
capabilities are used, and only on the tools whose work justifies them
(`get_app_store_app`, `get_app_store_reviews`, `digest_app_store_reviews`):
`ctx.report_progress` inside the shared review-harvesting helper's
up-to-10-page sequential feed loop (the one real long-running, multi-step
operation in the server), and `ctx.warning` at the two deepest fallback
points (page-enrichment failure, page-fallback-also-failed), alongside —
not instead of — the existing structured `meta.warnings` string.

## Considered options

- **Session state** (`ctx.get_state`/`set_state`) — rejected: would
  conflict with the shared, cross-session `TTLCache` (ADR-0007), which
  exists specifically to reduce load *across* all callers; per-session
  isolation would defeat that.
- **Resources / prompt access** (`ctx.list_resources`, `ctx.read_resource`,
  `ctx.list_prompts`, `ctx.get_prompt`) — rejected: no `@mcp.resource`
  exists to enumerate, and the one prompt (`compare_competitors`) never
  needs to be looked up programmatically by a tool.
- **LLM sampling for anything beyond review digestion** (`ctx.sample`) —
  rejected: would reintroduce the pre-baked analysis
  `compare_app_store_apps` deliberately avoids (see ADR-0001, ADR-0004).
- **Elicitation** (`ctx.elicit`) — rejected: every tool takes explicit
  typed params; disambiguation (e.g. app name → ID) is left to the calling
  agent via `search_app_store`, not an interactive mid-call prompt.
- **Session visibility / change notifications** — rejected: the
  tool/prompt set is fixed at startup, with no auth/tiering concept and
  nothing toggled at runtime.
- **`ctx.transport`** — rejected: `main()` hard-codes stdio (ADR-0003);
  there is no shipped code path where reading the transport would matter.

## Consequences

Progress is reported as a `progress_start`/`progress_end`-scaled percentage
of `total=100` (not raw page counts), with a terminal tick at
`progress_end` guaranteed on every exit path, so a client's progress
indicator never stalls below 100% with no "done" signal. `get_app_store_reviews`
uses the full 0-100 range; `digest_app_store_reviews` reserves 0-50 for
review harvesting and 50-100 for LLM sampling (with a 75% tick before a
one-shot invalid-output retry), keeping progress moving continuously across
both stages under its 120s timeout. `ctx.warning` calls pass structured
`extra={app_id, country, error_type, error_message, ...}` fields (reviewed
against FastMCP's Client Logging docs) so they're queryable in a
client-side log stream, not just embedded in the message string; `main()`
also raises the `fastmcp.server.context.to_client` logger to `DEBUG` so
these are visible in the server's own stderr locally/in CI. Any future
`Context` use should be checked against this list first — most capabilities
were considered and rejected for a specific, still-valid reason, not just
unused by omission.
