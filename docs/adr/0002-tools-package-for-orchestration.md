# Introduce a `tools/` package for orchestration logic

`server.py`'s `create_server()` had grown to 1085 lines: all 7 tool bodies
lived as closures nested inside it, sharing its scope for the `apple/`
clients, cache, and config it builds. This reverses the original v1 design's
"Project structure" decision ("No `tools/` package — five thin tools live in
`server.py`; logic lives in `apple/`"), made when the server had only 5
tools. Every test for review-harvesting, digest-retry, and
screenshot-fetching had to go through a full in-memory FastMCP `Client` +
`MockTransport` + JSON-RPC round trip just to reach what is actually plain
orchestration logic, because that logic could only be reached as closures
inside `create_server()`. We now extract all 7 tool bodies into a new
`tools/` package (one module per tool, or closely-related tool pair),
alongside a new `runtime.py` module holding `Warner`/`ProgressReporter`/
`Sampler` protocols. Simply extracting functions that still take
`ctx: Context` directly would only have fully solved testability for the 3
tools that never touch `ctx`/`Progress` (`search_app_store`,
`compare_app_store_apps`, `get_app_store_charts`): FastMCP's own docs
confirm there is no way to construct a bare `Context` outside a live
request — `CurrentContext()`/`get_context()` both pull the *current*
request's context, not a fresh one — so the other 4 tools needed narrow
protocols instead, letting tests pass fakes for warning/progress/sampling
without a live request.

## Considered options

- **Single `orchestration.py` module with one function per tool** —
  rejected: recreates a smaller monolith (~500-600 combined lines) rather
  than fixing the shallow-module problem.
- **Bundled `dependencies`/`deps` object passed to every orchestration
  function** — rejected: reintroduces a grab-bag interface; explicit
  per-function params keep each interface minimal to what it actually uses.
- **One combined `ToolRuntime` protocol bundling warning+progress+sampling**
  — rejected: forces every function to take all three capabilities even
  when it only needs one.
- **Passing `ctx: Context` straight through without new protocols** —
  rejected: FastMCP's `Context` can't be constructed standalone for tests
  (see finding above), so this would leave 4 of 7 tools only partially
  deepened.

## Consequences

`server.py` shrinks to bootstrap (icon/sampling-handler/middleware/
lifespan, unchanged) + 3 direct tool registrations + 4 thin ctx-unpacking
wrappers + the one prompt. New tests can call `tools/*` functions directly
with `Warner`/`ProgressReporter`/`Sampler` fakes instead of a full MCP
`Client`+`MockTransport` round trip. Existing `Client`-based tests are kept
as-is for MCP-level (annotations/schema/wire-progress) coverage. Any future
tool addition should default to living in `tools/` as a plain
dependency-accepting function, with `server.py` staying a thin registration
layer.
