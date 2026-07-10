# Add an API-key-gated hosted HTTP mode, additive to stdio

A hosted streamable-HTTP deployment is added via FastMCP Cloud, using this
repo's existing `http.fastmcp.json` target. This is a second, additive
deployment path, not a replacement: `uvx appstore-mcp` over stdio remains the
primary, recommended, default, unauthenticated way to run this server, and is
what normal/high-volume use should keep using. The hosted endpoint exists for
MCP clients that specifically need a network-accessible URL instead of a
local stdio process.

ADR-0003 rejected a hosted mode outright because it would concentrate every
user's Apple requests onto one IP, defeating the per-`uvx`-invocation
headroom that decision relied on. The hosted deployment here mitigates that
by requiring a static API key (bearer token, `APPSTORE_MCP_API_KEY`) to reach
it at all — traffic through the shared IP is gated to whoever holds the key,
rather than open to anyone who finds the URL. ADR-0007's existing shared
in-memory TTL cache also incidentally helps: concurrent hosted users share
one warm cache instead of each `uvx` invocation cold-starting its own, which
reduces redundant upstream Apple requests for popular apps/queries.

## Considered options

- **Replace stdio with a hosted default** — rejected: throws away the
  per-user-IP scaling property ADR-0003 was built around, and forces every
  user through one shared, key-gated endpoint even for the common case where
  a local `uvx` process works fine and needs no auth at all.
- **Hosted HTTP, no auth** — rejected: this is exactly the shape ADR-0003
  rejected — an open hosted endpoint invites unbounded traffic concentration
  onto one IP with no way to attribute or limit it.
- **Hosted HTTP, API-key-gated, additive to stdio** — adopted: stdio/`uvx`
  stays the primary, default, unauthenticated path; the hosted endpoint is an
  opt-in exception for clients that need HTTP, and the API key bounds who can
  drive traffic through the shared IP.

## Consequences

Two supported deployment paths now exist: `uvx appstore-mcp` (stdio, no
auth, one process per user, primary/default) and a hosted HTTPS endpoint at
`https://appstore-mcp.fastmcp.app/mcp` (requires `Authorization: Bearer
<key>`, shared process, secondary/opt-in). `server.py` gains an
`_auth_provider()` that's `None` unless `APPSTORE_MCP_API_KEY` is set, so
stdio behavior is unchanged and the auth gate only ever activates in the
hosted deployment's own environment. This does not revisit or weaken
ADR-0003's underlying reasoning — it still explains why hosted mode isn't the
primary/default choice — it only adds a narrower, gated exception on top of
it.
