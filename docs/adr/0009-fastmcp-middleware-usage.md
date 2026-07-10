# FastMCP `Middleware`: custom error/timing/structured logging, no built-in caching/rate-limiting/retry

Reviewed 2026-07-10 against FastMCP's Middleware docs. Three middleware are
registered in `create_server()`, in the order the docs recommend (error
handling first so it wraps everything on the way in; timing/logging last so
they observe the actual post-processed outcome): a custom
`UnexpectedErrorLoggingMiddleware` that logs (with traceback) and re-raises
any `on_call_tool` exception that isn't an `AppStoreMCPError`;
`DetailedTimingMiddleware` for per-tool timing lines; and
`StructuredLoggingMiddleware` (with `include_payload_length=True`, scoped to
`tools/call`) for JSON request-lifecycle lines. All three log via stdlib
`logging` under FastMCP's `"fastmcp"` namespace, never `sys.stdout`.

## Considered options

- **FastMCP's built-in `ErrorHandlingMiddleware`** — rejected in favor of
  the custom middleware above: its `transform_errors` (default `True`)
  rewrites every non-`McpError` exception's message as a generic
  `"Internal error: ..."`, and `ToolError` (the base of `AppStoreMCPError`)
  is *not* an `McpError` subclass (verified against the installed `fastmcp`
  package) — so it would overwrite `AppNotFoundError`/`RateLimitedError`/etc.'s
  carefully-worded agent-recovery messages. Its logging is also not gated
  by exception type, so it would flag every expected not-found/rate-limited
  call (see ADR-0006) as `ERROR`-level noise instead of working-as-designed.
- **Plain `TimingMiddleware`** — rejected in favor of
  `DetailedTimingMiddleware`: it only logs the generic `"tools/call"`
  method name, not which of the server's tools actually ran.
- **`ResponseCachingMiddleware`** — rejected: would sit above the tool as a
  second, argument-keyed cache layer alongside the existing `TTLCache`
  (ADR-0007), which is deliberately keyed at the HTTP-client layer so e.g.
  `get_app_store_app` and `compare_app_store_apps` can share one cache
  entry. A middleware-level cache hit would also report `meta.fresh=True`
  from inside the tool regardless of the middleware's own cache state,
  silently undermining the one field whose whole purpose is telling the
  agent when data might be stale.
- **`RateLimitingMiddleware` / `SlidingWindowRateLimitingMiddleware`** —
  rejected: they throttle the wrong direction (protect the server from the
  client), but stdio means server and client share one trust boundary
  (ADR-0003). The real constraint is server-to-Apple, already handled by
  honestly surfacing Apple's own 403/429 as `RateLimitedError` (ADR-0007).
- **`RetryMiddleware`** — rejected: its default `retry_exceptions` never
  fire, since `fetch.py` already converts raw `httpx`/connection errors
  into `UpstreamError`/`RateLimitedError` before middleware would ever see
  them; retrying a `RateLimitedError` specifically would be exactly the
  proactive throttle-fighting ADR-0007 avoids.
- **`PingMiddleware`** — rejected: keeps stateful HTTP connections alive,
  which has no effect on stdio.
- **`ResponseLimitingMiddleware`** — rejected: on overflow it discards all
  non-text content and keeps only truncated text, which for
  `get_app_store_screenshots` would silently drop every `Image` block — the
  tool's entire declared purpose. That tool already bounds size via `limit`
  (≤8) and per-image `return_exceptions=True` handling.
- **Session state, server composition/mounting, `on_initialize`,
  list-filtering/component-metadata hooks** — rejected: no session-scoped
  data (would conflict with the cross-session cache), no mounted servers,
  no auth/tiering concept.

## Consequences

Any future middleware proposal that touches caching, rate-limiting, or
retries needs to explain why it doesn't collide with the existing
`TTLCache` (ADR-0007) or the deliberate "let Apple's own errors surface"
design (ADR-0006) before being adopted — most of FastMCP's built-in
middleware for those concerns was considered and rejected for a specific
reason above, not left out by omission.
