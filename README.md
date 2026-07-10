# appstore-mcp

<!-- mcp-name: io.github.laurmost/appstore-mcp -->

An open-source MCP server for live Apple App Store competitor research.

It lets AI agents search apps, fetch public App Store metadata, compare
competitors side by side, and retrieve reviews and top charts as structured
JSON — for market research, ASO research, and product analysis.

It works with public competitor data only. No Apple developer account, no API
keys, no database.

> Compare Duolingo, Babbel, and Busuu on the US Apple App Store.

## Setup

Requires [uv](https://docs.astral.sh/uv/). Add to your MCP client config:

```json
{
  "mcpServers": {
    "appstore": {
      "command": "uvx",
      "args": ["appstore-mcp"]
    }
  }
}
```

- **Claude Code**: `claude mcp add appstore -- uvx appstore-mcp`, or install as a
  plugin: `/plugin marketplace add LaurMost/appstore-mcp` then `/plugin install appstore-mcp`
- **Claude Desktop**: add the snippet above to `claude_desktop_config.json`
- **Cursor**: add the snippet above to `~/.cursor/mcp.json`

If your MCP client can't find `uvx`, use the absolute path from `which uvx` as
the `command`.

## Tools

| Tool | What it does |
| --- | --- |
| `search_app_store` | Search apps by keyword (slim results: id, name, developer, rating, price) |
| `get_app_store_app` | Full public profile for one app: description, release notes, ratings, versions, screenshots, subtitle, in-app-purchase flag, privacy labels |
| `compare_app_store_apps` | Batch-fetch several apps (IDs or URLs) side by side in one call |
| `get_app_store_reviews` | Recent public customer reviews (up to ~500 per storefront) |
| `digest_app_store_reviews` | Compress up to 500 reviews into a structured digest (themes, complaints, praise, sentiment) via MCP sampling — raw reviews never enter your context, and foreign-language storefronts are digested in English |
| `get_app_store_screenshots` | An app's screenshots as actual images, so a multimodal model can analyze visual positioning, onboarding, and paywall design |
| `get_app_store_charts` | Top-free / top-paid / top-grossing charts, overall or per category, per country |

All tools are read-only and take a `country` storefront parameter (ISO
3166-1 alpha-2, default `us`). One MCP prompt, `compare_competitors`, packages
the headline comparison workflow.

Responses are compact and normalized by default; `get_app_store_app` accepts
`include_raw=true` when you want Apple's unmodified lookup payload.

### Review digestion and MCP sampling

`digest_app_store_reviews` uses [MCP sampling](https://modelcontextprotocol.io/docs/learn/client-concepts#sampling):
the tool asks *your client's* LLM to compress the reviews, so no API key is
needed. Not all MCP clients support sampling. If yours doesn't, either use
`get_app_store_reviews` for raw reviews, or enable the server-side fallback:

```sh
uvx "appstore-mcp[anthropic]"   # + set ANTHROPIC_API_KEY
uvx "appstore-mcp[openai]"      # + set OPENAI_API_KEY
```

Set `APPSTORE_MCP_SAMPLING_MODEL` to override the fallback model. The digest
is LLM-generated data reduction, not ground truth — quotes may be translated
or paraphrased, and responses say so in `meta.warnings`.

## Data sources and honest limitations

- Primary source: Apple's public [iTunes Search/Lookup API](https://performance-partners.apple.com/search-api).
- `subtitle`, `has_iap`, and `privacy` come from the public App Store web
  page; reviews and charts come from undocumented Apple feeds. These are
  **best-effort**: when they break or return nothing, tools degrade gracefully
  and say so in `meta.warnings` rather than failing or faking data.
- **Not available from public Apple data, so not provided**: download counts,
  revenue estimates, keyword rankings, full review history, historical charts.
- Apps are listed per-storefront: results, ratings, and reviews differ by
  `country`, and an app can exist in one storefront but not another.
- Results are cached in-memory for ~15 minutes; `meta.fresh` tells you whether
  a response came from cache.

## Development

```sh
uv sync --dev
uv run pytest          # offline fixture tests
uv run pytest -m live  # live smoke tests against real Apple endpoints
uv run mypy
uv run ruff check .    # lint
uv run ruff format .   # format (Black-compatible style)
```

Some tests assert full response shapes via [inline-snapshot](https://github.com/15r10nk/inline-snapshot).
After an intentional change to a tool's output shape, regenerate them:

```sh
uv run pytest --inline-snapshot=fix,create   # then review the diff
```

Optionally, verify the whole stack through a real MCP client — Claude Code
driving the local server over stdio against live Apple:

```sh
uv run python scripts/claude_code_integration_test.py
```

This is a manual, on-demand check, not part of the default dev loop: it
requires a logged-in [`claude` CLI](https://code.claude.com) and spends real
Anthropic API tokens per run.

This project is not affiliated with or endorsed by Apple. "App Store" is a
trademark of Apple Inc.

## License

MIT
