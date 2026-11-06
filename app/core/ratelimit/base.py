"""Rate-limiting port and limit-spec parsing.

The :class:`RateLimiter` protocol decouples endpoint protection from the
storage backend (Redis in prod, in-memory in tests - AGENTS.md §2-O/D).
"""

import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.core.errors import ValidationFailedError


@dataclass(frozen=True)
class RateLimitResult:
    """Outcome of a rate-limit check.

    Attributes:
        allowed: Whether the request may proceed.
        limit: Configured maximum requests per window.
        remaining: Requests left in the current window.
        reset_after: Seconds until the window resets.
        retry_after: Seconds the caller should wait when not allowed.
    """

    allowed: bool
    limit: int
    remaining: int
    reset_after: int
    retry_after: int = 0


@runtime_checkable
class RateLimiter(Protocol):
    """Port for fixed-window rate limiting.

    Implementations MUST be atomic under concurrency (two simultaneous calls
    must not both see the same counter value) and MUST NOT raise on backend
    unavailability other than via documented exceptions.
    """

    async def check(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        """Record one request against the key and evaluate the limit.

        Args:
            key: Unique bucket key (typically route + user/ip).
            limit: Maximum requests allowed per window.
            window_seconds: Window length in seconds.

        Returns:
            Evaluation result for this request.
        """
        ...


UNIT_SECONDS: dict[str, int] = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
}
"""Supported limit-spec time units mapped to window lengths in seconds."""


def parse_limit(spec: str) -> tuple[int, int]:
    """Parse a limit spec like ``"30/minute"`` into ``(limit, window_seconds)``.

    Args:
        spec: Limit spec in ``"<count>/<unit>"`` form.

    Returns:
        Tuple of (allowed request count, window length in seconds).

    Raises:
        ValidationFailedError: If the spec is malformed or the unit is unknown.
    """
    try:
        count_raw, unit_raw = spec.split("/", maxsplit=1)
        count = int(count_raw.strip())
        unit = unit_raw.strip().lower()
    except ValueError as exc:
        raise ValidationFailedError(
            f"Invalid rate limit spec: {spec!r}. Expected '<count>/<unit>'."
        ) from exc
    if count <= 0 or unit not in UNIT_SECONDS:
        raise ValidationFailedError(
            f"Invalid rate limit spec: {spec!r}. "
            f"Unit must be one of {sorted(UNIT_SECONDS)}."
        )
    return count, UNIT_SECONDS[unit]


def window_epoch(window_seconds: int, *, now: float | None = None) -> int:
    """Return the epoch index of the fixed window containing ``now``.

    Args:
        window_seconds: Window length in seconds.
        now: Optional explicit timestamp (seconds since epoch); defaults to
            the real clock. Exposed for deterministic tests.

    Returns:
        Integer index of the current window (floor of now / window).
    """
    current = time.time() if now is None else now
    return int(current // window_seconds)
