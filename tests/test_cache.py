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
