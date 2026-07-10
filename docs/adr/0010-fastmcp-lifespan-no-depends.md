# FastMCP `Lifespan` for HTTP client teardown; `Depends()` rejected for shared infrastructure

Reviewed 2026-07-10 against FastMCP's Dependency Injection and Lifespans
docs. `create_server()` uses a `@lifespan` hook solely to close the shared
`httpx.AsyncClient` on server shutdown — and only when `create_server()`
built that client itself (the production/`main()` path); never when a
caller injects their own client (`create_server(http=...)`, what every test
does to install a `MockTransport`), since that client is the caller's
resource to manage. `Depends()` is not used anywhere for the shared
`httpx.AsyncClient`, `TTLCache`, or the `apple/` client wrappers built once
in `create_server()` and closed over by every tool.

## Considered options

- **`Depends()` for the shared HTTP client / cache / apple clients** —
  rejected: `Depends()` dependencies are cached per-*request*, not
  per-server-lifetime. Wrapping any of these in `Depends()` would either
  reconstruct them on every call (breaking connection pooling and the point
  of the shared `TTLCache`, ADR-0007) or just return the same already-built
  singleton — exactly what the existing closures already do, with zero
  indirection gained.
- **No lifespan hook at all (rely on process exit to clean up the
  client)** — rejected: the process-lifetime `httpx.AsyncClient` was never
  explicitly `.aclose()`'d, which is the kind of thing that shows up as a
  resource-cleanup warning under a stricter async runtime or test harness.

## Consequences

`Depends()` remains the right tool for genuinely per-request or
cheaply-recomputed values; none of this server's shared infrastructure fits
that shape, since it's all built once and reused for the server's entire
lifetime. Any new shared, expensive-to-construct resource should follow the
same closure-plus-lifespan pattern rather than reaching for `Depends()`.
