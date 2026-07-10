"""In-memory async-safe TTL cache. No disk, no Redis - agent-loop refetch guard."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Generic, TypeVar

from appstore_mcp.models import utcnow

T = TypeVar("T")

DEFAULT_TTL_SECONDS = 15 * 60


@dataclass
class CacheEntry(Generic[T]):
    value: T
    retrieved_at: datetime
    fresh: bool


@dataclass
class _Stored(Generic[T]):
    value: T
    retrieved_at: datetime
    stored_at: float


class TTLCache(Generic[T]):
    def __init__(
        self,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[str, _Stored[T]] = {}
        self._lock = asyncio.Lock()

    async def get_or_fetch(self, key: str, fetch: Callable[[], Awaitable[T]]) -> CacheEntry[T]:
        async with self._lock:
            stored = self._entries.get(key)
            if stored is not None and self._clock() - stored.stored_at < self._ttl:
                return CacheEntry(stored.value, stored.retrieved_at, fresh=False)
        value = await fetch()
        entry = _Stored(value=value, retrieved_at=utcnow(), stored_at=self._clock())
        async with self._lock:
            self._entries[key] = entry
        return CacheEntry(value, entry.retrieved_at, fresh=True)
