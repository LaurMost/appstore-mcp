"""In-memory async-safe TTL cache. No disk, no Redis - agent-loop refetch guard."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from appstore_mcp.models import utcnow

DEFAULT_TTL_SECONDS = 15 * 60


@dataclass
class CacheEntry[T]:
    value: T
    retrieved_at: datetime
    fresh: bool


@dataclass
class _Stored[T]:
    value: T
    retrieved_at: datetime
    stored_at: float


class TTLCache[T]:
    def __init__(
        self,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[str, _Stored[T]] = {}
        self._in_flight: dict[str, asyncio.Future[CacheEntry[T]]] = {}
        self._lock = asyncio.Lock()
        self._last_swept_at = clock()

    def _fresh_entry_locked(self, key: str) -> _Stored[T] | None:
        """Must be called while holding self._lock. Evicts the entry if expired."""
        stored = self._entries.get(key)
        if stored is None:
            return None
        if self._clock() - stored.stored_at >= self._ttl:
            del self._entries[key]
            return None
        return stored

    def _sweep_expired_locked(self) -> None:
        """Must be called while holding self._lock.

        Bounds memory for keys that expire but are never looked up again by
        purging every expired entry, not just the one being requested.
        Throttled to once per TTL window so a busy cache isn't scanned on
        every call.
        """
        now = self._clock()
        if now - self._last_swept_at < self._ttl:
            return
        expired = [
            k
            for k, entry in self._entries.items()
            if now - entry.stored_at >= self._ttl
        ]
        for k in expired:
            del self._entries[k]
        self._last_swept_at = now

    async def get_or_fetch(
        self, key: str, fetch: Callable[[], Awaitable[T]]
    ) -> CacheEntry[T]:
        async with self._lock:
            self._sweep_expired_locked()
            stored = self._fresh_entry_locked(key)
            if stored is not None:
                return CacheEntry(stored.value, stored.retrieved_at, fresh=False)

            # Single-flight: concurrent callers for the same not-yet-cached key
            # await the one in-progress fetch instead of each triggering their own.
            future = self._in_flight.get(key)
            if future is not None:
                is_owner = False
            else:
                future = asyncio.get_running_loop().create_future()
                self._in_flight[key] = future
                is_owner = True

        if not is_owner:
            return await future

        try:
            value = await fetch()
            entry = _Stored(value=value, retrieved_at=utcnow(), stored_at=self._clock())
            async with self._lock:
                self._entries[key] = entry
            result = CacheEntry(value, entry.retrieved_at, fresh=True)
            future.set_result(result)
            return result
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            async with self._lock:
                del self._in_flight[key]
