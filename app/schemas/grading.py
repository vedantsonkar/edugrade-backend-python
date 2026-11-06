"""Grading wire schemas: direct AI grading and batch task reporting."""

from datetime import datetime

from pydantic import Field

from app.schemas.common import BaseRequest, BaseSchema, IdStr


class DirectGradingRequest(BaseRequest):
    """Trigger AI grading for one submission, synchronously.

    Attributes:
        assignment_id: Owning assignment.
        submission_id: Submission to grade.
    """

    assignment_id: IdStr
    submission_id: IdStr


class GradingResultResponse(BaseSchema):
    """AI grading outcome applied to a submission.

    Attributes:
        score: Numeric grade (NUMBER mode).
        summary: One-paragraph assessment summary.
        feedback: Constructive feedback incl. spelling/grammar notes.
        plagiarism_score: Plagiarism percentage (0-100), when assessed.
        plagiarism_analysis: Plagiarism explanation, when assessed.
    """

    score: float
    summary: str
    feedback: str
    plagiarism_score: float | None = None
    plagiarism_analysis: str | None = None


class BatchGradingTaskResponse(BaseSchema):
    """Batch grading task state for progress polling.

    Attributes:
        id: Task id.
        assignment_id: Assignment being graded.
        status: State-machine position.
        trigger: ``DUE_DATE`` or ``MANUAL``.
        mode: ``UNGRADED_ONLY`` or ``ALL``.
        total_submissions: Submissions in scope.
        processed_submissions: Rows resolved so far.
        graded_submissions: Rows graded successfully.
        failed_submissions: Rows that failed.
        error_message: Terminal failure explanation.
        started_at / completed_at: Run boundaries.
        created_at / updated_at: Timestamps.
    """

    id: IdStr
    assignment_id: IdStr
    status: str
    trigger: str
    mode: str
    total_submissions: int
    processed_submissions: int
    graded_submissions: int
    failed_submissions: int
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class GradeAllRequest(BaseRequest):
    """Queue batch grading for an assignment.

    Attributes:
        mode: ``UNGRADED_ONLY`` (default) or ``ALL``.
    """

    mode: str = Field(default="UNGRADED_ONLY")
