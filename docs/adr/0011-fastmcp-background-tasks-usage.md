# FastMCP Background Tasks: optional mode on slow tools, in-memory Docket backend only

Reviewed 2026-07-10 against FastMCP's Background Tasks docs (SEP-1686). The
tools whose work is sequential/blocking rather than a single quick request —
`get_app_store_reviews` and `digest_app_store_reviews` (both chain up to
`MAX_FEED_PAGES` sequential feed requests; the digest tool additionally
makes an `ctx.sample()` LLM call under a 120s timeout, with a possible
one-shot retry) and `get_app_store_screenshots` (concurrent image
downloads, still multi-second wall time) — set
`task=TaskConfig(mode="optional", poll_interval=timedelta(seconds=5))`. The
remaining tools stay at the `task=False` default: each does at most one or
two concurrent requests, so a 25s synchronous timeout is already a
reasonable bound. `fastmcp[tasks]` is a **hard** core dependency, not an
optional extra like `anthropic`/`openai`.

## Considered options

- **Server-wide `tasks=True` default** on the `FastMCP(...)` constructor —
  rejected: would force every fast/single-request tool to explicitly opt
  out with `task=False` for no benefit, since they were never
  long-running candidates in the first place. Per-tool opt-in is more
  precise.
- **`fastmcp[tasks]` as an optional extra** (like the LLM-provider
  extras) — rejected: `TaskConfig.validate_function` calls
  `require_docket()` at tool-*registration* time whenever
  `mode != "forbidden"`, regardless of whether any client ever requests a
  background task. Leaving `docket` opt-in would crash server startup for
  every default `uvx appstore-mcp` install the moment any tool sets
  `task=`.
- **Redis-backed Docket** (`FASTMCP_DOCKET_URL=redis://...`) — rejected:
  the default `memory://` backend is zero-config and exactly fits this
  project's per-user, single-process `uvx` distribution (ADR-0003);
  there's no shared queue across processes to persist, matching
  ADR-0007's "no disk, no Redis" caching rationale.
- **Horizontal worker scaling** (`fastmcp tasks worker server.py`) —
  rejected: that CLI adds consumers to a queue shared *across processes*,
  which only exists with the Redis backend; every `uvx appstore-mcp`
  invocation is already its own isolated process, so there is no second
  process that could ever attach to the same queue.
- **Driving progress through the `Progress` dependency alone (dropping
  `ctx.report_progress`)** — rejected: verified directly against the
  installed `fastmcp` package that outside a Docket worker, `Progress`
  falls back to `InMemoryProgress`, which never calls
  `send_progress_notification` — switching away from `ctx.report_progress`
  would silently break progress for every client that doesn't request a
  task. Both channels are now driven off the same tick (see ADR-0008).
- **`CurrentDocket()`/`CurrentWorker()`** — rejected: no tool needs to
  schedule follow-up background work or read worker metadata after a task
  completes.

## Consequences

`mode="optional"` is additive: a client that doesn't request a task keeps
the exact synchronous behavior it always had; a task-aware client gets a
task ID back immediately and can poll/await it instead of holding a
blocking connection open. Any new tool whose work is a sequential chain or
otherwise multi-second should default to the same `_BACKGROUND_TASK` config
rather than `task=False`; any future move to a Redis-backed or
horizontally-scaled Docket needs a real multi-process distribution story
first, which doesn't exist today (ADR-0003).
