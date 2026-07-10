# Domain Glossary

Canonical terms for appstore-mcp. Use these exact words in tool names, models,
issues, and tests; don't drift to synonyms.

- **Storefront** — one country's App Store catalog (ISO 3166-1 alpha-2 code).
  Apps, ratings, reviews, and charts are all per-storefront. Not "market",
  "region", or "locale".

- **Profile** — the normalized public record of one app (`AppProfile`): the
  compact default representation, as opposed to Apple's *raw* payload.

- **Chart** — a ranked list of apps for a storefront (optionally scoped to
  one category): top-free, top-paid, or top-grossing. Each entry is a rank
  plus a slim app identity, not a full Profile. A Chart is best-effort, like
  Enrichment.

- **Enrichment** — best-effort fields merged into a Profile from the public
  App Store web page (subtitle, has_iap, privacy). Always fail-soft: absence
  degrades to nulls plus a warning, never an error.

- **Best-effort (source)** — a data source whose failure never fails the
  tool call: absence degrades to null fields (or an empty list) plus a
  warning, never an exception. Enrichment, Chart, and Review data are all
  best-effort, sourced from the public web page or undocumented feeds —
  unlike the iTunes Search/Lookup API, the one reliable source, whose
  failure is a tool error, not a warning.

- **Digest** — an LLM-compressed representation of a review set (themes,
  complaints, praise, sentiment), produced via MCP sampling. A digest is
  *data reduction*, not ground truth: quotes may be translated or
  paraphrased, and every digest carries a warning saying so. Not "summary"
  (too generic) or "analysis" (analysis is the caller's job).

- **Data reduction** — compressing a payload too large for the caller's
  context into a compact representation. The only sanctioned use of sampling
  in this server (see ADR-0001). Contrast with **analysis**: interpreting
  data the caller already holds, which belongs to the calling LLM, never to
  tool code or nested sampling.
