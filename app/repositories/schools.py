"""Repositories for schools and school configs."""

from sqlalchemy import select

from app.domain.schools import School, SchoolConfig
from app.repositories.base import BaseRepository


class SchoolRepository(BaseRepository[School]):
    """Persistence for :class:`School` tenants."""

    model = School

    async def get_by_school_key(self, school_key: str) -> School | None:
        """Fetch a school by its public school key.

        Args:
            school_key: Case-insensitive key as typed by the user.

        Returns:
            The school, or ``None``.
        """
        stmt = select(School).where(School.school_key == school_key.lower())
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_admin_key(self, admin_key: str) -> School | None:
        """Fetch a school by its onboarding admin key.

        Args:
            admin_key: Raw key as presented by the client.

        Returns:
            The school, or ``None``.
        """
        stmt = select(School).where(School.admin_key == admin_key)
        return (await self.session.execute(stmt)).scalar_one_or_none()


class SchoolConfigRepository(BaseRepository[SchoolConfig]):
    """Persistence for :class:`SchoolConfig` (strict 1:1 with schools)."""

    model = SchoolConfig

    async def get_for_school(self, school_id: str) -> SchoolConfig | None:
        """Fetch the config of a school.

        Args:
            school_id: Owning school id.

        Returns:
            The config, or ``None`` when not provisioned.
        """
        stmt = select(SchoolConfig).where(SchoolConfig.school_id == school_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()
