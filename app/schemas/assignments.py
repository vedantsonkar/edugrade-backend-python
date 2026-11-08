"""Assignment wire schemas: CRUD, targets, questions, submissions, comments."""

from datetime import datetime

from pydantic import Field

from app.schemas.admin import ClassroomResponse, UserSummary
from app.schemas.common import BaseRequest, BaseSchema, IdStr


class AssignmentTargetResponse(BaseSchema):
    """Target row (assignment → classroom).

    Attributes:
        id: Target id.
        assignment_id: Owning assignment.
        class_id: Targeted classroom.
        created_at: Creation instant.
        classroom: Nested classroom projection when loaded.
    """

    id: IdStr
    assignment_id: IdStr
    class_id: IdStr
    created_at: datetime
    classroom: ClassroomResponse | None = None


class AssignmentResponse(BaseSchema):
    """Assignment detail with settings, targets, and creator.

    Grading prompts are staff-only fields; students legitimately need
    settings (time limit, proctoring).

    Attributes:
        id: Assignment id.
        school_id: Owning school.
        teacher_id: Creator.
        type: ``OFFLINE`` or ``ONLINE``.
        title / description / subject: Content.
        start_date / due_date: Window.
        auto_grade_on_due_date / allow_late_submissions / auto_graded_at:
            Lifecycle flags.
        strictness_level / grading_prompt / check_spelling / check_grammar /
            check_plagiarism / grading_type / grading_config: AI config.
        time_limit / shuffle_questions / shuffle_options / show_results_after:
            Assessment behavior.
        require_camera / require_screen_share / enable_tab_detection /
            max_tab_switches: Proctoring.
        targets: Classroom targets.
        teacher: Creator projection.
        created_at / updated_at: Timestamps.
    """

    id: IdStr
    school_id: IdStr
    teacher_id: IdStr
    type: str
    title: str
    description: str
    subject: str
    start_date: datetime
    due_date: datetime
    auto_grade_on_due_date: bool
    allow_late_submissions: bool
    auto_graded_at: datetime | None = None
    state: str | None = None
    submission_count: int = 0
    my_submission: SubmissionResponse | None = None
    my_attempt: AttemptSummary | None = None
    strictness_level: str
    grading_prompt: str = ""
    check_spelling: bool
    check_grammar: bool
    check_plagiarism: bool
    grading_type: str
    grading_config: dict[str, object] | None = None
    time_limit: int | None = None
    shuffle_questions: bool
    shuffle_options: bool
    show_results_after: bool
    require_camera: bool
    require_screen_share: bool
    enable_tab_detection: bool
    max_tab_switches: int
    targets: list[AssignmentTargetResponse] = []
    teacher: UserSummary | None = None
    created_at: datetime
    updated_at: datetime


class AssignmentCreate(BaseRequest):
    """Create-assignment payload.

    Attributes:
        type: ``OFFLINE`` or ``ONLINE``.
        title / description / subject: Content.
        start_date / due_date: Visibility window.
        auto_grade_on_due_date / allow_late_submissions: Lifecycle flags.
        class_ids: Target classroom ids (must belong to caller's school).
        strictness_level / grading_prompt / check_spelling / check_grammar /
            check_plagiarism / grading_type / grading_config: AI config.
        time_limit / shuffle_questions / shuffle_options / show_results_after:
            Assessment behavior.
        require_camera / require_screen_share / enable_tab_detection /
            max_tab_switches: Proctoring.
    """

    type: str = "OFFLINE"
    title: str = Field(min_length=1, max_length=255)
    description: str = ""
    subject: str = Field(min_length=1, max_length=128)
    start_date: datetime | None = None
    due_date: datetime
    auto_grade_on_due_date: bool = True
    allow_late_submissions: bool = False
    class_ids: list[str] = Field(default_factory=list)
    strictness_level: str = "MODERATE"
    grading_prompt: str = ""
    check_spelling: bool = True
    check_grammar: bool = True
    check_plagiarism: bool = False
    grading_type: str = "NUMBER"
    grading_config: dict[str, object] | None = None
    time_limit: int | None = None
    shuffle_questions: bool = False
    shuffle_options: bool = False
    show_results_after: bool = True
    require_camera: bool = False
    require_screen_share: bool = False
    enable_tab_detection: bool = False
    max_tab_switches: int = 3


