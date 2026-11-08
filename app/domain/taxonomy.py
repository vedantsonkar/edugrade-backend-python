"""Academic taxonomy: :class:`Grade`, :class:`Division`, and :class:`Classroom`.

A classroom is the (grade, division) pair students belong to, optionally led
by a class teacher. Assignments target classrooms via ``AssignmentTarget``.
"""

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db.base import UUID_KEY, Base, TimestampMixin, UUID7PKMixin

if TYPE_CHECKING:
    from app.domain.assignments import AssignmentTarget
    from app.domain.schools import School
    from app.domain.users import User


class Grade(UUID7PKMixin, TimestampMixin, Base):
    """A year level within a school (e.g. ``Grade 9``).

    Attributes:
        school_id: Owning school.
        name: Display name.
        display_order: Sort key for listings.
        is_custom: Whether the grade name was school-defined (vs preset).
    """

    __tablename__ = "grades"

    school_id: Mapped[str] = mapped_column(
        UUID_KEY, ForeignKey("schools.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    display_order: Mapped[int] = mapped_column(Integer, default=0, index=True)
    is_custom: Mapped[bool] = mapped_column(default=False)

    school: Mapped[School] = relationship(lazy="selectin")


class Division(UUID7PKMixin, TimestampMixin, Base):
    """A section within a grade (e.g. ``A``).

    Attributes:
        school_id: Owning school.
        name: Display name.
    """

    __tablename__ = "divisions"

    school_id: Mapped[str] = mapped_column(
        UUID_KEY, ForeignKey("schools.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))

    classrooms: Mapped[list[Classroom]] = relationship(back_populates="division")


class Classroom(UUID7PKMixin, TimestampMixin, Base):
    """A (grade, division) class group, optionally led by a teacher.

    Attributes:
        school_id: Owning school.
        grade_id: Grade of the classroom.
        division_id: Division of the classroom.
        class_teacher_id: Leading teacher, if assigned.
        academic_year: Academic year the classroom exists in.
    """

    __tablename__ = "classrooms"

    school_id: Mapped[str] = mapped_column(
        UUID_KEY, ForeignKey("schools.id", ondelete="CASCADE"), index=True
    )
    grade_id: Mapped[str] = mapped_column(
        UUID_KEY, ForeignKey("grades.id", ondelete="CASCADE"), index=True
    )
    division_id: Mapped[str] = mapped_column(
        UUID_KEY, ForeignKey("divisions.id", ondelete="CASCADE"), index=True
    )
    class_teacher_id: Mapped[str | None] = mapped_column(
        UUID_KEY, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    academic_year: Mapped[str] = mapped_column(String(16), index=True)

    school: Mapped[School] = relationship(back_populates="classrooms")
    grade: Mapped[Grade] = relationship(lazy="selectin")
    division: Mapped[Division] = relationship(lazy="selectin")
    class_teacher: Mapped[User | None] = relationship(lazy="selectin")
    assignment_targets: Mapped[list[AssignmentTarget]] = relationship(back_populates="classroom")
