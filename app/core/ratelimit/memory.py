"""In-memory fixed-window rate limiter.

Process-local implementation used in tests and single-process development.
NOT suitable for multi-worker production deployments - use
:class:`app.core.ratelimit.redis_limiter.RedisRateLimiter` there.
"""

import asyncio
import time

from app.core.ratelimit.base import RateLimitResult, window_epoch


class MemoryRateLimiter:
    """Fixed-window :class:`RateLimiter` kept in a process-local dictionary.

    Counters are ``{key: (window_epoch, hits)}``; a monotonic-free approach is
    sufficient because windows are coarse (minutes). Safe under asyncio
    concurrency via a lock (single event loop - no thread contention).
    """

    def __init__(self) -> None:
        """Initialize an empty counter store."""
        self._counters: dict[str, tuple[int, int]] = {}
        self._lock = asyncio.Lock()

    async def check(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        """Record one hit and evaluate the fixed-window limit.

        Args:
            key: Unique bucket key.
            limit: Maximum requests allowed per window.
            window_seconds: Window length in seconds.

        Returns:
            Evaluation result for this request.
        """
        epoch = window_epoch(window_seconds)
        async with self._lock:
            current_epoch, hits = self._counters.get(key, (epoch, 0))
            if current_epoch != epoch:
                current_epoch, hits = epoch, 0
            hits += 1
            self._counters[key] = (current_epoch, hits)

        remaining = max(0, limit - hits)
        reset_after = window_seconds - int(time.time() % window_seconds)
        if hits > limit:
            return RateLimitResult(
                allowed=False,
                limit=limit,
                remaining=0,
                reset_after=reset_after,
                retry_after=reset_after,
            )
        return RateLimitResult(
            allowed=True,
            limit=limit,
            remaining=remaining,
            reset_after=reset_after,
        )