class AssignmentUpdate(BaseRequest):
    """Partial assignment update; ``None`` fields untouched.

    Attributes mirror :class:`AssignmentCreate` (all optional);
    ``class_ids`` replaces the target set when provided.
    """

    title: str | None = Field(default=None, max_length=255)
    description: str | None = None
    subject: str | None = Field(default=None, max_length=128)
    start_date: datetime | None = None
    due_date: datetime | None = None
    auto_grade_on_due_date: bool | None = None
    allow_late_submissions: bool | None = None
    class_ids: list[str] | None = None
    strictness_level: str | None = None
    grading_prompt: str | None = None
    check_spelling: bool | None = None
    check_grammar: bool | None = None
    check_plagiarism: bool | None = None
    grading_type: str | None = None
    grading_config: dict[str, object] | None = None
    time_limit: int | None = None
    shuffle_questions: bool | None = None
    shuffle_options: bool | None = None
    show_results_after: bool | None = None
    require_camera: bool | None = None
    require_screen_share: bool | None = None
    enable_tab_detection: bool | None = None
    max_tab_switches: int | None = None


class AttachmentCreate(BaseRequest):
    """Attachment descriptor within a submission.

    Attributes:
        name: Client-visible filename.
        type: MIME type.
        url: Storage key returned by the presign endpoint.
        size: File size in bytes, when known.
    """

    name: str = Field(min_length=1, max_length=255)
    type: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1, max_length=512)
    size: int | None = Field(default=None, ge=0)


class AttachmentResponse(BaseSchema):
    """Attachment row.

    Attributes:
        id: Attachment id.
        submission_id: Owning submission.
        name / type / url / size: File metadata (url is the storage key).
        created_at: Creation instant.
    """

    id: IdStr
    submission_id: IdStr
    name: str
    type: str
    url: str
    size: int | None = None
    created_at: datetime


class SubmissionCreate(BaseRequest):
    """Submit hand-in payload.

    Attributes:
        attachments: One or more uploaded file keys.
    """

    attachments: list[AttachmentCreate] = Field(min_length=1)


class SubmissionResponse(BaseSchema):
    """Submission row with grading results.

    Attributes:
        id: Submission id.
        assignment_id: Graded assignment.
        student_id: Authoring student.
        submitted_at: Hand-in instant.
        grade: AI numeric grade.
        grade_override / teacher_notes / is_overridden: Manual result.
        ai_summary / ai_feedback: Gemini output.
        plagiarism_score / plagiarism_analysis: Plagiarism results.
        ai_grade_letter: Letter grade (ALPHABET mode).
        attachments: Attached files.
        student: Author projection when loaded.
        updated_at: Last update.
    """

    id: IdStr
    assignment_id: IdStr
    student_id: IdStr
    submitted_at: datetime
    grade: float | None = None
    grade_override: str | None = None
    is_overridden: bool = False
    ai_summary: str | None = None
    ai_feedback: str | None = None
    teacher_notes: str | None = None
    plagiarism_score: float | None = None
    plagiarism_analysis: str | None = None
    ai_grade_letter: str | None = None
    attachments: list[AttachmentResponse] = []
    student: UserSummary | None = None
    updated_at: datetime


class SubmissionGradeRequest(BaseRequest):
    """Manual grade override for a submission.

    Attributes:
        grade_override: Letter or numeric grade string as displayed.
        teacher_notes: Optional feedback.
    """

    grade_override: str = Field(min_length=1, max_length=32)
    teacher_notes: str | None = None


class RubricCommentCreate(BaseRequest):
    """New rubric-thread comment.

    Attributes:
        comment: Message body.
    """

    comment: str = Field(min_length=1, max_length=4096)


