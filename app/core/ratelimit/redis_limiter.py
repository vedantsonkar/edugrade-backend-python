"""Redis-backed fixed-window rate limiter.

Production implementation: atomic INCR + conditional EXPIRE via a pipeline,
correct across uvicorn workers and replicas (the legacy backend's in-memory
limiter was per-process, AGENTS.md §1 rationale). The only module allowed to
import the ``redis`` client.
"""

import time

from redis.asyncio import Redis

from app.core.ratelimit.base import RateLimitResult, window_epoch


class RedisRateLimiter:
    """Fixed-window :class:`RateLimiter` backed by Redis counters.

    Counter keys carry the window epoch (``rl:{key}:{epoch}``), so expiry is
    natural: the EXPIRE is only set on the first hit of a window and stale
    buckets vanish on their own.
    """

    def __init__(self, client: Redis) -> None:
        """Bind the limiter to a Redis client.

        Args:
            client: Async Redis client (decode_responses recommended).
        """
        self._client = client

    async def check(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        """Atomically record one hit and evaluate the fixed-window limit.

        Args:
            key: Unique bucket key.
            limit: Maximum requests allowed per window.
            window_seconds: Window length in seconds.

        Returns:
            Evaluation result for this request; on Redis failure the call
            raises (fail-closed) so callers surface a 503 rather than
            silently disabling protection.
        """
        bucket = f"rl:{key}:{window_epoch(window_seconds)}"
        pipeline = self._client.pipeline()
        pipeline.incr(bucket)
        pipeline.expire(bucket, window_seconds, nx=True)
        pipeline.ttl(bucket)
        hits_raw, _, ttl_raw = await pipeline.execute()
        hits = int(hits_raw)
        ttl = int(ttl_raw) if ttl_raw and int(ttl_raw) > 0 else window_seconds
        return self._evaluate(hits, limit, ttl)

    @staticmethod
    def _evaluate(hits: int, limit: int, ttl: int) -> RateLimitResult:
        """Translate raw counter values into a result.

        Args:
            hits: Current window hit count including this request.
            limit: Configured maximum.
            ttl: Seconds until the window resets.

        Returns:
            Evaluation result for this request.
        """
        if hits > limit:
            return RateLimitResult(
                allowed=False,
                limit=limit,
                remaining=0,
                reset_after=ttl,
                retry_after=ttl,
            )
        return RateLimitResult(
            allowed=True,
            limit=limit,
            remaining=max(0, limit - hits),
            reset_after=ttl,
        )


def sleep_until_window_end(window_seconds: int) -> float:
    """Compute seconds until the end of the current fixed window (utility).

    Args:
        window_seconds: Window length in seconds.

    Returns:
        Fractional seconds remaining in the window.
    """
    return window_seconds - (time.time() % window_seconds)
