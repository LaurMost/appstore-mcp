import json
from pathlib import Path

import httpx
import pytest

from appstore_mcp.errors import AppStoreMCPError
from appstore_mcp.tools.reviews import (
    digest_app_store_reviews,
    get_app_store_reviews,
    harvest_reviews,
)
from tools import FakeProgressReporter, FakeSampler, build_clients

FIXTURES = Path(__file__).parent.parent / "fixtures"

VALID_DIGEST = json.dumps(
    {
        "overall_sentiment": "positive",
        "summary": "Users love the gamified lessons but complain about ads.",
        "themes": [
            {
                "theme": "gamification",
                "sentiment": "positive",
                "approximate_share": "about half",
                "example_quote": "the streaks keep me coming back",
            }
        ],
        "top_complaints": ["too many ads"],
        "top_praise": ["fun lessons"],
        "source_language_note": None,
    }
)


def reviews_handler(empty_feed: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        if "/rss/customerreviews/" in request.url.path:
            if empty_feed:
                return httpx.Response(200, content=json.dumps({"feed": {}}).encode())
            return httpx.Response(
                200, content=(FIXTURES / "reviews_duolingo_us_p1.json").read_bytes()
            )
        if request.url.host == "apps.apple.com":
            return httpx.Response(
                200, content=(FIXTURES / "page_duolingo_us.html").read_bytes()
            )
        return httpx.Response(404)

    return handler


async def test_harvest_paginates_beyond_one_feed_page() -> None:
    paths: list[str] = []
    base = reviews_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return base(request)

    clients = build_clients(handler)
    progress = FakeProgressReporter()
    async with clients.http:
        collection = await harvest_reviews(
            clients.reviews,
            clients.app_page,
            "570060128",
            country="us",
            sort="most_recent",
            limit=60,
            progress=progress,
        )
    assert len(collection.reviews) == 60
    feed_pages = [p for p in paths if "/rss/customerreviews/" in p]
    assert len(feed_pages) == 2
    # Two page ticks (0.1, 0.2 of the 0-100 range) then a terminal 1.0.
    fractions = [round(f, 3) for f, _ in progress.calls]
    assert fractions == [0.1, 0.2, 1.0]


async def test_harvest_falls_back_to_page_when_feed_empty() -> None:
    paths: list[str] = []
    base = reviews_handler(empty_feed=True)

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(str(request.url))
        return base(request)

    clients = build_clients(handler)
    progress = FakeProgressReporter()
    async with clients.http:
        collection = await harvest_reviews(
            clients.reviews,
            clients.app_page,
            "570060128",
            country="us",
            sort="most_recent",
            limit=50,
            progress=progress,
        )
    assert len(collection.reviews) >= 5
    assert any("fallback" in w or "feed" in w for w in collection.warnings)
    assert collection.sources[-1].name == "apple_app_store_page"
    # One empty feed page (0.1), then terminal 1.0 once the fallback completes.
    fractions = [round(f, 3) for f, _ in progress.calls]
    assert fractions == [0.1, 1.0]


async def test_harvest_scales_progress_into_subrange() -> None:
    clients = build_clients(reviews_handler())
    progress = FakeProgressReporter()
    async with clients.http:
        await harvest_reviews(
            clients.reviews,
            clients.app_page,
            "570060128",
            country="us",
            sort="most_recent",
            limit=20,
            progress=progress,
            progress_end=50,
        )
    # A single page fills limit=20; the fraction is scaled into [0, 50] then
    # expressed as a bare 0-1 fraction of the caller's own 0-100 reporter.
    fractions = [round(f, 3) for f, _ in progress.calls]
    assert fractions == [0.05, 0.5]


async def test_get_reviews_respects_limit() -> None:
    clients = build_clients(reviews_handler())
    async with clients.http:
        result = await get_app_store_reviews(
            clients.reviews, clients.app_page, "570060128", limit=10
        )
    assert result.app_id == "570060128"
    assert len(result.reviews) == 10


async def test_digest_retries_once_on_invalid_json() -> None:
    clients = build_clients(reviews_handler())
    sampler = FakeSampler(["not valid json at all", VALID_DIGEST])
    progress = FakeProgressReporter()
    async with clients.http:
        result = await digest_app_store_reviews(
            clients.reviews,
            clients.app_page,
            "570060128",
            limit=20,
            sampler=sampler,
            progress=progress,
        )
    assert result.digest.overall_sentiment == "positive"
    assert result.reviews_considered == 20
    assert any("retry" in w for w in result.meta.warnings)
    assert len(sampler.calls) == 2
    # Harvest (0.05, 0.5), a message-only re-report at 0.5, retry 0.75, done 1.0.
    fractions = [round(f, 3) for f, _ in progress.calls]
    assert fractions == [0.05, 0.5, 0.5, 0.75, 1.0]


async def test_digest_first_valid_response_skips_retry() -> None:
    clients = build_clients(reviews_handler())
    sampler = FakeSampler([VALID_DIGEST])
    async with clients.http:
        result = await digest_app_store_reviews(
            clients.reviews,
            clients.app_page,
            "570060128",
            limit=20,
            sampler=sampler,
        )
    assert result.digest.overall_sentiment == "positive"
    assert not any("retry" in w for w in result.meta.warnings)
    assert len(sampler.calls) == 1


async def test_digest_without_sampler_gives_guidance() -> None:
    clients = build_clients(reviews_handler())
    async with clients.http:
        with pytest.raises(AppStoreMCPError, match="get_app_store_reviews"):
            await digest_app_store_reviews(
                clients.reviews,
                clients.app_page,
                "570060128",
                limit=20,
                sampler=None,
            )
