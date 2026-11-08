"""Assessment wire schemas: attempts, answers, proctoring, review."""

from datetime import datetime

from pydantic import Field

from app.schemas.admin import UserSummary
from app.schemas.assignments import QuestionResponse
from app.schemas.common import BaseRequest, BaseSchema, IdStr


class AttemptSettings(BaseSchema):
    """Effective proctoring settings for an attempt.

    Attributes:
        require_camera: Webcam capture required.
        require_screen_share: Screen share required.
        enable_tab_detection: Tab-switch counting active.
        max_tab_switches: Allowed switches before termination.
    """

    require_camera: bool
    require_screen_share: bool
    enable_tab_detection: bool
    max_tab_switches: int


class StudentAnswerResponse(BaseSchema):
    """Saved answer row.

    Attributes:
        id: Answer id.
        attempt_id: Owning attempt.
        question_id: Answered question.
        answer_data: Option id(s), boolean, or text.
        is_correct: Objective grading result.
        points_awarded: Points granted.
    """

    id: IdStr
    attempt_id: IdStr
    question_id: IdStr
    answer_data: object
    is_correct: bool | None = None
    points_awarded: float | None = None


class AttemptResponse(BaseSchema):
    """Assessment attempt row.

    Attributes:
        id: Attempt id.
        assignment_id: Assessed assignment.
        student_id: Attempting student.
        started_at / submitted_at: Boundaries.
        score / max_score: Result aggregates.
        tab_switch_count: Proctoring counter.
        grade_override / is_overridden: Manual override.
        status: Lifecycle state.
        proctoring_log: Structured proctoring events.
        answers: Saved answers when included.
    """

    id: IdStr
    assignment_id: IdStr
    student_id: IdStr
    started_at: datetime
    submitted_at: datetime | None = None
    score: float | None = None
    max_score: float | None = None
    tab_switch_count: int
    grade_override: float | None = None
    is_overridden: bool = False
    status: str
    student: UserSummary | None = None
    proctoring_log: dict[str, object] | None = None
    answers: list[StudentAnswerResponse] = []


class AttemptStartResponse(BaseSchema):
    """Start/resume result handed to the exam client.

    Attributes:
        attempt: Attempt state.
        questions: Answer-safe questions (correct flags and explanations
            stripped for exam security).
        remaining_seconds: Time left before auto-timeout.
        settings: Effective proctoring settings.
    """

    attempt: AttemptResponse
    questions: list[QuestionResponse]
    remaining_seconds: int
    settings: AttemptSettings


class AttemptStatusResponse(BaseSchema):
    """Lightweight status for the take-assessment launcher and resume bar.

    Attributes:
        status: ``NOT_STARTED`` or the attempt's lifecycle state.
        started_at / submitted_at: Boundaries when an attempt exists.
        tab_switch_count: Proctoring counter when an attempt exists.
        score / max_score: Results once graded.
        remaining_seconds: Time left, when an active attempt exists.
        max_score_total: Total points across questions (launcher display).
    """

    status: str
    started_at: datetime | None = None
    submitted_at: datetime | None = None
    tab_switch_count: int | None = None
    score: float | None = None
    max_score: float | None = None
    remaining_seconds: int | None = None
    max_score_total: int | None = None


class SaveAnswerRequest(BaseRequest):
    """Per-question answer save.

    Attributes:
        question_id: Question being answered.
        answer_data: Option id (str) for single-choice, list of ids for
            multiple-choice, boolean for true/false, or text for free-text.
    """

    question_id: IdStr
    answer_data: object


class TabSwitchRequest(BaseRequest):
    """Tab-switch proctoring event (body may be empty ``{}``).

    Attributes:
        tab_switch_count: Optional client-side count (informational; the
            server owns the authoritative counter).
    """

    tab_switch_count: int | None = Field(default=None, ge=0)


class TabSwitchResponse(BaseSchema):
    """Result of logging a tab switch.

    Attributes:
        terminated: Whether the attempt was terminated by this event.
        tab_switch_count: Authoritative count after the event.
        max_tab_switches: Configured allowance.
        remaining: Switches left before termination.
        message: Human explanation when terminated.
    """

    terminated: bool
    tab_switch_count: int
    max_tab_switches: int
    remaining: int
    message: str | None = None


class SnapshotResponse(BaseSchema):
    """Upload grant for a proctoring snapshot.

    Attributes:
        upload_url: Pre-signed PUT URL.
        file_key: Storage key to log after upload.
    """

    upload_url: str
    file_key: str


class SubmitResponse(BaseSchema):
    """Submission result with computed aggregates.

    Attributes:
        attempt: Final attempt state.
        score: Achieved points.
        max_score: Total possible points.
        percentage: Rounded percentage (0-100); ``None`` when ungraded.
    """

    attempt: AttemptResponse
    score: float | None
    max_score: float
    percentage: float | None = None


class AttemptGradeRequest(BaseRequest):
    """Teacher override of an attempt's score.

    Attributes:
        grade_override: New score in points.
    """

    grade_override: float | None = Field(default=None, ge=0)
    question_overrides: dict[str, float] = Field(default_factory=dict)


class ReviewResponse(BaseSchema):
    """Post-submission review: questions with answers revealed.

    Attributes:
        attempt: Final attempt state.
        questions: Questions including correct flags and explanations.
        answers: The student's answers with grading results.
    """

    attempt: AttemptResponse
    questions: list[QuestionResponse]
    answers: list[StudentAnswerResponse]
    snapshots: list[ReviewSnapshot] = []


class ReviewSnapshot(BaseSchema):
    """A temporary download grant for a review snapshot."""

    url: str
    timestamp: str
