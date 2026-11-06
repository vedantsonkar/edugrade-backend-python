"""Repositories for grades, divisions, and classrooms."""

from sqlalchemy import func, select

from app.domain.enums import UserRole
from app.domain.taxonomy import Classroom, Division, Grade
from app.domain.users import User
from app.repositories.base import BaseRepository


class GradeRepository(BaseRepository[Grade]):
    """Persistence for :class:`Grade`."""

    model = Grade

    async def list_for_school(self, school_id: str) -> list[Grade]:
        """List a school's grades ordered by display order, then name.

        Args:
            school_id: Tenant.

        Returns:
            Ordered grades.
        """
        stmt = (
            select(Grade)
            .where(Grade.school_id == school_id)
            .order_by(Grade.display_order, Grade.name)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return list(rows)

    async def name_exists(self, school_id: str, name: str) -> bool:
        """Whether a grade name already exists in the school.

        Args:
            school_id: Tenant.
            name: Candidate name (case-insensitive).

        Returns:
            ``True`` when a duplicate exists.
        """
        stmt = select(func.count()).select_from(Grade).where(
            Grade.school_id == school_id, func.lower(Grade.name) == name.lower()
        )
        return bool((await self.session.execute(stmt)).scalar_one())


class DivisionRepository(BaseRepository[Division]):
    """Persistence for :class:`Division`."""

    model = Division

    async def list_for_school(self, school_id: str) -> list[Division]:
        """List a school's divisions ordered by name.

        Args:
            school_id: Tenant.

        Returns:
            Ordered divisions.
        """
        stmt = select(Division).where(Division.school_id == school_id).order_by(Division.name)
        rows = (await self.session.execute(stmt)).scalars().all()
        return list(rows)

    async def name_exists(self, school_id: str, name: str) -> bool:
        """Whether a division name already exists in the school.

        Args:
            school_id: Tenant.
            name: Candidate name (case-insensitive).

        Returns:
            ``True`` when a duplicate exists.
        """
        stmt = select(func.count()).select_from(Division).where(
            Division.school_id == school_id, func.lower(Division.name) == name.lower()
        )
        return bool((await self.session.execute(stmt)).scalar_one())


class ClassroomRepository(BaseRepository[Classroom]):
    """Persistence for :class:`Classroom`."""

    model = Classroom

    async def list_for_school(self, school_id: str) -> list[Classroom]:
        """List a school's classrooms with grade/division/teacher loaded.

        Args:
            school_id: Tenant.

        Returns:
            Ordered classrooms.
        """
        stmt = (
            select(Classroom)
            .where(Classroom.school_id == school_id)
            .order_by(Classroom.grade_id, Classroom.division_id)
        )
        rows = (await self.session.execute(stmt)).scalars().unique().all()
        return list(rows)

    async def pair_exists(
        self, school_id: str, grade_id: str, division_id: str
    ) -> bool:
        """Whether a (grade, division) classroom already exists.

        Args:
            school_id: Tenant.
            grade_id: Grade reference.
            division_id: Division reference.

        Returns:
            ``True`` when the pair is taken.
        """
        stmt = select(func.count()).select_from(Classroom).where(
            Classroom.school_id == school_id,
            Classroom.grade_id == grade_id,
            Classroom.division_id == division_id,
        )
        return bool((await self.session.execute(stmt)).scalar_one())

    async def count_without_teacher(self, school_id: str) -> int:
        """Count classrooms missing a class teacher.

        Args:
            school_id: Tenant.

        Returns:
            Number of unstaffed classrooms.
        """
        stmt = (
            select(func.count())
            .select_from(Classroom)
            .where(
                Classroom.school_id == school_id,
                Classroom.class_teacher_id.is_(None),
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def count_for_school(self, school_id: str) -> int:
        """Count all classrooms in a school.

        Args:
            school_id: Tenant.

        Returns:
            Classroom count.
        """
        stmt = (
            select(func.count())
            .select_from(Classroom)
            .where(Classroom.school_id == school_id)
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def active_teacher_picklist(self, school_id: str) -> list[User]:
        """List active teachers for assignment picklists.

        Args:
            school_id: Tenant.

        Returns:
            Teachers ordered by name.
        """
        stmt = (
            select(User)
            .where(
                User.school_id == school_id,
                User.role == UserRole.TEACHER,
                User.is_active.is_(True),
            )
            .order_by(User.name)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return list(rows)
