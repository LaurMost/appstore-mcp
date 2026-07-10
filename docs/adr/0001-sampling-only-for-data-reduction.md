# Sampling is used only for data reduction, never for analysis

MCP sampling (`ctx.sample`) lets a tool invoke the client's LLM mid-execution.
We restrict it to one job: compressing payloads the caller could not
affordably hold in context (today: `digest_app_store_reviews`, turning up to
500 reviews / ~25k tokens into a structured digest, normalizing any storefront
language to English). We deliberately do NOT sample for analysis of data the
caller already receives — comparison verdicts, positioning insights, ASO
advice — because the calling LLM sees those tool results anyway and a nested
LLM call only adds latency, client cost, and nondeterminism. This extends the
v1 decision that stripped the `comparison` analysis object from
`compare_app_store_apps` ("analysis is the LLM's job").

## Considered options

- **Sampling for comparison/positioning analysis** — rejected: duplicates the
  outer LLM's native capability against the same data.
- **Sampling with images for screenshot analysis** — rejected in favor of
  `get_app_store_screenshots` returning `ImageContent` directly, letting the
  outer multimodal model see actual pixels; a text description from an inner
  model would be lossier and depends on the least-supported client
  capability.
- **fastmcp `result_type` structured sampling** — rejected for the digest:
  it requires tools-in-sampling client support (SEP-1577), which almost no
  client has. We sample plain text and parse/validate JSON ourselves with one
  retry, so any basic-sampling client works.

## Consequences

Clients without sampling support get a structured error from
`digest_app_store_reviews` pointing to `get_app_store_reviews`; the server
stays keyless by default, with an opt-in API-key fallback handler
(`ANTHROPIC_API_KEY`/`OPENAI_API_KEY` + the matching optional dependency).
Any future sampling proposal must answer: "is this reducing data the caller
can't hold, or analyzing data it already has?" — only the former is in scope.
