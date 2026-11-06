"""Assessment routes: attempt lifecycle and proctoring (student-facing)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from app.api.v1.deps import get_assessment_service, get_question_repo
from app.core.errors import ForbiddenError
from app.core.ratelimit.dependency import rate_limit
from app.core.security.guards import StudentUser
from app.repositories.assignments import QuestionRepository
from app.schemas.assessments import (
    AttemptResponse,
    AttemptSettings,
    AttemptStartResponse,
    AttemptStatusResponse,
    SaveAnswerRequest,
    SnapshotResponse,
    StudentAnswerResponse,
    SubmitResponse,
    TabSwitchRequest,
    TabSwitchResponse,
)
from app.schemas.assignments import QuestionResponse
from app.services.assessments import AssessmentService

router = APIRouter(prefix="/assessments", tags=["assessments"])


def _school(principal: object) -> str:
    """Extract the caller's school id, failing closed.

    Args:
        principal: Authenticated principal.

    Returns:
        School id string.

    Raises:
        ForbiddenError: When no school context exists.
    """
    school_id = getattr(principal, "school_id", None)
    if not school_id:
        raise ForbiddenError("Caller has no school context.")
    return str(school_id)


@router.post(
    "/{assignment_id}/start",
    response_model=AttemptStartResponse,
    summary="Start or resume the attempt (student)",
    dependencies=[Depends(rate_limit("10/minute"))],
)
async def start(
    assignment_id: UUID,
    current_user: StudentUser,
    service: Annotated[AssessmentService, Depends(get_assessment_service)],
) -> AttemptStartResponse:
    """Start/resume the caller's attempt with answer-safe questions.

    Args:
        assignment_id: Online assignment id.
        current_user: Authenticated student.
        service: Assessment service.

    Returns:
        Attempt, sanitized questions, remaining time, proctoring settings.

    Raises:
        ConflictError: When the attempt cannot be (re)started.
    """
    result = await service.start(current_user, assignment_id)
    return AttemptStartResponse(
        attempt=AttemptResponse.model_validate(result.attempt),
        questions=[QuestionResponse.model_validate(q) for q in result.questions],
        remaining_seconds=result.remaining_seconds,
        settings=AttemptSettings(
            require_camera=result.remaining_settings[0],
            require_screen_share=result.remaining_settings[1],
            enable_tab_detection=result.remaining_settings[2],
            max_tab_switches=result.remaining_settings[3],
        ),
    )


@router.get(
    "/{assignment_id}/status",
    response_model=AttemptStatusResponse,
    summary="Attempt status (student)",
)
async def status(
    assignment_id: UUID,
    current_user: StudentUser,
    service: Annotated[AssessmentService, Depends(get_assessment_service)],
    questions: Annotated[QuestionRepository, Depends(get_question_repo)],
) -> AttemptStatusResponse:
    """Return the caller's attempt status plus the question-total points.

    Args:
        assignment_id: Assignment id.
        current_user: Authenticated student.
        service: Assessment service.
        questions: Question repository (total points for the launcher).

    Returns:
        Status payload (``NOT_STARTED`` when no attempt exists).
    """
    from app.domain.enums import AttemptStatus

    assignment = await service.get_assignment_for(current_user, assignment_id)
    attempt = await service.status(current_user, assignment_id, None)
    total_points = sum(q.points for q in await questions.list_for_assignment(str(assignment.id)))
    if attempt is None:
        return AttemptStatusResponse(status="NOT_STARTED", max_score_total=total_points)
    remaining: int | None = None
    if attempt.status is AttemptStatus.IN_PROGRESS:
        remaining = service.remaining_for(assignment, attempt)
    return AttemptStatusResponse(
        status=attempt.status.value,
        started_at=attempt.started_at,
        submitted_at=attempt.submitted_at,
        tab_switch_count=attempt.tab_switch_count,
        score=attempt.score,
        max_score=attempt.max_score,
        remaining_seconds=remaining,
        max_score_total=total_points,
    )


@router.post(
    "/{assignment_id}/answer",
    response_model=StudentAnswerResponse,
    summary="Save one answer (student)",
    dependencies=[Depends(rate_limit("60/minute"))],
)
async def save_answer(
    assignment_id: UUID,
    payload: SaveAnswerRequest,
    current_user: StudentUser,
    service: Annotated[AssessmentService, Depends(get_assessment_service)],
) -> StudentAnswerResponse:
    """Save or replace the student's answer for one question.

    Args:
        assignment_id: Assignment id.
        payload: Question id + raw answer payload.
        current_user: Authenticated student.
        service: Assessment service.

    Returns:
        The persisted answer.

    Raises:
        ConflictError: When no active attempt exists or time is up.
    """
    answer = await service.save_answer(
        current_user, assignment_id, payload.question_id, payload.answer_data
    )
    return StudentAnswerResponse.model_validate(answer)


@router.post(
    "/{assignment_id}/tab-switch",
    response_model=TabSwitchResponse,
    summary="Log a tab switch (student)",
)
async def tab_switch(
    assignment_id: UUID,
    current_user: StudentUser,
    service: Annotated[AssessmentService, Depends(get_assessment_service)],
    payload: TabSwitchRequest | None = None,
) -> TabSwitchResponse:
    """Record a tab-switch proctoring event; terminate past the allowance.

    Args:
        assignment_id: Assignment id.
        current_user: Authenticated student.
        service: Assessment service.
        payload: Optional informational client count (server is authoritative).

    Returns:
        Counter state and termination decision.

    Raises:
        ConflictError: When no active attempt exists.
    """
    _ = payload
    attempt, terminated = await service.register_tab_switch(current_user, assignment_id)
    assignment = await service.get_assignment_for(current_user, assignment_id)
    return TabSwitchResponse(
        terminated=terminated,
        tab_switch_count=attempt.tab_switch_count,
        max_tab_switches=assignment.max_tab_switches,
        remaining=max(0, assignment.max_tab_switches - attempt.tab_switch_count),
        message="Attempt terminated: too many tab switches." if terminated else None,
    )


@router.post(
    "/{assignment_id}/snapshot",
    response_model=SnapshotResponse,
    summary="Request a snapshot upload grant (student)",
)
async def snapshot(
    assignment_id: UUID,
    current_user: StudentUser,
    service: Annotated[AssessmentService, Depends(get_assessment_service)],
) -> SnapshotResponse:
    """Create a proctoring snapshot upload grant and log the key.

    Args:
        assignment_id: Assignment id.
        current_user: Authenticated student.
        service: Assessment service.

    Returns:
        Upload grant for the webcam capture.

    Raises:
        ConflictError: When no active attempt exists.
    """
    url, key = await service.snapshot_url(current_user, assignment_id)
    return SnapshotResponse(upload_url=url, file_key=key)


@router.post(
    "/{assignment_id}/submit",
    response_model=SubmitResponse,
    summary="Submit the attempt (student)",
    dependencies=[Depends(rate_limit("5/minute"))],
)
async def submit(
    assignment_id: UUID,
    current_user: StudentUser,
    service: Annotated[AssessmentService, Depends(get_assessment_service)],
) -> SubmitResponse:
    """Finalize the attempt: auto-grade MCQs, Gemini-grade free text.

    Args:
        assignment_id: Assignment id.
        current_user: Authenticated student.
        service: Assessment service.

    Returns:
        Aggregates: attempt, score, max score, percentage.

    Raises:
        ConflictError: When no active attempt exists.
    """
    result = await service.submit(current_user, assignment_id)
    return SubmitResponse(
        attempt=AttemptResponse.model_validate(result.attempt),
        score=result.score,
        max_score=result.max_score,
        percentage=result.percentage,
    )
