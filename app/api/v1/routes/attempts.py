"""Attempt routes: teacher grade override and student review."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from app.api.v1.deps import get_assessment_service
from app.core.errors import ForbiddenError
from app.core.security.guards import CurrentUser
from app.schemas.assessments import (
    AttemptGradeRequest,
    AttemptResponse,
    ReviewResponse,
    ReviewSnapshot,
    StudentAnswerResponse,
)
from app.schemas.assignments import QuestionResponse
from app.services.assessments import AssessmentService

router = APIRouter(prefix="/attempts", tags=["attempts"])


@router.patch(
    "/{attempt_id}/grade",
    response_model=AttemptResponse,
    summary="Override an attempt's score (teacher/admin)",
)
async def override_grade(
    attempt_id: UUID,
    payload: AttemptGradeRequest,
    current_user: CurrentUser,
    service: Annotated[AssessmentService, Depends(get_assessment_service)],
) -> AttemptResponse:
    """Manually override an attempt's automatic score.

    Args:
        attempt_id: Attempt id.
        payload: Override score.
        current_user: Authenticated principal (staff role enforced here).
        service: Assessment service.

    Returns:
        The updated attempt.

    Raises:
        ForbiddenError: When the caller is a student.
        NotFoundError: When the attempt is not in the caller's school.
    """
    if current_user.role not in ("TEACHER", "ADMIN"):
        raise ForbiddenError("Teacher or administrator role required.")
    attempt = await service.grade_attempt(
        current_user, attempt_id, payload.grade_override, payload.question_overrides
    )
    return AttemptResponse.model_validate(attempt)


@router.get(
    "/{attempt_id}/review",
    response_model=ReviewResponse,
    summary="Review a finished attempt",
)
async def review(
    attempt_id: UUID,
    current_user: CurrentUser,
    service: Annotated[AssessmentService, Depends(get_assessment_service)],
) -> ReviewResponse:
    """Return the attempt with correct answers and explanations revealed.

    Args:
        attempt_id: Attempt id.
        current_user: Authenticated principal.
        service: Assessment service.

    Returns:
        Review payload (own attempt only).

    Raises:
        ConflictError: When the attempt is still in progress.
        ForbiddenError: When another student's attempt is requested.
    """
    attempt, questions = await service.review(current_user, attempt_id)
    return ReviewResponse(
        snapshots=[
            ReviewSnapshot(url=url, timestamp=timestamp)
            for url, timestamp in await service.review_snapshots(attempt)
        ],
        attempt=AttemptResponse.model_validate(attempt),
        questions=[QuestionResponse.model_validate(q) for q in questions],
        answers=[StudentAnswerResponse.model_validate(a) for a in attempt.answers],
    )
