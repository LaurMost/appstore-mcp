import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client

from appstore_mcp.server import create_server

FIXTURES = Path(__file__).parent / "fixtures"

VALID_DIGEST = json.dumps(
    {
        "overall_sentiment": "positive",
        "summary": "Users love the gamified lessons but complain about ads.",
        "themes": [
            {
                "theme": "gamification",
                "sentiment": "positive",
                "approximate_share": "about half of reviews",
                "example_quote": "the streaks keep me coming back",
            }
        ],
        "top_complaints": ["too many ads"],
        "top_praise": ["fun lessons"],
        "source_language_note": None,
    }
)


def reviews_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/rss/customerreviews/" in request.url.path
        return httpx.Response(
            200, content=(FIXTURES / "reviews_duolingo_us_p1.json").read_bytes()
        )

    return httpx.MockTransport(handler)


def make_client(responses: list[str] | None) -> tuple[Client, list[Any]]:
    """Client whose sampling handler replays canned responses and records
    the requests it receives. responses=None means no sampling support."""
    seen: list[Any] = []
    server = create_server(http=httpx.AsyncClient(transport=reviews_transport()))
    if responses is None:
        return Client(server), seen
    queue = list(responses)

    async def sampling_handler(messages: Any, params: Any, context: Any) -> str:
        seen.append((messages, params))
        return queue.pop(0)

    return Client(server, sampling_handler=sampling_handler), seen


async def test_digest_returns_structured_digest() -> None:
    client, seen = make_client([VALID_DIGEST])
    async with client:
        result = await client.call_tool(
            "digest_app_store_reviews", {"app_id_or_url": "570060128", "limit": 50}
        )
    data = result.structured_content
    assert data["digest"]["overall_sentiment"] == "positive"
    assert data["digest"]["themes"][0]["theme"] == "gamification"
    assert data["reviews_considered"] == 50
    assert any("LLM-generated" in w for w in data["meta"]["warnings"])
    # The sampled prompt must actually carry review text.
    messages, params = seen[0]
    prompt_text = messages[0].content.text
    assert "/5]" in prompt_text and "570060128" in prompt_text
    assert "English" in (params.systemPrompt or "")


async def test_digest_retries_once_on_invalid_llm_output() -> None:
    client, seen = make_client(["I cannot produce JSON, sorry.", VALID_DIGEST])
    async with client:
        result = await client.call_tool(
            "digest_app_store_reviews", {"app_id_or_url": "570060128", "limit": 20}
        )
    data = result.structured_content
    assert data["digest"]["overall_sentiment"] == "positive"
    assert any("retry" in w for w in data["meta"]["warnings"])
    assert len(seen) == 2


async def test_digest_without_sampling_support_gives_guidance() -> None:
    client, _ = make_client(None)
    async with client:
        with pytest.raises(Exception, match="get_app_store_reviews"):
            await client.call_tool(
                "digest_app_store_reviews", {"app_id_or_url": "570060128"}
            )