class RubricCommentResponse(BaseSchema):
    """Rubric-thread comment row.

    Attributes:
        id: Comment id.
        assignment_id: Thread owner.
        user_id: Author.
        comment: Message body.
        created_at: Posted instant.
        author: Author projection when loaded.
    """

    id: IdStr
    assignment_id: IdStr
    user_id: IdStr
    comment: str
    created_at: datetime
    author: UserSummary | None = None


class QuestionOptionCreate(BaseRequest):
    """Create-option payload.

    Attributes:
        text: Option label.
        is_correct: Correctness flag.
        display_order: Position.
    """

    text: str = Field(min_length=1, max_length=1024)
    is_correct: bool = False
    display_order: int = 0


class QuestionCreate(BaseRequest):
    """Create-question payload.

    Attributes:
        type: Question format.
        text: Question body.
        points: Points for a correct answer.
        is_required: Must-answer flag.
        display_order: Position.
        explanation: Review-time explanation.
        min_char_length / max_char_length: Free-text bounds.
        options: Options for objective questions.
    """

    type: str
    text: str = Field(min_length=1)
    points: int = Field(default=1, ge=1)
    is_required: bool = True
    display_order: int = 0
    explanation: str | None = None
    min_char_length: int | None = Field(default=None, ge=0)
    max_char_length: int | None = Field(default=None, ge=1)
    options: list[QuestionOptionCreate] = Field(default_factory=list)


class QuestionsCreateRequest(BaseRequest):
    """Batch question-creation wrapper (accepts one or many).

    Attributes:
        questions: Questions to create in order.
    """

    questions: list[QuestionCreate] = Field(min_length=1)


class QuestionUpdate(BaseRequest):
    """Partial question update; ``None`` fields untouched.

    When ``options`` is provided the option set is replaced wholesale.

    Attributes mirror :class:`QuestionCreate` (all optional).
    """

    type: str | None = None
    text: str | None = None
    points: int | None = Field(default=None, ge=1)
    is_required: bool | None = None
    explanation: str | None = None
    min_char_length: int | None = Field(default=None, ge=0)
    max_char_length: int | None = Field(default=None, ge=1)
    options: list[QuestionOptionCreate] | None = None


class QuestionOptionResponse(BaseSchema):
    """Option row; ``is_correct`` is stripped for students at read time.

    Attributes:
        id: Option id.
        question_id: Owning question.
        text: Label.
        is_correct: Correctness flag.
        display_order: Position.
    """

    id: IdStr
    question_id: IdStr
    text: str
    is_correct: bool
    display_order: int


class QuestionResponse(BaseSchema):
    """Question row with options.

    Attributes:
        id: Question id.
        assignment_id: Owning assignment.
        type: Format.
        text: Body.
        points: Max points.
        is_required: Must-answer flag.
        display_order: Position.
        explanation: Review-time explanation (students: post-submission only).
        min_char_length / max_char_length: Free-text bounds.
        options: Options.
        created_at / updated_at: Timestamps.
    """

    id: IdStr
    assignment_id: IdStr
    type: str
    text: str
    points: int
    is_required: bool
    display_order: int
    explanation: str | None = None
    min_char_length: int | None = None
    max_char_length: int | None = None
    options: list[QuestionOptionResponse] = []
    created_at: datetime
    updated_at: datetime


class ReorderQuestionsRequest(BaseRequest):
    """Reorder payload; list order defines the new display order.

    Attributes:
        question_ids: Question ids in the desired order.
    """

    question_ids: list[str] = Field(min_length=1)


class AttemptSummary(BaseSchema):
    """Answer-free assessment progress for a viewer or staff results list."""

    id: IdStr
    student_id: IdStr
    status: str
    score: float | None = None
    max_score: float | None = None
    grade_override: float | None = None
    is_overridden: bool = False
    submitted_at: datetime | None = None
    student: UserSummary | None = None
