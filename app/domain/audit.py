"""Audit log entity - append-only record of security-relevant events.

Rows are written by server actions (via :class:`AuditService`) and by batched
client events (``POST /analytics/batch``). A daily worker archives rows older
than 24 hours to object storage and purges them.
"""

from datetime import datetime

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.core.db.base import Base, UUID7PKMixin, utc_now


class AuditLog(UUID7PKMixin, Base):
    """One audited event within a school.

    Attributes:
        user_id: Acting user, if known (no FK: logs must survive user deletion).
        school_id: Tenant the event belongs to.
        action: Machine-readable event name (e.g. ``USER_LOGIN``).
        resource: Affected resource identifier, when applicable.
        details: Structured event payload.
        ip_address / user_agent: Request provenance.
        created_at: Event instant.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_school_created", "school_id", "created_at"),
    )

    user_id: Mapped[str | None] = mapped_column(String(36), index=True)
    school_id: Mapped[str] = mapped_column(String(36), index=True)
    action: Mapped[str] = mapped_column(String(128))
    resource: Mapped[str | None] = mapped_column(String(255))
    details: Mapped[dict[str, object] | None] = mapped_column(JSON)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
