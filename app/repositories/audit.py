"""Repositories for audit logs."""

from datetime import datetime

from sqlalchemy import delete, func, select

from app.core.pagination import PageParams
from app.domain.audit import AuditLog
from app.repositories.base import BaseRepository


class AuditLogRepository(BaseRepository[AuditLog]):
    """Persistence and archival queries for :class:`AuditLog`."""

    model = AuditLog

    async def add_event(
        self,
        *,
        school_id: str,
        action: str,
        user_id: str | None,
        resource: str | None,
        details: dict[str, object] | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> AuditLog:
        """Append one audit event.

        Args:
            school_id: Tenant.
            action: Event name.
            user_id: Acting user, when known.
            resource: Affected resource, when applicable.
            details: Structured payload.
            ip_address: Caller IP.
            user_agent: Caller user agent.

        Returns:
            The persisted row.
        """
        return await self.add(
            AuditLog(
                school_id=school_id,
                action=action,
                user_id=user_id,
                resource=resource,
                details=details,
                ip_address=ip_address,
                user_agent=user_agent,
            )
        )

    async def list_for_school(
        self,
        school_id: str,
        params: PageParams,
        *,
        action: str | None = None,
        user_id: str | None = None,
    ) -> tuple[list[AuditLog], int]:
        """List a school's events newest-first with optional filters.

        Args:
            school_id: Tenant.
            params: Pagination window.
            action: Exact-action filter.
            user_id: Acting-user filter.

        Returns:
            Tuple of (logs, total).
        """
        stmt = select(AuditLog).where(AuditLog.school_id == school_id)
        if action:
            stmt = stmt.where(AuditLog.action == action)
        if user_id:
            stmt = stmt.where(AuditLog.user_id == user_id)
        stmt = stmt.order_by(AuditLog.created_at.desc())
        return await self.paginate(stmt, params)

    async def find_older_than(self, cutoff: datetime) -> list[AuditLog]:
        """Fetch events older than the cutoff, grouped scan for archiving.

        Args:
            cutoff: Exclusive boundary.

        Returns:
            Rows oldest-first.
        """
        stmt = (
            select(AuditLog)
            .where(AuditLog.created_at < cutoff)
            .order_by(AuditLog.created_at)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return list(rows)

    async def purge(self, rows: list[AuditLog]) -> int:
        """Delete archived rows.

        Args:
            rows: Rows to remove.

        Returns:
            Number of rows removed.
        """
        ids = [row.id for row in rows]
        if not ids:
            return 0
        from typing import Any, cast

        from sqlalchemy import CursorResult

        result = cast(
            "CursorResult[Any]",
            await self.session.execute(delete(AuditLog).where(AuditLog.id.in_(ids))),
        )
        return int(result.rowcount or 0)

    async def count_for_school(self, school_id: str) -> int:
        """Count a school's stored events.

        Args:
            school_id: Tenant.

        Returns:
            Event count.
        """
        stmt = (
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.school_id == school_id)
        )
        return int((await self.session.execute(stmt)).scalar_one())
