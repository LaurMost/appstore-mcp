"""Review digestion via MCP sampling: prompt construction and response parsing.

Sampling is used here for data reduction only - compressing hundreds of
reviews the caller could not affordably hold in context. Analysis of data the
caller already has stays the caller's job (see docs/adr/0001).

We deliberately avoid fastmcp's `result_type` structured output: it requires
the client to support tools-in-sampling (SEP-1577), which almost no client
does yet. Plain-text sampling + our own JSON parsing works with any client
that supports basic sampling, and we retry once on invalid JSON.
"""

import json

from mcp.types import SamplingMessage, TextContent
from pydantic import ValidationError

from appstore_mcp.models import Review, ReviewDigest
from appstore_mcp.runtime import Sampler

DIGEST_SYSTEM_PROMPT = """\
You are an app-market analyst digesting App Store customer reviews.
Always respond in English, even when reviews are in other languages; translate
quotes to English and note the original language in source_language_note.
Respond with a single JSON object only - no markdown fences, no prose - with
exactly these fields:
  overall_sentiment: one of "very_negative", "negative", "mixed", "positive",
    "very_positive"
  summary: 2-4 sentence overview of what reviewers say
  themes: array of {theme, sentiment ("positive"|"negative"|"mixed"),
    approximate_share, example_quote}
  top_complaints: array of short strings, most common first
  top_praise: array of short strings, most common first
  source_language_note: string or null (mention if reviews were not in English)
Base everything strictly on the reviews given; do not invent data."""

MAX_PROMPT_CHARS = 60_000
MAX_REVIEW_CHARS = 500


class DigestSamplingError(Exception):
    """A sampling call inside `digest_with_retry` failed (as opposed to the
    model returning invalid JSON).

    The two failure modes would otherwise be indistinguishable at this
    function's boundary: a client that does not support sampling surfaces as a
    plain `ValueError("Client does not support sampling")`, the same type
    `parse_digest` raises on unparseable output. Wrapping sampling-channel
    failures in a distinct type lets the caller (tools/reviews.py) turn them
    into the agent-facing "use get_app_store_reviews instead" guidance while
    still letting a genuine post-retry parse failure propagate uncaught.
    """


def format_reviews(reviews: list[Review], max_chars: int = MAX_PROMPT_CHARS) -> str:
    lines: list[str] = []
    total = 0
    for review in reviews:
        body = review.body[:MAX_REVIEW_CHARS]
        rating = f"{review.rating}/5" if review.rating is not None else "?/5"
        title = f" {review.title!r}" if review.title else ""
        line = f"[{rating}]{title} {body}"
        total += len(line)
        if total > max_chars:
            break
        lines.append(line)
    return "\n".join(lines)


def build_digest_prompt(
    app_id: str, country: str, reviews: list[Review], focus: str | None
) -> str:
    focus_line = f"\nFocus the digest on: {focus}\n" if focus else ""
    return (
        f"Digest these {len(reviews)} App Store customer reviews for app "
        f"{app_id} (storefront '{country}').{focus_line}\n"
        f"{format_reviews(reviews)}"
    )


def parse_digest(text: str) -> ReviewDigest:
    """Parse the sampled response into a ReviewDigest.

    Tolerates markdown fences and surrounding prose by slicing from the first
    '{' to the last '}'. Raises ValueError/ValidationError on anything else.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("response contained no JSON object")
    return ReviewDigest.model_validate(json.loads(text[start : end + 1]))


async def digest_with_retry(
    sample: Sampler, prompt: str, *, system_prompt: str = DIGEST_SYSTEM_PROMPT
) -> tuple[ReviewDigest, list[str]]:
    """Sample a digest, retrying once on invalid JSON output.

    Mirrors the state machine that used to live inline in
    `digest_app_store_reviews`: one plain-text sample parsed as JSON; on
    `ValueError`/`ValidationError` (invalid or missing JSON), one corrective
    retry - replaying the original prompt, the bad response, and a request
    for JSON-only, at temperature=0.0 to favor compliance over creativity -
    then parsing again. A second parse failure propagates uncaught; there is
    no unbounded retry loop.

    Returns the digest plus any warnings produced along the way (currently
    just the one-line note when a retry was needed, so callers can fold it
    into their own warnings list).
    """
    try:
        result = await sample(
            prompt, system_prompt=system_prompt, temperature=0.2, max_tokens=2000
        )
    except Exception as exc:
        raise DigestSamplingError(str(exc)) from exc
    text = result.text or ""
    try:
        return parse_digest(text), []
    except (ValueError, ValidationError) as first_error:
        try:
            retry = await sample(
                [
                    SamplingMessage(
                        role="user", content=TextContent(type="text", text=prompt)
                    ),
                    SamplingMessage(
                        role="assistant", content=TextContent(type="text", text=text)
                    ),
                    SamplingMessage(
                        role="user",
                        content=TextContent(
                            type="text",
                            text=(
                                f"That response was invalid ({first_error}). "
                                f"Reply again with ONLY the corrected JSON object."
                            ),
                        ),
                    ),
                ],
                system_prompt=system_prompt,
                temperature=0.0,
                max_tokens=2000,
            )
        except Exception as exc:
            raise DigestSamplingError(str(exc)) from exc
        digest = parse_digest(retry.text or "")
        return digest, ["digest required a retry after invalid LLM output"]
