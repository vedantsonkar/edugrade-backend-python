"""Repositories for users and refresh tokens."""

from typing import Any
from uuid import UUID

from sqlalchemy import Select, delete, func, select

from app.domain.enums import UserRole
from app.domain.users import RefreshToken, User
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    """Persistence and tenant-scoped queries for :class:`User`."""

    model = User

    async def get_by_email(self, email: str) -> User | None:
        """Fetch a user by email (case-insensitive).

        Args:
            email: Email address.

        Returns:
            The user, or ``None``.
        """
        stmt = select(User).where(func.lower(User.email) == email.lower())
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def scoped(self, school_id: str, role: UserRole | None = None) -> Select[tuple[User]]:
        """Build a school-scoped user select, optionally by role.

        Args:
            school_id: Tenant to restrict to.
            role: Optional role filter.

        Returns:
            A select awaiting ordering/pagination by the caller.
        """
        stmt = select(User).where(User.school_id == school_id)
        if role is not None:
            stmt = stmt.where(User.role == role)
        return stmt.order_by(User.name)

    async def count_active(self, school_id: str, role: UserRole) -> int:
        """Count active users of a role in a school.

        Args:
            school_id: Tenant.
            role: Role to count.

        Returns:
            Number of matching active users.
        """
        stmt = (
            select(func.count())
            .select_from(User)
            .where(
                User.school_id == school_id,
                User.role == role,
                User.is_active.is_(True),
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def count_by_grade(self, school_id: str) -> dict[str, int]:
        """Count active students grouped by grade id.

        Args:
            school_id: Tenant.

        Returns:
            Mapping of grade id → active student count.
        """
        stmt = (
            select(User.grade_id, func.count())
            .where(
                User.school_id == school_id,
                User.role == UserRole.STUDENT,
                User.is_active.is_(True),
                User.grade_id.is_not(None),
            )
            .group_by(User.grade_id)
        )
        rows = (await self.session.execute(stmt)).all()
        return {str(grade_id): int(count) for grade_id, count in rows}

    async def count_by_division(self, school_id: str) -> dict[str, int]:
        """Count active students grouped by division id.

        Args:
            school_id: Tenant.

        Returns:
            Mapping of division id → active student count.
        """
        stmt = (
            select(User.division_id, func.count())
            .where(
                User.school_id == school_id,
                User.role == UserRole.STUDENT,
                User.is_active.is_(True),
                User.division_id.is_not(None),
            )
            .group_by(User.division_id)
        )
        rows = (await self.session.execute(stmt)).all()
        return {str(division_id): int(count) for division_id, count in rows}


class RefreshTokenRepository(BaseRepository[RefreshToken]):
    """Persistence for rotating :class:`RefreshToken` rows.

    The repository performs state changes it is told to make; deciding
    *when* tokens are revoked (the clock) is the auth service's job.
    """

    model = RefreshToken

    async def get_by_jti(self, jti: UUID) -> RefreshToken | None:
        """Fetch a token row by its ``jti``.

        Args:
            jti: Token unique id.

        Returns:
            The row, or ``None``.
        """
        stmt = select(RefreshToken).where(RefreshToken.jti == jti)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def revoke_all_for_user(self, user_id: str) -> int:
        """Delete every token row of a user (e.g. password reset).

        Args:
            user_id: Owner id.

        Returns:
            Number of rows removed.
        """
        from typing import cast

        from sqlalchemy import CursorResult

        result = cast(
            "CursorResult[Any]",
            await self.session.execute(
                delete(RefreshToken).where(RefreshToken.user_id == user_id)
            ),
        )
        return int(result.rowcount or 0)
