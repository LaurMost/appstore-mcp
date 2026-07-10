# Ship stdio-only transport in v1, no hosted mode

> Partially superseded by
> [ADR-0012](0012-additive-hosted-http-mode.md): an additive, API-key-gated
> hosted HTTP mode was added. The rate-limit-concentration and no-auth
> reasoning below still explains why that hosted mode isn't the
> primary/default path — stdio/`uvx` remains it.

appstore-mcp runs exclusively over stdio, published to PyPI and launched via
`uvx appstore-mcp`. Apple's iTunes Search/Lookup API is undocumented but has
real-world headroom well above its documented "~20 req/min" (reports of
300-350 req/min before 429s), and residential/office IPs get throttled far
less aggressively than datacenter IPs. A hosted server would concentrate
every user's traffic onto one IP, defeating that headroom and inviting
rate-limiting that a per-user `uvx` install never hits. stdio also needs no
auth, no ops, and no infrastructure to run or pay for.

## Considered options

- **Hosted streamable-HTTP server (SaaS-style)** — rejected: concentrates
  all users' requests onto one IP against Apple's real-world rate limit, and
  requires auth, hosting, and ongoing ops for a project with no revenue
  model.
- **stdio-only, code stays transport-agnostic** — adopted:
  `mcp.run(transport="streamable-http")` remains a one-line self-host option
  for anyone who wants it, but nothing is shipped or supported.

## Consequences

Every `uvx appstore-mcp` invocation is its own isolated process hitting
Apple from the invoking user's own IP, so aggregate throughput scales with
the number of users rather than concentrating on the maintainer's
infrastructure. No hosted mode also means no auth story is needed anywhere
in this server. Revisit only if a tested, supported HTTP self-host mode is
deliberately built and documented — the transport-agnostic code already
permits it, but nothing about it (deployment, auth, rate limiting) is
designed today.
