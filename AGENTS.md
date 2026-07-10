# appstore-mcp

Open-source MCP server for live Apple App Store competitor research.
Python 3.12, uv, fastmcp. Design record: `CONTEXT.md` (domain glossary) +
`docs/adr/` (decisions) — see `docs/agents/domain.md`. There is no PLAN.md;
the original v1 design doc was fully redistributed into ADRs, CONTEXT.md,
and this file once the decisions it recorded had settled.

Depend on the `fastmcp` package (jlowin's actively-developed one) — not the
frozen FastMCP bundled in the official `mcp` SDK. `mcp[cli]` is not a
dependency; don't reintroduce it.

Local dev server: `fastmcp dev` (Inspector UI, stdio) or `fastmcp run`
(standalone), driven by `fastmcp.json` — see README's Development section.

## Tool surface

7 read-only MCP tools over public Apple App Store data (`search_app_store`,
`get_app_store_app`, `compare_app_store_apps`, `get_app_store_charts`,
`get_app_store_reviews`, `digest_app_store_reviews`,
`get_app_store_screenshots`), plus one prompt, `compare_competitors`. Tool
orchestration lives in `src/appstore_mcp/tools/` (one module per tool),
`server.py` stays thin registrations, and `apple/` stays single-source API
clients per upstream domain — see
`docs/adr/0002-tools-package-for-orchestration.md` for why. This list will
drift as tools are added; `server.py`'s `@mcp.tool` decorators are the
source of truth for exact signatures, not this file.

Conventions for any new tool:
- `readOnlyHint: true`, `idempotentHint: true`, `openWorldHint: true`
  annotations; an explicit `timeout`; numeric limits as
  `Annotated[int, Field(ge=..., le=...)]`, never manual clamping; a
  Google-style `Args:` docstring section so FastMCP generates
  per-parameter schema descriptions.
- Compact, normalized response by default — raw upstream payloads are
  opt-in only (`docs/adr/0004-compact-response-shape-with-raw-opt-in.md`).
- Failures follow the two-tier split in
  `docs/adr/0006-two-tier-failure-semantics.md`: a broken promise raises;
  in-band degradation returns success plus `meta.warnings`/per-item
  `errors`.

## Out of scope

Google Play, download/revenue estimates, App Store Connect data, historical
tracking, paid data providers, browser automation, a hosted/HTTP mode, a
dashboard, a database. This project stays public-Apple-data-only and
stdio-only (`docs/adr/0003-stdio-only-transport.md`); don't pick up a
feature request from this list without first revisiting the scope decision
explicitly.

## Testing

Fixture-based unit tests (`tests/fixtures/`, real recorded Apple responses)
are the bulk of the suite — they test Apple's shapes; hand-written mocks
are for testing our own assumptions, not Apple's. A small
`@pytest.mark.live` smoke suite (excluded by default, `-m "not live"`) runs
against real Apple endpoints on a weekly CI cron, to catch upstream format
drift between releases. See README's Development section for exact
commands.

## Agent skills

### Issue tracker

Issues live in this repo's GitHub Issues via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Canonical label names used as-is (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
