"""Repositories for assessment attempts and student answers."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.pagination import PageParams
from app.domain.assessments import AssessmentAttempt, StudentAnswer
from app.domain.enums import AttemptStatus
from app.repositories.base import BaseRepository


class AssessmentAttemptRepository(BaseRepository[AssessmentAttempt]):
    """Persistence and student-scoped queries for :class:`AssessmentAttempt`."""

    model = AssessmentAttempt

    async def get_for_student(
        self, assignment_id: UUID, student_id: str
    ) -> AssessmentAttempt | None:
        """Fetch a student's attempt for an assignment (any status).

        Args:
            assignment_id: Assignment id.
            student_id: Student id.

        Returns:
            The attempt, or ``None``.
        """
        stmt = (
            select(AssessmentAttempt)
            .where(
                AssessmentAttempt.assignment_id == assignment_id,
                AssessmentAttempt.student_id == student_id,
            )
            .options(
                selectinload(AssessmentAttempt.answers), selectinload(AssessmentAttempt.student)
            )
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_active(self, assignment_id: UUID, student_id: str) -> AssessmentAttempt | None:
        """Fetch a student's in-progress attempt, if any.

        Args:
            assignment_id: Assignment id.
            student_id: Student id.

        Returns:
            The active attempt, or ``None``.
        """
        stmt = (
            select(AssessmentAttempt)
            .where(
                AssessmentAttempt.assignment_id == assignment_id,
                AssessmentAttempt.student_id == student_id,
                AssessmentAttempt.status == AttemptStatus.IN_PROGRESS,
            )
            .options(
                selectinload(AssessmentAttempt.answers), selectinload(AssessmentAttempt.student)
            )
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_id(self, attempt_id: UUID, school_id: str) -> AssessmentAttempt | None:
        """Fetch an attempt enforced to a school (join through assignment).

        Args:
            attempt_id: Attempt id.
            school_id: Tenant.

        Returns:
            The attempt, or ``None``.
        """
        stmt = (
            select(AssessmentAttempt)
            .join(AssessmentAttempt.assignment)
            .where(
                AssessmentAttempt.id == attempt_id,
                AssessmentAttempt.assignment.has(school_id=school_id),
            )
            .options(
                selectinload(AssessmentAttempt.answers), selectinload(AssessmentAttempt.student)
            )
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_assignment(
        self, assignment_id: str, params: PageParams
    ) -> tuple[list[AssessmentAttempt], int]:
        """List a (student-identifiable subset of) attempts for staff review.

        Args:
            assignment_id: Assignment id.
            params: Pagination window.

        Returns:
            Tuple of (attempts, total).
        """
        stmt = (
            select(AssessmentAttempt)
            .where(AssessmentAttempt.assignment_id == assignment_id)
            .options(
                selectinload(AssessmentAttempt.answers), selectinload(AssessmentAttempt.student)
            )
            .order_by(AssessmentAttempt.started_at.desc())
        )
        return await self.paginate(stmt, params)


class StudentAnswerRepository(BaseRepository[StudentAnswer]):
    """Persistence for :class:`StudentAnswer` upserts."""

    model = StudentAnswer

    async def list_for_attempt(self, attempt_id: str) -> list[StudentAnswer]:
        """Read all answers when recomputing an overridden total."""
        return list(
            (
                await self.session.execute(
                    select(StudentAnswer).where(StudentAnswer.attempt_id == attempt_id)
                )
            )
            .scalars()
            .all()
        )

    async def get_for_question(self, attempt_id: str, question_id: str) -> StudentAnswer | None:
        """Fetch a student's saved answer for one question.

        Args:
            attempt_id: Owning attempt.
            question_id: Question.

        Returns:
            The answer, or ``None``.
        """
        stmt = select(StudentAnswer).where(
            StudentAnswer.attempt_id == attempt_id,
            StudentAnswer.question_id == question_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def upsert(
        self,
        *,
        attempt_id: str,
        question_id: str,
        answer_data: object,
    ) -> StudentAnswer:
        """Create or replace the answer for a question in an attempt.

        Args:
            attempt_id: Owning attempt.
            question_id: Question.
            answer_data: Raw answer payload (option id(s)/bool/text).

        Returns:
            The persisted answer.
        """
        existing = await self.get_for_question(attempt_id, question_id)
        if existing is not None:
            existing.answer_data = answer_data
            existing.is_correct = None
            existing.points_awarded = None
            await self.session.flush()
            return existing
        return await self.add(
            StudentAnswer(
                attempt_id=attempt_id,
                question_id=question_id,
                answer_data=answer_data,
            )
        )

    async def count_for_attempt(self, attempt_id: str) -> int:
        """Count answers saved in an attempt (progress display).

        Args:
            attempt_id: Attempt id.

        Returns:
            Number of saved answers.
        """
        stmt = (
            select(func.count())
            .select_from(StudentAnswer)
            .where(StudentAnswer.attempt_id == attempt_id)
        )
        return int((await self.session.execute(stmt)).scalar_one())
