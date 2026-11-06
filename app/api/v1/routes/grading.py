"""Grading routes: direct synchronous AI grading of one submission."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.v1.deps import get_direct_grading_service
from app.core.errors import ForbiddenError
from app.core.ratelimit.dependency import rate_limit
from app.core.security.guards import StaffUser
from app.schemas.assignments import SubmissionResponse
from app.schemas.grading import DirectGradingRequest
from app.services.grading.direct import DirectGradingService

router = APIRouter(tags=["grading"])


@router.post(
    "/grading",
    response_model=SubmissionResponse,
    summary="Grade one submission now (AI)",
    dependencies=[Depends(rate_limit("10/minute"))],
)
async def grade_submission(
    payload: DirectGradingRequest,
    current_user: StaffUser,
    grading: Annotated[DirectGradingService, Depends(get_direct_grading_service)],
) -> SubmissionResponse:
    """Run OCR + Gemini over a submission and persist the results.

    Args:
        payload: Assignment + submission ids.
        current_user: Authenticated staff principal.
        grading: Direct grading service.

    Returns:
        The graded submission.

    Raises:
        ForbiddenError: When the caller has no school context.
        NotFoundError: When the submission does not exist.
        ExternalServiceError: When AI grading is unconfigured or fails.
    """
    if current_user.school_id is None:
        raise ForbiddenError("Caller has no school context.")
    graded = await grading.grade_submission(
        submission_id=payload.submission_id,
        school_id=current_user.school_id,
    )
    return SubmissionResponse.model_validate(graded)
