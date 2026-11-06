"""Audit-log archiver worker: archive events older than 24 hours."""

import json
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.core.db.engine import session_scope
from app.core.storage.base import ObjectStorage
from app.core.storage.keys import audit_archive_key
from app.domain.audit import AuditLog
from app.repositories.audit import AuditLogRepository

logger = logging.getLogger(__name__)

ARCHIVE_AFTER: Final[timedelta] = timedelta(hours=24)
"""Honest 24-hour retention (the legacy doc said 24h but cut at midnight)."""


class AuditArchiver:
    """Moves aged audit events into per-school daily archive files."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        storage: ObjectStorage,
        clock: Clock,
    ) -> None:
        """Inject collaborators.

        Args:
            sessionmaker: Session factory (repo built per pass).
            storage: Object storage port.
            clock: Time source.
        """
        self._sessionmaker = sessionmaker
        self._storage = storage
        self._clock = clock

    async def archive_and_purge(self) -> dict[str, int]:
        """Archive + purge events older than :data:`ARCHIVE_AFTER`.

        Returns:
            Counters ``{"archived", "purged"}``.
        """
        cutoff = self._clock.now() - ARCHIVE_AFTER
        async with session_scope(self._sessionmaker) as session:
            logs = AuditLogRepository(session)
            return await self._archive_and_purge(logs, cutoff)

    async def _archive_and_purge(
        self, logs: AuditLogRepository, cutoff: datetime
    ) -> dict[str, int]:
        """Upload per-school daily archives, then purge archived rows.

        Args:
            logs: Audit-log repository bound to the pass session.
            cutoff: Exclusive age boundary.

        Returns:
            Counters ``{"archived", "purged"}``.
        """
        rows = await logs.find_older_than(cutoff)
        if not rows:
            return {"archived": 0, "purged": 0}

        by_school_day: dict[tuple[str, str], list[AuditLog]] = defaultdict(list)
        for row in rows:
            created = (
                row.created_at
                if row.created_at.tzinfo
                else row.created_at.replace(tzinfo=UTC)
            )
            day = created.astimezone(UTC)
            by_school_day[(row.school_id, day.date().isoformat())].append(row)

        archived = 0
        for (school_id, day_key), events in by_school_day.items():
            payload = json.dumps(
                [
                    {
                        "id": str(event.id),
                        "action": event.action,
                        "resource": event.resource,
                        "user_id": event.user_id,
                        "details": event.details,
                        "created_at": event.created_at.isoformat(),
                    }
                    for event in events
                ],
                default=str,
            ).encode("utf-8")
            archive_day = datetime.strptime(day_key, "%Y-%m-%d").date()
            key = audit_archive_key(school_id, archive_day)
            await self._storage.put_bytes(key, payload, "application/json")
            archived += len(events)

        purged = await logs.purge(rows)
        return {"archived": archived, "purged": purged}
