"""Time access port.

Services must never call ``datetime.now()`` directly (AGENTS.md §2-D): the
:class:`Clock` port keeps them deterministic under test and lets workers/CRON
code share the same time source as request handling.
"""

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Port for reading the current time.

    Implementations MUST return timezone-aware UTC datetimes.
    """

    def now(self) -> datetime:
        """Return the current instant.

        Returns:
            Current UTC time, timezone-aware.
        """
        ...


class SystemClock:
    """Default :class:`Clock` backed by the wall clock."""

    def now(self) -> datetime:
        """Return the current wall-clock time in UTC.

        Returns:
            Current UTC time, timezone-aware.
        """
        return datetime.now(UTC)
