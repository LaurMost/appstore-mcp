"""Direct-call unit tests for the tools/ orchestration bodies.

These call the `appstore_mcp.tools.*` functions directly (no fastmcp Client),
building the real apple/ clients over an httpx.MockTransport and passing small
fakes for the runtime protocols (Warner/ProgressReporter/Sampler). They cover
the orchestration friction the architecture review targeted - pagination,
fallbacks, retries, partial failures - without a full MCP round-trip.

The `__init__.py` doubles as a package marker (so the duplicate-basename test
modules here, in tests/apple/, and at the tests/ root don't collide under
pytest's prepend import mode) and a home for the shared fakes/helpers below.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from appstore_mcp.apple.charts import ChartsClient
from appstore_mcp.apple.itunes import ITunesClient
from appstore_mcp.apple.page import AppPageClient
from appstore_mcp.apple.reviews import ReviewsClient
from appstore_mcp.cache import TTLCache


@dataclass
class FakeWarner:
    """Records the (message, extra) tuples a tool's Warner callback fires."""

    calls: list[tuple[str, Mapping[str, Any] | None]] = field(default_factory=list)

    async def __call__(
        self, message: str, *, extra: Mapping[str, Any] | None = None
    ) -> None:
        self.calls.append((message, extra))


@dataclass
class FakeProgressReporter:
    """Records the (fraction, message) tuples a tool reports, in order."""

    calls: list[tuple[float, str | None]] = field(default_factory=list)

    async def report(self, fraction: float, message: str | None = None) -> None:
        self.calls.append((fraction, message))


@dataclass
class FakeSampleResult:
    text: str | None


@dataclass
class FakeSampler:
    """Replays canned text responses and records each call's args.

    `responses` is consumed one per call; construct with the exact number of
    replies the code under test should need (e.g. two for the digest
    retry-once path: an invalid one then a valid one).
    """

    responses: list[str]
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(
        self,
        messages: str | Sequence[Any],
        *,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> FakeSampleResult:
        self.calls.append(
            {
                "messages": messages,
                "system_prompt": system_prompt,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        return FakeSampleResult(text=self.responses.pop(0))


def mock_http(handler: Any) -> httpx.AsyncClient:
    """An AsyncClient whose requests are served by `handler` (a MockTransport
    request handler), matching how the existing Client-based tests inject
    fixtures."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@dataclass
class Clients:
    http: httpx.AsyncClient
    itunes: ITunesClient
    app_page: AppPageClient
    charts: ChartsClient
    reviews: ReviewsClient


def build_clients(handler: Any) -> Clients:
    """Wire the four apple/ clients over one mock-transport http client,
    sharing a single cache the way create_server() does."""
    http = mock_http(handler)
    cache: TTLCache[Any] = TTLCache()
    return Clients(
        http=http,
        itunes=ITunesClient(http, cache),
        app_page=AppPageClient(http, cache),
        charts=ChartsClient(http, cache),
        reviews=ReviewsClient(http, cache),
    )
