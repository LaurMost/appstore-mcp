# Releasing

## Publish a release to PyPI

One-time setup: on [pypi.org](https://pypi.org) → Account → Publishing → add a
**pending publisher**: project `appstore-mcp`, owner `LaurMost`, repo
`appstore-mcp`, workflow `release.yml`, environment `pypi`. On GitHub, create
the `pypi` environment (Settings → Environments).

Per release:

1. Bump `version` in `pyproject.toml` **and** `server.json` **and**
   `.claude-plugin/plugin.json` (keep the three in sync).
2. Commit, push, wait for CI.
3. Create a GitHub release tagged `vX.Y.Z`. The `release.yml` workflow builds,
   re-tests, and publishes to PyPI via trusted publishing.
4. Smoke-test the published artifact: `uvx appstore-mcp@latest` in a client,
   or `npx @modelcontextprotocol/inspector uvx appstore-mcp` for a manual
   pass over how the tools present. To exercise actual tool-*calling* (not just
   listing/schemas) through a real MCP client, run
   `uv run python scripts/claude_code_integration_test.py` — it drives the
   Claude Code CLI against the local server over stdio and asserts an
   `mcp__appstore__*` tool was really invoked end-to-end (needs a logged-in
   `claude` CLI; costs API tokens).

## After first publish: discoverability (optional)

### MCP Registry (registry.modelcontextprotocol.io)

`server.json` at the repo root is the registry manifest; the README carries
the required `mcp-name: io.github.laurmost/appstore-mcp` ownership marker
(the registry checks the PyPI package description for it, so this only
validates against a version published *after* the marker was added).

```sh
brew install mcp-publisher
mcp-publisher login github   # must be the LaurMost account
mcp-publisher publish        # reads ./server.json
```

Re-run `mcp-publisher publish` after each release (with the bumped version).

### Anthropic directory (Claude Code plugin route)

The repo doubles as a plugin marketplace (`.claude-plugin/`): users can
install with `/plugin marketplace add LaurMost/appstore-mcp`. To be listed in
the directory, validate first (`claude plugin validate .`), then submit per
https://claude.com/docs/connectors/building/submission (requires this public
repo and public docs - the README qualifies).
