import asyncio

import pytest

from appstore_mcp.cache import TTLCache


async def test_first_fetch_is_fresh_and_second_is_cached() -> None:
    now = [0.0]
    cache: TTLCache[str] = TTLCache(ttl_seconds=900, clock=lambda: now[0])
    calls = []

    async def fetch() -> str:
        calls.append(1)
        return "payload"

    first = await cache.get_or_fetch("key", fetch)
    assert first.value == "payload"
    assert first.fresh is True

    now[0] = 60.0
    second = await cache.get_or_fetch("key", fetch)
    assert second.value == "payload"
    assert second.fresh is False
    assert len(calls) == 1


async def test_expired_entry_is_refetched() -> None:
    now = [0.0]
    cache: TTLCache[int] = TTLCache(ttl_seconds=900, clock=lambda: now[0])
    values = iter([1, 2])

    async def fetch() -> int:
        return next(values)

    assert (await cache.get_or_fetch("k", fetch)).value == 1
    now[0] = 901.0
    refetched = await cache.get_or_fetch("k", fetch)
    assert refetched.value == 2
    assert refetched.fresh is True


async def test_expired_entries_are_swept_even_without_a_repeat_lookup() -> None:
    """A key that expires and is never looked up again shouldn't linger forever."""
    now = [0.0]
    cache: TTLCache[int] = TTLCache(ttl_seconds=900, clock=lambda: now[0])

    async def fetch_one() -> int:
        return 1

    await cache.get_or_fetch("one-off-key", fetch_one)
    assert "one-off-key" in cache._entries

    # Past the TTL, and past the sweep throttle window (>= one full TTL later).
    now[0] = 1800.0

    async def fetch_other() -> int:
        return 2

    # A lookup for an unrelated key still triggers the periodic sweep pass.
    await cache.get_or_fetch("other-key", fetch_other)
    assert "one-off-key" not in cache._entries


async def test_concurrent_fetches_for_same_key_are_single_flighted() -> None:
    """Two concurrent callers for the same uncached key share one fetch call."""
    now = [0.0]
    cache: TTLCache[int] = TTLCache(ttl_seconds=900, clock=lambda: now[0])
    calls = []
    started = asyncio.Event()

    async def fetch() -> int:
        calls.append(1)
        started.set()
        await asyncio.sleep(0.05)
        return 42

    first_task = asyncio.create_task(cache.get_or_fetch("shared-key", fetch))
    await started.wait()
    second_task = asyncio.create_task(cache.get_or_fetch("shared-key", fetch))

    first, second = await asyncio.gather(first_task, second_task)
    assert first.value == 42
    assert second.value == 42
    assert len(calls) == 1


async def test_single_flighted_fetch_failure_propagates_to_all_waiters() -> None:
    cache: TTLCache[int] = TTLCache(ttl_seconds=900, clock=lambda: 0.0)
    started = asyncio.Event()

    async def failing_fetch() -> int:
        started.set()
        await asyncio.sleep(0.05)
        raise ValueError("upstream boom")

    first_task = asyncio.create_task(cache.get_or_fetch("bad-key", failing_fetch))
    await started.wait()
    second_task = asyncio.create_task(cache.get_or_fetch("bad-key", failing_fetch))

    for task in (first_task, second_task):
        with pytest.raises(ValueError, match="upstream boom"):
            await task

    # No leftover in-flight state after both waiters observed the failure.
    assert cache._in_flight == {}
