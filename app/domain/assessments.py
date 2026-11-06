"""Online-assessment attempt entities.

:class:`AssessmentAttempt` and :class:`StudentAnswer`: an attempt is a
student's single run of an online assignment; answers are
upserted per question during the run. Proctoring events (tab switches,
snapshots) accumulate in ``proctoring_log``. Behavioral invariants (e.g. auto
termination after too many tab switches) live on the entity (AGENTS.md §3).
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.db.base import UUID_KEY, Base, TimestampMixin, UUID7PKMixin, utc_now
from app.domain.enums import AttemptStatus

if TYPE_CHECKING:
    from app.domain.assignments import Assignment, Question
    from app.domain.users import User


class AssessmentAttempt(UUID7PKMixin, TimestampMixin, Base):
    """A student's run of an online assignment.

    Attributes:
        assignment_id: Assessed assignment.
        student_id: Attempting student.
        started_at / submitted_at: Run boundaries.
        score / max_score: Result aggregates (set at submission).
        tab_switch_count: Proctoring counter.
        grade_override / is_overridden: Teacher's manual score override.
        status: Attempt lifecycle state.
        proctoring_log: Structured log (snapshots, tab-switch events).
    """

    __tablename__ = "assessment_attempts"

    assignment_id: Mapped[str] = mapped_column(
        UUID_KEY,
        ForeignKey("assignments.id", ondelete="CASCADE"), index=True
    )
    student_id: Mapped[str] = mapped_column(
        UUID_KEY,
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    score: Mapped[float | None] = mapped_column(Float)
    max_score: Mapped[float | None] = mapped_column(Float)
    tab_switch_count: Mapped[int] = mapped_column(Integer, default=0)
    grade_override: Mapped[float | None] = mapped_column(Float)
    is_overridden: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[AttemptStatus] = mapped_column(
        default=AttemptStatus.IN_PROGRESS, index=True
    )
    proctoring_log: Mapped[dict[str, object] | None] = mapped_column(JSON)

    assignment: Mapped["Assignment"] = relationship(
        back_populates="attempts", lazy="selectin"
    )
    student: Mapped["User"] = relationship(back_populates="attempts")
    answers: Mapped[list["StudentAnswer"]] = relationship(
        back_populates="attempt",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def is_active(self) -> bool:
        """Whether the attempt is still runnable."""
        return self.status == AttemptStatus.IN_PROGRESS

    @property
    def effective_score(self) -> float | None:
        """The teacher override if present, else the computed score."""
        if self.is_overridden and self.grade_override is not None:
            return self.grade_override
        return self.score

    def register_tab_switch(self, *, max_switches: int, enable_detection: bool) -> bool:
        """Record a tab-switch event; returns whether to terminate the run.

        If tab detection is disabled on the assignment the counter is left
        untouched and the attempt continues (matches legacy behavior).

        Args:
            max_switches: Assignment-configured allowance.
            enable_detection: Whether proctoring counts tab switches.

        Returns:
            ``True`` when the attempt must be terminated.
        """
        if not enable_detection:
            return False
        self.tab_switch_count = (self.tab_switch_count or 0) + 1
        return self.tab_switch_count > max_switches

    def terminate(self, *, at: datetime, timed_out: bool = False) -> None:
        """Close the attempt without student submission.

        Args:
            at: Termination instant.
            timed_out: ``True`` for deadline expiry, ``False`` for proctoring.
        """
        self.status = AttemptStatus.TIMED_OUT if timed_out else AttemptStatus.TERMINATED
        self.submitted_at = at


class StudentAnswer(UUID7PKMixin, TimestampMixin, Base):
    """A student's saved answer to one question within an attempt.

    Attributes:
        attempt_id: Owning attempt.
        question_id: Answered question.
        answer_data: JSON payload - option id (str), option ids (list),
            boolean, or free-text string depending on question type.
        is_correct: Objective grading result (``None`` until graded).
        points_awarded: Points granted by grading (``None`` until graded).
    """

    __tablename__ = "student_answers"

    attempt_id: Mapped[str] = mapped_column(
        UUID_KEY,
        ForeignKey("assessment_attempts.id", ondelete="CASCADE"), index=True
    )
    question_id: Mapped[str] = mapped_column(
        UUID_KEY,
        ForeignKey("questions.id", ondelete="CASCADE"), index=True
    )
    answer_data: Mapped[object] = mapped_column(JSON)
    is_correct: Mapped[bool | None] = mapped_column(Boolean)
    points_awarded: Mapped[float | None] = mapped_column(Float)

    attempt: Mapped["AssessmentAttempt"] = relationship(back_populates="answers")
    question: Mapped["Question"] = relationship(lazy="selectin")
