"""Declarative base, naming conventions, and shared entity mixins.

All ORM entities inherit :class:`Base` and use :class:`UUID7PKMixin` (native
``uuid`` column holding UUIDv7 values) plus :class:`TimestampMixin`. A naming
convention is attached so Alembic generates deterministic constraint names.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import DateTime, MetaData, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator
from uuid_utils.compat import uuid7

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
"""Deterministic constraint naming for Alembic autogenerate stability."""


class FlexibleUuid(TypeDecorator[UUID]):
    """UUID column accepting both UUID objects and their string forms.

    Services pass ids as strings at the boundary (schema aliases) while the
    database stores native UUID (PG) / CHAR(32) (sqlite). Coercion happens
    here, once, so neither layer ever sees a mismatched type.
    """

    impl = Uuid
    cache_ok = True

    def process_bind_param(self, value: object, dialect: object) -> UUID | None:
        """Coerce the bind value into a UUID.

        Args:
            value: UUID or string form (or ``None``).
            dialect: Active dialect (unused).

        Returns:
            UUID instance, or ``None``.
        """
        import uuid as _uuid

        if value is None or isinstance(value, _uuid.UUID):
            return value
        return _uuid.UUID(str(value))

    def process_result_value(self, value: object, dialect: object) -> UUID | None:
        """Normalize result values to stdlib UUID.

        Args:
            value: Driver-provided value.
            dialect: Active dialect (unused).

        Returns:
            UUID instance, or ``None``.
        """
        import uuid as _uuid

        if value is None or isinstance(value, _uuid.UUID):
            return value
        return _uuid.UUID(str(value))


UUID_KEY = FlexibleUuid()
"""Shared column type for every primary/foreign key."""


def utc_now() -> datetime:
    """Return the current UTC instant (timezone-aware).

    Returns:
        Timezone-aware UTC datetime for column defaults.
    """
    return datetime.now(UTC)


def new_uuid7() -> UUID:
    """Generate a UUIDv7 primary key value.

    Returns:
        Time-ordered UUIDv7 as a stdlib UUID.
    """
    return uuid7()


class Base(DeclarativeBase):
    """Declarative base for every ORM entity in the system."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUID7PKMixin:
    """Adds a ``id`` primary key column backed by native UUID (UUIDv7 values)."""

    id: Mapped[UUID] = mapped_column(
        UUID_KEY,
        primary_key=True,
        default=new_uuid7,
        sort_order=-100,
        doc="UUIDv7 primary key.",
    )


class TimestampMixin:
    """Adds timezone-aware creation/update audit timestamps."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        sort_order=90,
        doc="Row creation timestamp (UTC).",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        sort_order=91,
        doc="Row last-update timestamp (UTC), maintained on UPDATE.",
    )
