"""Repositories for assignments, targets, questions, and rubric comments."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import ColumnElement, Select, false, func, or_, select
from sqlalchemy.orm import selectinload

from app.core.pagination import PageParams
from app.domain.assignments import Assignment, AssignmentTarget, Question, RubricComment
from app.domain.enums import AssignmentState, AssignmentType
from app.domain.grading import Submission
from app.domain.taxonomy import Classroom
from app.domain.users import User  # noqa: F401 - registers mapper for module-level selectinload
from app.repositories.base import BaseRepository

_ASSIGNMENT_LOADS = (
    selectinload(Assignment.targets).selectinload(AssignmentTarget.classroom),
    selectinload(Assignment.teacher),
)
"""Standard eager loads for assignment list/detail reads."""


def _text_condition(q: str) -> ColumnElement[bool]:
    """Build the title/subject substring condition.

    Args:
        q: Search text.

    Returns:
        OR condition over title and subject.
    """
    like = f"%{q}%"
    return or_(Assignment.title.ilike(like), Assignment.subject.ilike(like))


def _state_condition(state: AssignmentState, now: datetime) -> ColumnElement[bool]:
    """Translate a lifecycle state into a SQL condition.

    Date states use the assignment window; submission/graded states check
    for matching submissions. Per-student scoping is applied by the service
    where relevant.

    Args:
        state: Requested lifecycle state.
        now: Current instant.

    Returns:
        Boolean SQL condition.
    """
    if state is AssignmentState.UPCOMING:
        return Assignment.start_date > now
    if state is AssignmentState.ONGOING:
        return (Assignment.start_date <= now) & (Assignment.due_date >= now)
    if state is AssignmentState.OVERDUE:
        return Assignment.due_date < now
    if state is AssignmentState.SUBMITTED:
        return Assignment.submissions.any()
    graded = or_(
        Submission.grade.is_not(None),
        Submission.ai_grade_letter.is_not(None),
        Submission.is_overridden.is_(True),
    )
    return Assignment.submissions.any(graded)


class AssignmentRepository(BaseRepository[Assignment]):
    """Persistence and role-scoped queries for :class:`Assignment`."""

    model = Assignment

    async def get_scoped(self, assignment_id: UUID, school_id: str) -> Assignment | None:
        """Fetch an assignment enforced to a school.

        Args:
            assignment_id: Assignment id.
            school_id: Tenant.

        Returns:
            The assignment, or ``None``.
        """
        stmt = (
            select(Assignment)
            .where(Assignment.id == assignment_id, Assignment.school_id == school_id)
            .options(*_ASSIGNMENT_LOADS)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def _paginated_filtered(
        self,
        base: Select[tuple[Assignment]],
        params: PageParams,
        *,
        q: str | None,
        state: AssignmentState | None,
        now: datetime,
    ) -> tuple[list[Assignment], int]:
        """Apply shared text/state filters and paginate a base select.

        Args:
            base: Select with role/class scoping already applied.
            params: Pagination window.
            q: Title/subject substring filter.
            state: Optional lifecycle-state filter.
            now: Current instant for date-based states.

        Returns:
            Tuple of (assignments, total).
        """
        stmt = base.options(*_ASSIGNMENT_LOADS)
        if q:
            stmt = stmt.where(_text_condition(q))
        if state is not None:
            stmt = stmt.where(_state_condition(state, now))
        stmt = stmt.order_by(Assignment.due_date.asc(), Assignment.created_at.desc())
        return await self.paginate(stmt, params)

    async def list_for_student(
        self,
        school_id: str,
        class_ids: list[str],
        params: PageParams,
        *,
        q: str | None = None,
        class_filter: list[str] | None = None,
        state: AssignmentState | None = None,
        now: datetime,
    ) -> tuple[list[Assignment], int]:
        """List assignments targeted at the student's classrooms.

        Args:
            school_id: Tenant.
            class_ids: Student's classroom ids.
            params: Pagination window.
            q: Title/subject substring filter.
            class_filter: Optional client-side classroom filter.
            state: Optional lifecycle-state filter.
            now: Current instant.

        Returns:
            Tuple of (assignments, total).
        """
        target_ids = class_filter if class_filter is not None else class_ids
        base = (
            select(Assignment)
            .join(AssignmentTarget, AssignmentTarget.assignment_id == Assignment.id)
            .where(
                Assignment.school_id == school_id,
                AssignmentTarget.class_id.in_(target_ids) if target_ids else false(),
            )
        )
        return await self._paginated_filtered(base, params, q=q, state=state, now=now)

    async def list_for_teacher(
        self,
        school_id: str,
        teacher_id: str,
        params: PageParams,
        *,
        q: str | None = None,
        class_filter: list[str] | None = None,
        state: AssignmentState | None = None,
        now: datetime,
    ) -> tuple[list[Assignment], int]:
        """List assignments visible to a teacher.

        Includes assignments the teacher created plus admin-created ones
        targeting classrooms the teacher leads.

        Args:
            school_id: Tenant.
            teacher_id: Teacher id.
            params: Pagination window.
            q: Title/subject substring filter.
            class_filter: Optional classroom filter.
            state: Optional lifecycle-state filter.
            now: Current instant.

        Returns:
            Tuple of (assignments, total).
        """
        led_assignment_ids = (
            select(AssignmentTarget.assignment_id)
            .join(Classroom, Classroom.id == AssignmentTarget.class_id)
            .where(Classroom.class_teacher_id == teacher_id)
        )
        base = select(Assignment).where(
            Assignment.school_id == school_id,
            or_(
                Assignment.teacher_id == teacher_id,
                Assignment.id.in_(led_assignment_ids),
            ),
        )
        if class_filter is not None:
            base = base.where(
                Assignment.targets.any(AssignmentTarget.class_id.in_(class_filter))
            )
        return await self._paginated_filtered(base, params, q=q, state=state, now=now)

    async def list_for_admin(
        self,
        school_id: str,
        params: PageParams,
        *,
        q: str | None = None,
        class_filter: list[str] | None = None,
        state: AssignmentState | None = None,
        now: datetime,
    ) -> tuple[list[Assignment], int]:
        """List every assignment in the school.

        Args:
            school_id: Tenant.
            params: Pagination window.
            q: Title/subject substring filter.
            class_filter: Optional classroom filter.
            state: Optional lifecycle-state filter.
            now: Current instant.

        Returns:
            Tuple of (assignments, total).
        """
        base = select(Assignment).where(Assignment.school_id == school_id)
        if class_filter is not None:
            base = base.where(
                Assignment.targets.any(AssignmentTarget.class_id.in_(class_filter))
            )
        return await self._paginated_filtered(base, params, q=q, state=state, now=now)

    async def find_due_for_auto_grading(
        self, now: datetime, limit: int = 50
    ) -> list[Assignment]:
        """Find offline assignments whose due-date auto grading is pending.

        Args:
            now: Current instant.
            limit: Safety cap per worker pass.

        Returns:
            Assignments with auto-grade enabled and unprocessed due dates.
        """
        stmt = (
            select(Assignment)
            .where(
                Assignment.type == AssignmentType.OFFLINE,
                Assignment.auto_grade_on_due_date.is_(True),
                Assignment.due_date <= now,
                Assignment.auto_graded_at.is_(None),
            )
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return list(rows)

    async def count_for_school(self, school_id: str) -> int:
        """Count assignments in a school.

        Args:
            school_id: Tenant.

        Returns:
            Assignment count.
        """
        stmt = (
            select(func.count())
            .select_from(Assignment)
            .where(Assignment.school_id == school_id)
        )
        return int((await self.session.execute(stmt)).scalar_one())


class AssignmentTargetRepository(BaseRepository[AssignmentTarget]):
    """Persistence for :class:`AssignmentTarget` join rows."""

    model = AssignmentTarget

    async def replace_for_assignment(
        self, assignment_id: str, class_ids: list[str]
    ) -> None:
        """Replace the target set of an assignment.

        Args:
            assignment_id: Owning assignment.
            class_ids: New classroom id set.
        """
        existing = (
            await self.session.execute(
                select(AssignmentTarget).where(
                    AssignmentTarget.assignment_id == assignment_id
                )
            )
        ).scalars()
        for row in existing:
            await self.session.delete(row)
        for class_id in class_ids:
            await self.add(AssignmentTarget(assignment_id=assignment_id, class_id=class_id))


class QuestionRepository(BaseRepository[Question]):
    """Persistence for :class:`Question` (options cascade with questions)."""

    model = Question

    async def get_loaded(self, question_id: UUID) -> Question | None:
        """Fetch one question with its options eagerly loaded.

        Args:
            question_id: Question id.

        Returns:
            The question, or ``None``.
        """
        stmt = (
            select(Question)
            .where(Question.id == question_id)
            .options(selectinload(Question.options))
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_assignment(self, assignment_id: str) -> list[Question]:
        """List an assignment's questions ordered for display.

        Args:
            assignment_id: Owning assignment.

        Returns:
            Ordered questions with options loaded.
        """
        stmt = (
            select(Question)
            .where(Question.assignment_id == assignment_id)
            .order_by(Question.display_order)
        )
        rows = (await self.session.execute(stmt)).scalars().unique().all()
        return list(rows)

    async def next_display_order(self, assignment_id: str) -> int:
        """Compute the next display order for appended questions.

        Args:
            assignment_id: Owning assignment.

        Returns:
            One past the current maximum (0 when empty).
        """
        stmt = (
            select(func.max(Question.display_order))
            .where(Question.assignment_id == assignment_id)
        )
        current = (await self.session.execute(stmt)).scalar_one()
        return int(current or 0) + 1


class RubricCommentRepository(BaseRepository[RubricComment]):
    """Persistence for :class:`RubricComment` threads."""

    model = RubricComment

    async def get_loaded(self, comment_id: object) -> RubricComment | None:
        """Fetch one comment with its author eagerly loaded.

        Args:
            comment_id: Comment id.

        Returns:
            The comment, or ``None``.
        """
        from sqlalchemy.orm import selectinload

        stmt = (
            select(RubricComment)
            .where(RubricComment.id == comment_id)
            .options(selectinload(RubricComment.author))
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_assignment(self, assignment_id: str) -> list[RubricComment]:
        """List a thread's comments oldest-first.

        Args:
            assignment_id: Thread owner.

        Returns:
            Comments with authors loaded.
        """
        stmt = (
            select(RubricComment)
            .where(RubricComment.assignment_id == assignment_id)
            .order_by(RubricComment.created_at)
        )
        rows = (await self.session.execute(stmt)).scalars().unique().all()
        return list(rows)
