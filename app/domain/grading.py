"""Offline grading entities.

:class:`Submission`, :class:`Attachment`, and the :class:`BatchGradingTask`
state machine. A submission is a student's hand-in for an offline assignment; attachments
point at storage keys. Batch tasks coordinate asynchronous Gemini Batch API
runs (enqueue → upload → poll → finalize); :class:`GradingAttempt` records
each execution (initial run and retries).
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.db.base import UUID_KEY, Base, TimestampMixin, UUID7PKMixin, utc_now
from app.domain.enums import (
    BatchGradingMode,
    BatchGradingTaskStatus,
    BatchGradingTrigger,
    GradingAttemptStatus,
)

if TYPE_CHECKING:
    from app.domain.assignments import Assignment
    from app.domain.schools import School
    from app.domain.users import User


class Submission(UUID7PKMixin, TimestampMixin, Base):
    """A student's hand-in for an offline assignment.

    Attributes:
        assignment_id: Graded assignment.
        student_id: Authoring student.
        submitted_at: Hand-in instant.
        grade: AI-assigned numeric grade (grading type NUMBER); letter grades
            land in ``ai_grade_letter``.
        grade_override / teacher_notes / is_overridden: Manual teacher result.
        ai_summary / ai_feedback: Gemini's summary and constructive feedback.
        plagiarism_score / plagiarism_analysis: Plagiarism assessment.
        ai_grade_letter: Letter grade when grading type is ALPHABET.
    """

    __tablename__ = "submissions"

    assignment_id: Mapped[str] = mapped_column(
        UUID_KEY, ForeignKey("assignments.id", ondelete="CASCADE"), index=True
    )
    student_id: Mapped[str] = mapped_column(
        UUID_KEY, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    grade: Mapped[float | None] = mapped_column(Float)
    grade_override: Mapped[str | None] = mapped_column(String(32))
    is_overridden: Mapped[bool] = mapped_column(default=False)
    ai_summary: Mapped[str | None] = mapped_column(Text)
    ai_feedback: Mapped[str | None] = mapped_column(Text)
    teacher_notes: Mapped[str | None] = mapped_column(Text)
    plagiarism_score: Mapped[float | None] = mapped_column(Float)
    plagiarism_analysis: Mapped[str | None] = mapped_column(Text)
    ai_grade_letter: Mapped[str | None] = mapped_column(String(8))

    assignment: Mapped[Assignment] = relationship(back_populates="submissions", lazy="selectin")
    student: Mapped[User] = relationship(back_populates="submissions", lazy="selectin")
    attachments: Mapped[list[Attachment]] = relationship(
        back_populates="submission",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def is_graded(self) -> bool:
        """Whether any grading result exists (AI or manual)."""
        return self.grade is not None or self.ai_grade_letter is not None or self.is_overridden


class Attachment(UUID7PKMixin, Base):
    """A file attached to a submission; ``url`` holds the storage key.

    Attributes:
        submission_id: Owning submission.
        name: Client-visible filename.
        type: MIME type.
        url: Storage key (pre-signed GET is generated on demand).
        size: File size in bytes, when known.
    """

    __tablename__ = "attachments"

    submission_id: Mapped[str] = mapped_column(
        UUID_KEY, ForeignKey("submissions.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    type: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(String(512))
    size: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    submission: Mapped[Submission] = relationship(back_populates="attachments")


class BatchGradingTask(UUID7PKMixin, TimestampMixin, Base):
    """Asynchronous batch grading job following a strict state machine.

    State flow: ``QUEUED → RUNNING → COMPLETED | PARTIAL | FAILED``
    (``CANCELLED`` terminal). Retryable failures requeue the task as
    ``QUEUED`` with ``next_retry_at`` set; each execution is recorded as a
    :class:`GradingAttempt`. Progress counters are updated as results are
    applied; ``request_mappings`` maps submission ids to Gemini request keys.

    Attributes:
        assignment_id: Assignment being graded.
        school_id: Owning school (denormalized for scoping).
        trigger: ``DUE_DATE`` automation or ``MANUAL`` request.
        mode: Which submissions the task grades.
        status: Current state machine position.
        created_by_id: Requesting user for manual tasks.
        total / processed / graded / failed_submissions: Progress counters
            of the latest run.
        retry_count: Retries consumed since the last (re)queue reset.
        next_retry_at: Earliest instant the worker may restart a QUEUED
            task (``None`` = eligible immediately).
        request_mappings: Gemini request-key → submission-id mapping.
        failure_details: Per-submission failure records.
        gemini_job_name / gemini_input_file_name / gemini_output_file_name:
            Gemini Batch API bookkeeping.
        error_message: Terminal failure explanation.
        started_at / completed_at: Run boundaries.
    """

    __tablename__ = "batch_grading_tasks"

    assignment_id: Mapped[str] = mapped_column(
        UUID_KEY, ForeignKey("assignments.id", ondelete="CASCADE"), index=True
    )
    school_id: Mapped[str] = mapped_column(
        UUID_KEY, ForeignKey("schools.id", ondelete="CASCADE"), index=True
    )
    trigger: Mapped[BatchGradingTrigger] = mapped_column(index=True)
    mode: Mapped[BatchGradingMode] = mapped_column(default=BatchGradingMode.UNGRADED_ONLY)
    status: Mapped[BatchGradingTaskStatus] = mapped_column(
        default=BatchGradingTaskStatus.QUEUED, index=True
    )
    created_by_id: Mapped[str | None] = mapped_column(
        UUID_KEY, ForeignKey("users.id", ondelete="SET NULL")
    )
    total_submissions: Mapped[int] = mapped_column(Integer, default=0)
    processed_submissions: Mapped[int] = mapped_column(Integer, default=0)
    graded_submissions: Mapped[int] = mapped_column(Integer, default=0)
    failed_submissions: Mapped[int] = mapped_column(Integer, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_mappings: Mapped[dict[str, object] | None] = mapped_column(JSON)
    failure_details: Mapped[list[object] | None] = mapped_column(JSON)
    gemini_job_name: Mapped[str | None] = mapped_column(String(512))
    gemini_input_file_name: Mapped[str | None] = mapped_column(String(512))
    gemini_output_file_name: Mapped[str | None] = mapped_column(String(512))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    assignment: Mapped[Assignment] = relationship(back_populates="batch_tasks", lazy="selectin")
    school: Mapped[School] = relationship(back_populates="batch_tasks")
    created_by: Mapped[User | None] = relationship(back_populates="batch_tasks_created")
    attempts: Mapped[list[GradingAttempt]] = relationship(
        back_populates="task",
        order_by="GradingAttempt.attempt_number",
        lazy="selectin",
    )


class GradingAttempt(UUID7PKMixin, TimestampMixin, Base):
    """One execution of a batch grading task (initial run or retry).

    A ``PENDING`` row is created when a task is enqueued or a retry is
    scheduled; the worker flips it to ``SUCCESS`` (job finished, every
    submission graded) or ``FAILED`` (any other outcome) as the run
    resolves. Attempt rows are the run-history record; live provider
    bookkeeping stays on the owning :class:`BatchGradingTask`.

    Attributes:
        task_id: Owning batch grading task.
        attempt_number: Monotonic 1-based execution index for the task.
        status: ``PENDING`` / ``SUCCESS`` / ``FAILED``.
        total / graded / failed_submissions: Outcome counters of this run.
        error_message: Failure explanation when ``FAILED``.
        started_at / completed_at: Run boundaries.
    """

    __tablename__ = "grading_attempts"

    task_id: Mapped[str] = mapped_column(
        UUID_KEY, ForeignKey("batch_grading_tasks.id", ondelete="CASCADE"), index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[GradingAttemptStatus] = mapped_column(
        default=GradingAttemptStatus.PENDING, index=True
    )
    total_submissions: Mapped[int] = mapped_column(Integer, default=0)
    graded_submissions: Mapped[int] = mapped_column(Integer, default=0)
    failed_submissions: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    task: Mapped[BatchGradingTask] = relationship(back_populates="attempts")
