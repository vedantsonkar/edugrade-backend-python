"""Domain enumerations shared across entities and services.

All enums are ``StrEnum`` so member names equal their stored values; they are
persisted via non-native enum columns (VARCHAR + CHECK) for portability and
safe Alembic evolution.
"""

from enum import StrEnum


class UserRole(StrEnum):
    """Roles a user can hold within a school."""

    ADMIN = "ADMIN"
    TEACHER = "TEACHER"
    STUDENT = "STUDENT"


class AuthMethod(StrEnum):
    """How a user is allowed to authenticate."""

    CREDENTIALS = "CREDENTIALS"
    SSO_GOOGLE = "SSO_GOOGLE"
    BOTH = "BOTH"


class AssignmentType(StrEnum):
    """Whether an assignment is a file-upload task or an online assessment."""

    OFFLINE = "OFFLINE"
    ONLINE = "ONLINE"


class AssignmentState(StrEnum):
    """Computed lifecycle state of an assignment (never stored).

    Derived from dates and, for students, from the caller's submission or
    attempt state. Used as a list-filter parameter.
    """

    UPCOMING = "upcoming"
    ONGOING = "ongoing"
    OVERDUE = "overdue"
    SUBMITTED = "submitted"
    GRADED = "graded"


class StrictnessLevel(StrEnum):
    """AI grading strictness preset passed to the model prompt."""

    LENIENT = "LENIENT"
    MODERATE = "MODERATE"
    STRICT = "STRICT"


class GradingType(StrEnum):
    """Marks scale used by AI grading for offline assignments."""

    NUMBER = "NUMBER"
    ALPHABET = "ALPHABET"


class QuestionType(StrEnum):
    """Supported online-assessment question formats."""

    MCQ_SINGLE = "MCQ_SINGLE"
    MCQ_MULTIPLE = "MCQ_MULTIPLE"
    TRUE_FALSE = "TRUE_FALSE"
    FREE_TEXT = "FREE_TEXT"


class AttemptStatus(StrEnum):
    """Lifecycle status of a student's assessment attempt."""

    IN_PROGRESS = "IN_PROGRESS"
    SUBMITTED = "SUBMITTED"
    TIMED_OUT = "TIMED_OUT"
    TERMINATED = "TERMINATED"


class SubmissionState(StrEnum):
    """Computed grading state of a submission (never stored)."""

    PENDING = "pending"
    GRADED = "graded"
    OVERRIDDEN = "overridden"


class BatchGradingTrigger(StrEnum):
    """What caused a batch grading task to be created."""

    DUE_DATE = "DUE_DATE"
    MANUAL = "MANUAL"


class BatchGradingMode(StrEnum):
    """Which submissions a batch task grades."""

    UNGRADED_ONLY = "UNGRADED_ONLY"
    ALL = "ALL"


class BatchGradingTaskStatus(StrEnum):
    """State machine for background batch grading tasks."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
