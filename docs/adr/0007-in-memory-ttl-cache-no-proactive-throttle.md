# In-memory TTL cache, no disk/Redis, no proactive throttling

All four data sources share one in-memory, async-safe TTL cache (~15
minutes), keyed by `(endpoint, id/query, country)`. This exists to kill the
redundant refetching an agent loop naturally produces (repeated lookups of
the same app/query within one session), not to survive process restarts.
No proactive throttle is applied on the way out to Apple either: instead of
guessing at a safe request rate, the server sends an honest `User-Agent`
and lets Apple's own 403/429 respond with a rate-limit signal, which is
surfaced as a structured `RateLimitedError` telling the agent to wait and
retry.

## Considered options

- **Disk-backed cache** — rejected: would only help across full
  client-process restarts within roughly one TTL window, and that narrow
  benefit doesn't justify filesystem/locking complexity or the risk of a
  stale on-disk entry reporting `meta.fresh=False` for what is, from the
  fetching side, a brand-new process.
- **Redis or another external cache backend** — rejected for the same
  reason plus a distribution-shape mismatch: each `uvx appstore-mcp`
  invocation is already its own isolated single-process run (see
  ADR-0003); there's no second process to share a cache with.
- **Proactive client-side rate limiting** (e.g. a token bucket capping
  outbound requests before Apple ever sees them) — rejected: real-world
  headroom is far above Apple's documented floor (see ADR-0005) and unknown
  in advance, so a proactive cap would either be too conservative
  (throttling requests Apple would have accepted) or too permissive to
  matter; Apple's own 403/429 is the only ground truth.

## Consequences

`cache.py`'s `TTLCache` sweeps expired entries (throttled to once per TTL
window) so a long-lived stdio session touching many distinct keys doesn't
grow unbounded, and single-flights concurrent `get_or_fetch` calls for a
not-yet-cached key so near-simultaneous calls for the same app/country
share one upstream fetch instead of double-hitting Apple. `meta.retrieved_at`
always reflects the actual fetch time and `meta.fresh=False` marks cache
hits, so an agent can tell when data might be stale without the server ever
needing to model Apple's true rate limit.
