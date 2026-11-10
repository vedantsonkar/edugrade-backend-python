"""Repositories for submissions, attachments, batch grading tasks, and attempts."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from app.core.pagination import PageParams
from app.domain.enums import BatchGradingTaskStatus, GradingAttemptStatus
from app.domain.grading import Attachment, BatchGradingTask, GradingAttempt, Submission
from app.repositories.base import BaseRepository


class SubmissionRepository(BaseRepository[Submission]):
    """Persistence and listing queries for :class:`Submission`."""

    model = Submission

    async def get_scoped(self, submission_id: UUID, school_id: str) -> Submission | None:
        """Fetch a submission enforced to a school (join via assignment).

        Args:
            submission_id: Submission id.
            school_id: Tenant.

        Returns:
            The submission, or ``None``.
        """
        stmt = (
            select(Submission)
            .join(Submission.assignment)
            .where(
                Submission.id == submission_id,
                Submission.assignment.has(school_id=school_id),
            )
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_loaded(self, submission_id: object) -> Submission | None:
        """Fetch one submission with attachments and student eager-loaded.

        Args:
            submission_id: Submission id.

        Returns:
            The submission, or ``None``.
        """
        stmt = (
            select(Submission)
            .where(Submission.id == submission_id)
            .options(
                selectinload(Submission.attachments),
                selectinload(Submission.student),
            )
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_for_student(self, assignment_id: str, student_id: str) -> Submission | None:
        """Fetch a student's submission for an assignment.

        Args:
            assignment_id: Assignment id.
            student_id: Student id.

        Returns:
            The submission, or ``None``.
        """
        stmt = (
            select(Submission)
            .options(selectinload(Submission.student), selectinload(Submission.attachments))
            .where(
                Submission.assignment_id == assignment_id,
                Submission.student_id == student_id,
            )
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_assignment(
        self,
        assignment_id: str,
        params: PageParams,
        *,
        only_ungraded: bool = False,
    ) -> tuple[list[Submission], int]:
        """List an assignment's submissions for staff review.

        Args:
            assignment_id: Assignment id.
            params: Pagination window.
            only_ungraded: Restrict to submissions without any grading result.

        Returns:
            Tuple of (submissions, total).
        """
        stmt = select(Submission).where(Submission.assignment_id == assignment_id)
        if only_ungraded:
            stmt = stmt.where(
                or_(
                    Submission.grade.is_(None),
                    Submission.ai_grade_letter.is_(None) & Submission.grade.is_(None),
                )
            )
        stmt = stmt.order_by(Submission.submitted_at.asc())
        return await self.paginate(stmt, params)

    async def count_for_assignment(self, assignment_id: str, *, only_ungraded: bool = False) -> int:
        """Count submissions of an assignment.

        Args:
            assignment_id: Assignment id.
            only_ungraded: Restrict count to ungraded rows.

        Returns:
            Submission count.
        """
        stmt = (
            select(func.count())
            .select_from(Submission)
            .where(Submission.assignment_id == assignment_id)
        )
        if only_ungraded:
            stmt = stmt.where(
                or_(
                    Submission.grade.is_(None),
                    Submission.ai_grade_letter.is_(None) & Submission.grade.is_(None),
                )
            )
        return int((await self.session.execute(stmt)).scalar_one())


class AttachmentRepository(BaseRepository[Attachment]):
    """Persistence for :class:`Attachment` rows."""

    model = Attachment


class BatchGradingTaskRepository(BaseRepository[BatchGradingTask]):
    """Persistence for the :class:`BatchGradingTask` state machine."""

    model = BatchGradingTask

    async def lock_assignment(self, assignment_id: UUID) -> None:
        """Serialize task creation on the owning assignment row."""
        from app.domain.assignments import Assignment

        await self.session.execute(
            select(Assignment.id).where(Assignment.id == assignment_id).with_for_update()
        )

    async def active_for_assignment(self, assignment_id: str) -> BatchGradingTask | None:
        """Return an active task for deduplication under the assignment lock."""
        stmt = (
            select(BatchGradingTask)
            .where(
                BatchGradingTask.assignment_id == assignment_id,
                BatchGradingTask.status.in_(
                    [BatchGradingTaskStatus.QUEUED, BatchGradingTaskStatus.RUNNING]
                ),
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_by_status(
        self, status: BatchGradingTaskStatus, limit: int = 10
    ) -> list[BatchGradingTask]:
        """List tasks in a given state (worker intake).

        Args:
            status: State to select.
            limit: Safety cap per pass.

        Returns:
            Tasks oldest-first.
        """
        stmt = (
            select(BatchGradingTask)
            .where(BatchGradingTask.status == status)
            .order_by(BatchGradingTask.created_at)
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return list(rows)

    async def list_due_queued(
        self, now: datetime, limit: int = 10
    ) -> list[BatchGradingTask]:
        """List QUEUED tasks whose retry gate has opened (worker intake).

        Args:
            now: Current instant; tasks with ``next_retry_at`` in the future
                are withheld until their backoff expires.
            limit: Safety cap per pass.

        Returns:
            Due tasks oldest-first.
        """
        stmt = (
            select(BatchGradingTask)
            .where(
                BatchGradingTask.status == BatchGradingTaskStatus.QUEUED,
                or_(
                    BatchGradingTask.next_retry_at.is_(None),
                    BatchGradingTask.next_retry_at <= now,
                ),
            )
            .order_by(BatchGradingTask.created_at)
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return list(rows)

    async def latest_for_assignment(self, assignment_id: str) -> BatchGradingTask | None:
        """Fetch the most recent task of an assignment.

        Args:
            assignment_id: Assignment id.

        Returns:
            The latest task (attempts eager-loaded), or ``None``.
        """
        stmt = (
            select(BatchGradingTask)
            .where(BatchGradingTask.assignment_id == assignment_id)
            .options(selectinload(BatchGradingTask.attempts))
            .order_by(BatchGradingTask.created_at.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()


class GradingAttemptRepository(BaseRepository[GradingAttempt]):
    """Persistence for :class:`GradingAttempt` run-history rows."""

    model = GradingAttempt

    async def pending_for_task(self, task_id: str) -> GradingAttempt | None:
        """Fetch the open (PENDING) attempt of a task.

        Args:
            task_id: Owning task id.

        Returns:
            The newest pending attempt, or ``None``.
        """
        stmt = (
            select(GradingAttempt)
            .where(
                GradingAttempt.task_id == task_id,
                GradingAttempt.status == GradingAttemptStatus.PENDING,
            )
            .order_by(GradingAttempt.attempt_number.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def latest_for_task(self, task_id: str) -> GradingAttempt | None:
        """Fetch the newest attempt of a task regardless of status.

        Args:
            task_id: Owning task id.

        Returns:
            The latest attempt, or ``None``.
        """
        stmt = (
            select(GradingAttempt)
            .where(GradingAttempt.task_id == task_id)
            .order_by(GradingAttempt.attempt_number.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_task(self, task_id: str) -> list[GradingAttempt]:
        """List a task's attempts in execution order.

        Args:
            task_id: Owning task id.

        Returns:
            Attempts oldest-first.
        """
        stmt = (
            select(GradingAttempt)
            .where(GradingAttempt.task_id == task_id)
            .order_by(GradingAttempt.attempt_number.asc())
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return list(rows)


def utc_now() -> datetime:
    """UTC timestamp helper for repository-level defaults.

    Returns:
        Current UTC instant.
    """
    from app.core.db.base import utc_now as _utc_now

    return _utc_now()
