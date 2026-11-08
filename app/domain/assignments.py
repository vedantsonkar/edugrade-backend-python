"""Assignment entities.

:class:`Assignment`, targets, questions, and the :class:`RubricComment`
discussion thread. Assignments are either ``OFFLINE`` (file uploads, AI grading) or ``ONLINE``
(assessments built from :class:`Question` s). Targeting is many-to-many with
:class:`Classroom` through :class:`AssignmentTarget`.
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.db.base import UUID_KEY, Base, TimestampMixin, UUID7PKMixin, utc_now
from app.domain.enums import AssignmentType, GradingType, QuestionType, StrictnessLevel

if TYPE_CHECKING:
    from app.domain.assessments import AssessmentAttempt
    from app.domain.grading import BatchGradingTask, Submission
    from app.domain.schools import School
    from app.domain.taxonomy import Classroom
    from app.domain.users import User


class Assignment(UUID7PKMixin, TimestampMixin, Base):
    """A piece of work assigned to one or more classrooms.

    Attributes:
        school_id: Owning school.
        teacher_id: Creator (teacher or admin).
        type: ``OFFLINE`` or ``ONLINE``.
        title / description / subject: Human content.
        start_date / due_date: Visibility window boundaries.
        auto_grade_on_due_date: Whether AI grading kicks off at due time.
        allow_late_submissions: Whether submissions after ``due_date`` count.
        auto_graded_at: When due-date auto grading was enqueued (dedupe flag).
        strictness_level / grading_prompt / check_spelling / check_grammar /
            check_plagiarism / grading_type / grading_config: AI grading
            configuration (offline).
        time_limit / shuffle_questions / shuffle_options / show_results_after:
            Assessment behavior (online).
        require_camera / require_screen_share / enable_tab_detection /
            max_tab_switches: Proctoring configuration (online).
    """

    __tablename__ = "assignments"

    school_id: Mapped[str] = mapped_column(
        UUID_KEY, ForeignKey("schools.id", ondelete="CASCADE"), index=True
    )
    teacher_id: Mapped[str] = mapped_column(
        UUID_KEY, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[AssignmentType] = mapped_column(default=AssignmentType.OFFLINE, index=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    subject: Mapped[str] = mapped_column(String(128))
    start_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    due_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    auto_grade_on_due_date: Mapped[bool] = mapped_column(default=True)
    allow_late_submissions: Mapped[bool] = mapped_column(default=False)
    auto_graded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    strictness_level: Mapped[StrictnessLevel] = mapped_column(default=StrictnessLevel.MODERATE)
    grading_prompt: Mapped[str] = mapped_column(Text, default="")
    check_spelling: Mapped[bool] = mapped_column(default=True)
    check_grammar: Mapped[bool] = mapped_column(default=True)
    check_plagiarism: Mapped[bool] = mapped_column(default=False)
    grading_type: Mapped[GradingType] = mapped_column(default=GradingType.NUMBER)
    grading_config: Mapped[dict[str, object] | None] = mapped_column(JSON, default=None)

    time_limit: Mapped[int | None] = mapped_column(Integer)
    shuffle_questions: Mapped[bool] = mapped_column(default=False)
    shuffle_options: Mapped[bool] = mapped_column(default=False)
    show_results_after: Mapped[bool] = mapped_column(default=True)

    require_camera: Mapped[bool] = mapped_column(default=False)
    require_screen_share: Mapped[bool] = mapped_column(default=False)
    enable_tab_detection: Mapped[bool] = mapped_column(default=False)
    max_tab_switches: Mapped[int] = mapped_column(Integer, default=3)

    school: Mapped[School] = relationship()
    teacher: Mapped[User] = relationship(back_populates="assignments_created")
    targets: Mapped[list[AssignmentTarget]] = relationship(
        back_populates="assignment",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    questions: Mapped[list[Question]] = relationship(
        back_populates="assignment",
        cascade="all, delete-orphan",
        order_by="Question.display_order",
        lazy="selectin",
    )
    submissions: Mapped[list[Submission]] = relationship(
        back_populates="assignment", cascade="all, delete-orphan"
    )
    attempts: Mapped[list[AssessmentAttempt]] = relationship(
        back_populates="assignment", cascade="all, delete-orphan"
    )
    rubric_comments: Mapped[list[RubricComment]] = relationship(
        back_populates="assignment",
        cascade="all, delete-orphan",
        order_by="RubricComment.created_at",
        lazy="selectin",
    )
    batch_tasks: Mapped[list[BatchGradingTask]] = relationship(
        back_populates="assignment", cascade="all, delete-orphan"
    )

    @property
    def total_points(self) -> int:
        """Sum of question points for online assignments."""
        return sum(question.points for question in self.questions)


class AssignmentTarget(UUID7PKMixin, Base):
    """Join row: an assignment targets a classroom.

    Attributes:
        assignment_id: Targeting assignment.
        class_id: Targeted classroom.
    """

    __tablename__ = "assignment_targets"

    assignment_id: Mapped[str] = mapped_column(
        UUID_KEY, ForeignKey("assignments.id", ondelete="CASCADE"), index=True
    )
    class_id: Mapped[str] = mapped_column(
        UUID_KEY, ForeignKey("classrooms.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    assignment: Mapped[Assignment] = relationship(back_populates="targets")
    classroom: Mapped[Classroom] = relationship(
        back_populates="assignment_targets", lazy="selectin"
    )


class Question(UUID7PKMixin, TimestampMixin, Base):
    """A question within an online assignment.

    Attributes:
        assignment_id: Owning assignment.
        type: Question format (drives the grading strategy).
        text: Question body.
        points: Points awarded for a fully correct answer.
        is_required: Whether the student must answer it.
        display_order: Position within the assignment.
        explanation: Post-submission explanation shown in review.
        min_char_length / max_char_length: Bounds for free-text answers.
    """

    __tablename__ = "questions"

    assignment_id: Mapped[str] = mapped_column(
        UUID_KEY, ForeignKey("assignments.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[QuestionType] = mapped_column(default=QuestionType.MCQ_SINGLE)
    text: Mapped[str] = mapped_column(Text)
    points: Mapped[int] = mapped_column(Integer, default=1)
    is_required: Mapped[bool] = mapped_column(default=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0, index=True)
    explanation: Mapped[str | None] = mapped_column(Text)
    min_char_length: Mapped[int | None] = mapped_column(Integer)
    max_char_length: Mapped[int | None] = mapped_column(Integer)

    assignment: Mapped[Assignment] = relationship(back_populates="questions")
    options: Mapped[list[QuestionOption]] = relationship(
        back_populates="question",
        cascade="all, delete-orphan",
        order_by="QuestionOption.display_order",
        lazy="selectin",
    )


class QuestionOption(UUID7PKMixin, Base):
    """An answer option for an objective question.

    Attributes:
        question_id: Owning question.
        text: Option label.
        is_correct: Whether selecting this option is correct.
        display_order: Position within the question.
    """

    __tablename__ = "question_options"

    question_id: Mapped[str] = mapped_column(
        UUID_KEY, ForeignKey("questions.id", ondelete="CASCADE"), index=True
    )
    text: Mapped[str] = mapped_column(String(1024))
    is_correct: Mapped[bool] = mapped_column(default=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0)

    question: Mapped[Question] = relationship(back_populates="options")


class RubricComment(UUID7PKMixin, Base):
    """A message in an assignment's rubric discussion thread.

    Attributes:
        assignment_id: Thread owner.
        user_id: Comment author.
        comment: Message body.
    """

    __tablename__ = "rubric_comments"

    assignment_id: Mapped[str] = mapped_column(
        UUID_KEY, ForeignKey("assignments.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(
        UUID_KEY, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    comment: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    assignment: Mapped[Assignment] = relationship(back_populates="rubric_comments")
    author: Mapped[User] = relationship(back_populates="rubric_comments", lazy="selectin")
