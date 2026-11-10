"""School tenant entities: :class:`School` and its 1:1 :class:`SchoolConfig`.

A school is the tenancy boundary: every other entity is scoped (directly or
transitively) by a school id, and all repositories filter on it.
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db.base import UUID_KEY, Base, TimestampMixin, UUID7PKMixin
from app.domain.enums import BatchGradingTaskStatus

if TYPE_CHECKING:
    from app.domain.grading import BatchGradingTask
    from app.domain.taxonomy import Classroom
    from app.domain.users import User


class School(UUID7PKMixin, TimestampMixin, Base):
    """A tenant school with branding, contact details, and its admin key.

    Attributes:
        name: Display name.
        school_key: Public identifier users type on the login page, unique
            across the platform.
        primary_color: Brand hex color (``#rrggbb``).
        logo_url: Storage key of the school logo, if any.
        admin_key: Onboarding secret used by ``POST /school/validate-admin``.
        is_active: Whether the school is enabled; disabled schools are
            invisible to lookups and reject logins.
        deleted_at: Soft-delete timestamp; ``None`` while the school exists.
        phone_country_code / phone_number / address / city / state / country /
            postal_code: Contact and location details.
        default_locale: BCP-47 default locale for communications.
        timezone: IANA timezone name.
    """

    __tablename__ = "schools"

    name: Mapped[str] = mapped_column(String(255))
    school_key: Mapped[str] = mapped_column(String(63), unique=True, index=True)
    primary_color: Mapped[str] = mapped_column(String(9), default="#3b82f6")
    logo_url: Mapped[str | None] = mapped_column(String(512))
    admin_key: Mapped[str] = mapped_column(String(255), unique=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", index=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    phone_country_code: Mapped[str] = mapped_column(String(8), default="+91")
    phone_number: Mapped[str | None] = mapped_column(String(15))
    address: Mapped[str | None] = mapped_column(String(512))
    city: Mapped[str | None] = mapped_column(String(128))
    state: Mapped[str | None] = mapped_column(String(128))
    country: Mapped[str] = mapped_column(String(2), default="IN")
    postal_code: Mapped[str | None] = mapped_column(String(16))
    default_locale: Mapped[str] = mapped_column(String(10), default="en")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")

    config: Mapped[SchoolConfig] = relationship(
        back_populates="school", uselist=False, lazy="selectin"
    )
    users: Mapped[list[User]] = relationship(back_populates="school")
    classrooms: Mapped[list[Classroom]] = relationship(back_populates="school")
    batch_tasks: Mapped[list[BatchGradingTask]] = relationship(back_populates="school")

    @property
    def is_available(self) -> bool:
        """Whether the school is usable: not disabled and not soft-deleted."""
        return self.is_active and self.deleted_at is None


class SchoolConfig(UUID7PKMixin, TimestampMixin, Base):
    """Feature and academic-year configuration for exactly one school.

    Attributes:
        school_id: Owning school (unique - strict 1:1).
        allow_multiple_class_teachers: Whether a teacher may lead several
            classrooms simultaneously.
        enable_logging: Whether client-side analytics events are recorded.
        enable_vision_ocr: Whether AI grading may run OCR over attachments.
        current_academic_year: Academic year label (e.g. ``2025-2026``).
    """

    __tablename__ = "school_configs"
    __table_args__ = (UniqueConstraint("school_id", name="uq_school_configs_school_id"),)

    school_id: Mapped[str] = mapped_column(
        UUID_KEY, ForeignKey("schools.id", ondelete="CASCADE"), index=True
    )
    allow_multiple_class_teachers: Mapped[bool] = mapped_column(Boolean, default=False)
    enable_logging: Mapped[bool] = mapped_column(Boolean, default=False)
    enable_vision_ocr: Mapped[bool] = mapped_column(Boolean, default=True)
    current_academic_year: Mapped[str] = mapped_column(String(16), default="2025-2026")

    school: Mapped[School] = relationship(back_populates="config")


def task_status_values() -> list[str]:
    """List batch task status values (utility for reporting queries).

    Returns:
        All :class:`BatchGradingTaskStatus` values as strings.
    """
    return [status.value for status in BatchGradingTaskStatus]
