"""Assignment routes: CRUD, questions, submissions, comments, batch grading."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response

from app.api.v1.deps import (
    get_assignment_service,
    get_batch_task_repo,
    get_submission_repo,
)
from app.core.errors import ForbiddenError
from app.core.pagination import Page, PageParams, page_params
from app.core.ratelimit.dependency import rate_limit
from app.core.security.guards import CurrentUser, StaffUser
from app.domain.enums import BatchGradingMode, BatchGradingTrigger
from app.domain.grading import BatchGradingTask
from app.repositories.grading import BatchGradingTaskRepository, SubmissionRepository
from app.schemas.assignments import (
    AssignmentCreate,
    AssignmentResponse,
    AssignmentUpdate,
    QuestionCreate,
    QuestionResponse,
    QuestionsCreateRequest,
    QuestionUpdate,
    ReorderQuestionsRequest,
    RubricCommentCreate,
    RubricCommentResponse,
    SubmissionCreate,
    SubmissionGradeRequest,
    SubmissionResponse,
)
from app.schemas.grading import BatchGradingTaskResponse, GradeAllRequest
from app.services.assignments import AssignmentService
from app.services.audit import AuditService as _UnusedAudit  # noqa: F401
from app.services.files import FileService as _UnusedFiles  # noqa: F401

router = APIRouter(prefix="/assignments", tags=["assignments"])


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


@router.get(
    "",
    response_model=Page[AssignmentResponse],
    summary="List assignments (role-scoped, paginated)",
)
async def list_assignments(
    current_user: CurrentUser,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
    params: Annotated[PageParams, Depends(page_params)],
    q: str | None = None,
    state: str | None = None,
    classIds: str | None = None,
) -> Page[AssignmentResponse]:
    """List assignments visible to the caller, filtered and paginated.

    Args:
        current_user: Authenticated principal.
        service: Assignment service.
        params: Pagination window.
        q: Title/subject substring.
        state: Lifecycle-state filter (``upcoming``…``graded``).
        classIds: Comma-separated classroom filter.

    Returns:
        Paginated assignments.

    Raises:
        ValidationFailedError: On unknown state values.
    """
    class_ids = [c.strip() for c in classIds.split(",") if c.strip()] if classIds else None
    page = await service.list_assignments(
        current_user, params, q=q, state=state, class_ids=class_ids
    )
    return Page[AssignmentResponse](
        items=[AssignmentResponse.model_validate(a) for a in page.items],
        page=page.page,
        page_size=page.page_size,
        total=page.total,
        total_pages=page.total_pages,
    )


@router.post(
    "",
    response_model=AssignmentResponse,
    summary="Create assignment (staff)",
)
async def create_assignment(
    payload: AssignmentCreate,
    current_user: StaffUser,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
) -> AssignmentResponse:
    """Create an assignment with classroom targets.

    Args:
        payload: Creation payload.
        current_user: Authenticated staff principal.
        service: Assignment service.

    Returns:
        The created assignment.
    """
    _ = _school(current_user)
    assignment = await service.create_assignment(current_user, payload)
    return AssignmentResponse.model_validate(assignment)


@router.get(
    "/{assignment_id}",
    response_model=AssignmentResponse,
    summary="Assignment detail",
)
async def get_assignment(
    assignment_id: UUID,
    current_user: CurrentUser,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
) -> AssignmentResponse:
    """Fetch one assignment (role/visibility enforced).

    Args:
        assignment_id: Assignment id.
        current_user: Authenticated principal.
        service: Assignment service.

    Returns:
        The assignment.

    Raises:
        NotFoundError: When absent or invisible to the caller.
    """
    assignment = await service.get_assignment(current_user, assignment_id)
    return AssignmentResponse.model_validate(assignment)


@router.patch(
    "/{assignment_id}",
    response_model=AssignmentResponse,
    summary="Update assignment (creator/admin)",
)
async def update_assignment(
    assignment_id: UUID,
    payload: AssignmentUpdate,
    current_user: StaffUser,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
) -> AssignmentResponse:
    """Apply a partial update to an assignment.

    Args:
        assignment_id: Assignment id.
        payload: Patch payload.
        current_user: Authenticated staff principal.
        service: Assignment service.

    Returns:
        The updated assignment.

    Raises:
        ForbiddenError: When a non-creator teacher attempts the edit.
    """
    assignment = await service.update_assignment(current_user, assignment_id, payload)
    return AssignmentResponse.model_validate(assignment)


@router.get(
    "/{assignment_id}/submissions",
    response_model=Page[SubmissionResponse],
    summary="List submissions (staff)",
)
async def list_submissions(
    assignment_id: UUID,
    current_user: StaffUser,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
    params: Annotated[PageParams, Depends(page_params)],
    onlyUngraded: bool = False,
) -> Page[SubmissionResponse]:
    """List an assignment's submissions (closes the legacy gap).

    Args:
        assignment_id: Assignment id.
        current_user: Authenticated staff principal.
        service: Assignment service.
        params: Pagination window.
        onlyUngraded: Restrict to ungraded submissions.

    Returns:
        Paginated submissions.
    """
    page = await service.list_submissions(
        current_user, assignment_id, params, only_ungraded=onlyUngraded
    )
    return Page[SubmissionResponse](
        items=[SubmissionResponse.model_validate(s) for s in page.items],
        page=page.page,
        page_size=page.page_size,
        total=page.total,
        total_pages=page.total_pages,
    )


@router.post(
    "/{assignment_id}/submit",
    response_model=SubmissionResponse,
    summary="Submit hand-in (student)",
    dependencies=[Depends(rate_limit("10/minute"))],
)
async def submit_assignment(
    assignment_id: UUID,
    payload: SubmissionCreate,
    current_user: CurrentUser,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
    submissions: Annotated[SubmissionRepository, Depends(get_submission_repo)],
) -> SubmissionResponse:
    """Create the student's submission with attachments.

    Args:
        assignment_id: Assignment id.
        payload: Attachment descriptors.
        current_user: Authenticated student.
        service: Assignment service.
        submissions: Submission repository (fresh read for the response).

    Returns:
        The created submission.

    Raises:
        ConflictError: On duplicate submission or closed window.
    """
    submission = await service.submit_assignment(current_user, assignment_id, payload)
    fresh = await submissions.get_loaded(submission.id)
    return SubmissionResponse.model_validate(fresh or submission)


@router.post(
    "/{assignment_id}/grade/{submission_id}",
    response_model=SubmissionResponse,
    summary="Override a submission's grade (staff)",
)
async def override_grade(
    assignment_id: UUID,
    submission_id: UUID,
    payload: SubmissionGradeRequest,
    current_user: StaffUser,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
) -> SubmissionResponse:
    """Apply a manual grade override with notes.

    Args:
        assignment_id: Assignment id.
        submission_id: Submission id.
        payload: Override payload.
        current_user: Authenticated staff principal.
        service: Assignment service.

    Returns:
        The updated submission.
    """
    submission = await service.override_grade(
        current_user, assignment_id, submission_id, payload
    )
    return SubmissionResponse.model_validate(submission)


@router.post(
    "/{assignment_id}/grade-all",
    response_model=BatchGradingTaskResponse,
    summary="Queue batch AI grading (staff)",
    dependencies=[Depends(rate_limit("5/minute"))],
)
async def grade_all(
    assignment_id: UUID,
    current_user: StaffUser,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
    tasks: Annotated[object, Depends(get_batch_task_repo)],
    payload: GradeAllRequest | None = None,
) -> BatchGradingTaskResponse:
    """Queue a background batch grading task for the assignment.

    Args:
        assignment_id: Assignment id.
        current_user: Authenticated staff principal.
        service: Assignment service (manageability check).
        tasks: Batch-task repository.
        payload: Optional mode (``UNGRADED_ONLY`` default).

    Returns:
        The created task's state.
    """
    await service.get_assignment(current_user, assignment_id)
    if current_user.role == "STUDENT":
        raise ForbiddenError("Teacher or administrator role required.")
    mode = BatchGradingMode(payload.mode if payload else "UNGRADED_ONLY")
    task_repo: BatchGradingTaskRepository = tasks  # type: ignore[assignment]
    task = await task_repo.add(
        BatchGradingTask(
            assignment_id=str(assignment_id),
            school_id=_school(current_user),
            trigger=BatchGradingTrigger.MANUAL,
            mode=mode,
            created_by_id=str(current_user.id),
        )
    )
    return BatchGradingTaskResponse.model_validate(task)


# ── Questions ────────────────────────────────────────────────────────────────


@router.get(
    "/{assignment_id}/questions",
    response_model=list[QuestionResponse],
    summary="List questions (answers stripped for students)",
)
async def list_questions(
    assignment_id: UUID,
    current_user: CurrentUser,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
) -> list[QuestionResponse]:
    """List an assignment's questions; students get a sanitized view.

    Args:
        assignment_id: Assignment id.
        current_user: Authenticated principal.
        service: Assignment service.

    Returns:
        Ordered questions.
    """
    questions = await service.list_questions(current_user, assignment_id)
    return [QuestionResponse.model_validate(q) for q in questions]


@router.post(
    "/{assignment_id}/questions",
    response_model=list[QuestionResponse],
    summary="Create questions (staff)",
)
async def create_questions(
    assignment_id: UUID,
    payload: QuestionsCreateRequest | QuestionCreate,
    current_user: StaffUser,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
) -> list[QuestionResponse]:
    """Append one or many questions to an online assignment.

    Accepts a single question object or a ``{questions: [...]}`` wrapper.

    Args:
        assignment_id: Assignment id.
        payload: Question(s) to create.
        current_user: Authenticated staff principal.
        service: Assignment service.

    Returns:
        The created questions.
    """
    payloads = (
        payload.questions if isinstance(payload, QuestionsCreateRequest) else [payload]
    )
    created = await service.create_questions(current_user, assignment_id, payloads)
    return [QuestionResponse.model_validate(q) for q in created]


@router.patch(
    "/{assignment_id}/questions/{question_id}",
    response_model=QuestionResponse,
    summary="Update question (staff)",
)
async def update_question(
    assignment_id: UUID,
    question_id: UUID,
    payload: QuestionUpdate,
    current_user: StaffUser,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
) -> QuestionResponse:
    """Update a question (options replaced when provided).

    Args:
        assignment_id: Assignment id.
        question_id: Question id.
        payload: Patch payload.
        current_user: Authenticated staff principal.
        service: Assignment service.

    Returns:
        The updated question.
    """
    question = await service.update_question(
        current_user, assignment_id, question_id, payload
    )
    return QuestionResponse.model_validate(question)


@router.delete(
    "/{assignment_id}/questions/{question_id}",
    status_code=204,
    summary="Delete question (staff)",
)
async def delete_question(
    assignment_id: UUID,
    question_id: UUID,
    current_user: StaffUser,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
) -> Response:
    """Delete a question and its options.

    Args:
        assignment_id: Assignment id.
        question_id: Question id.
        current_user: Authenticated staff principal.
        service: Assignment service.

    Returns:
        Empty 204 response.
    """
    await service.delete_question(current_user, assignment_id, question_id)
    return Response(status_code=204)


@router.patch(
    "/{assignment_id}/questions/reorder",
    status_code=204,
    summary="Reorder questions (staff)",
)
async def reorder_questions(
    assignment_id: UUID,
    payload: ReorderQuestionsRequest,
    current_user: StaffUser,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
) -> Response:
    """Apply a new display order to the assignment's questions.

    Args:
        assignment_id: Assignment id.
        payload: Question ids in the desired order.
        current_user: Authenticated staff principal.
        service: Assignment service.

    Returns:
        Empty 204 response.

    Raises:
        ValidationFailedError: When the id set does not match.
    """
    await service.reorder_questions(
        current_user, assignment_id, payload.question_ids
    )
    return Response(status_code=204)


# ── Rubric comments ──────────────────────────────────────────────────────────


@router.get(
    "/{assignment_id}/comments",
    response_model=list[RubricCommentResponse],
    summary="List rubric-thread comments",
)
async def list_comments(
    assignment_id: UUID,
    current_user: CurrentUser,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
) -> list[RubricCommentResponse]:
    """List the assignment's rubric discussion thread.

    Args:
        assignment_id: Assignment id.
        current_user: Authenticated principal.
        service: Assignment service.

    Returns:
        Oldest-first comments.
    """
    comments = await service.list_comments(current_user, assignment_id)
    return [RubricCommentResponse.model_validate(c) for c in comments]


@router.post(
    "/{assignment_id}/comments",
    response_model=RubricCommentResponse,
    summary="Post a rubric comment",
)
async def add_comment(
    assignment_id: UUID,
    payload: RubricCommentCreate,
    current_user: CurrentUser,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
) -> RubricCommentResponse:
    """Post a comment to the rubric discussion thread.

    Args:
        assignment_id: Assignment id.
        payload: Comment body.
        current_user: Authenticated principal.
        service: Assignment service.

    Returns:
        The created comment.
    """
    comment = await service.add_comment(current_user, assignment_id, payload.comment)
    return RubricCommentResponse.model_validate(comment)
