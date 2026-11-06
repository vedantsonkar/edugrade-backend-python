"""Identifier generation.

Provides the :class:`IdProvider` port and the default UUIDv7 implementation.
UUIDv7 values are time-ordered, which keeps B-tree indexes healthy for the
insert-heavy tables in this system while remaining fully random per value.
"""

from typing import Protocol, runtime_checkable
from uuid import UUID

from uuid_utils.compat import uuid7


@runtime_checkable
class IdProvider(Protocol):
    """Port for generating unique entity identifiers.

    Implementations MUST return a fresh, collision-free, non-nil UUID on every
    call and MUST be safe for concurrent use.
    """

    def new_id(self) -> UUID:
        """Generate a new unique identifier.

        Returns:
            A UUID that has never been returned by this provider before.
        """
        ...


class Uuid7Provider:
    """Default :class:`IdProvider` producing time-ordered UUIDv7 values.

    Used as the column default for every entity primary key and by services
    that need identifiers outside the database (file keys, job names).
    """

    def new_id(self) -> UUID:
        """Generate a new UUIDv7.

        Returns:
            A stdlib :class:`uuid.UUID` holding a UUIDv7 value.
        """
        return uuid7()
