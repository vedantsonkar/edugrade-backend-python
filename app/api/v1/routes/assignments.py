"""Assignment routes: CRUD, questions, submissions, comments, batch grading."""

from collections.abc import AsyncIterator
from time import monotonic
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse

from app.api.v1.deps import (
    Container,
    build_assignment_service,
    get_assessment_service,
    get_assignment_service,
    get_audit_service,
    get_batch_task_repo,
    get_container,
    get_submission_repo,
)
from app.core.db.engine import session_scope
from app.core.errors import AppError, ForbiddenError
from app.core.events import grading_task_channel
from app.core.pagination import Page, PageParams, page_params
from app.core.ratelimit.dependency import rate_limit
from app.core.security.guards import CurrentUser, StaffUser
from app.core.security.principal import UserPrincipal
from app.domain.enums import BatchGradingTaskStatus
from app.repositories.grading import SubmissionRepository
from app.schemas.assessments import AttemptResponse
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
from app.services.assessments import AssessmentService
from app.services.assignments import AssignmentService
from app.services.audit import AuditService
from app.services.files import FileService as _UnusedFiles  # noqa: F401

router = APIRouter(prefix="/assignments", tags=["assignments"])

STREAM_TICK_SECONDS = 3.0
"""Safety-tick interval: re-read state even without published events."""
STREAM_HEARTBEAT_SECONDS = 15.0
"""Idle interval for SSE comment heartbeats (proxy keep-alive)."""
STREAM_MAX_SECONDS = 60.0
"""Connection lifetime cap (under the frontend gateway's 125s fetch timeout);
clients reconnect transparently via EventSource."""
TERMINAL_TASK_STATUSES = frozenset(
    {
        BatchGradingTaskStatus.COMPLETED,
        BatchGradingTaskStatus.PARTIAL,
        BatchGradingTaskStatus.FAILED,
        BatchGradingTaskStatus.CANCELLED,
    }
)
"""Task states that end progress streaming."""


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
        items=[service.present(a, current_user) for a in page.items],
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
    request: Request,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> AssignmentResponse:
    """Create an assignment with classroom targets.

    Args:
        payload: Creation payload.
        current_user: Authenticated staff principal.
        request: Incoming request (audit provenance).
        service: Assignment service.
        audit: Audit service.

    Returns:
        The created assignment.
    """
    school_id = _school(current_user)
    assignment = await service.create_assignment(current_user, payload)
    await audit.record_event(
        school_id=school_id,
        principal=current_user,
        request=request,
        action="ASSIGNMENT_CREATED",
        resource=str(assignment.id),
        details={
            "title": assignment.title,
            "type": str(assignment.type),
            "targetClassIds": payload.class_ids,
        },
    )
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
    return service.present(assignment, current_user)


@router.patch(
    "/{assignment_id}",
    response_model=AssignmentResponse,
    summary="Update assignment (creator/admin)",
)
async def update_assignment(
    assignment_id: UUID,
    payload: AssignmentUpdate,
    current_user: StaffUser,
    request: Request,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> AssignmentResponse:
    """Apply a partial update to an assignment.

    Args:
        assignment_id: Assignment id.
        payload: Patch payload.
        current_user: Authenticated staff principal.
        request: Incoming request (audit provenance).
        service: Assignment service.
        audit: Audit service.

    Returns:
        The updated assignment.

    Raises:
        ForbiddenError: When a non-creator teacher attempts the edit.
    """
    assignment = await service.update_assignment(current_user, assignment_id, payload)
    await audit.record_event(
        school_id=_school(current_user),
        principal=current_user,
        request=request,
        action="ASSIGNMENT_UPDATED",
        resource=str(assignment.id),
        details={"fields": sorted(payload.model_dump(exclude_unset=True))},
    )
    return AssignmentResponse.model_validate(assignment)


@router.delete(
    "/{assignment_id}",
    status_code=204,
    summary="Delete assignment (creator/admin)",
)
async def delete_assignment(
    assignment_id: UUID,
    current_user: StaffUser,
    request: Request,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> Response:
    """Delete an assignment and all of its dependent rows.

    Args:
        assignment_id: Assignment id.
        current_user: Authenticated staff principal.
        request: Incoming request (audit provenance).
        service: Assignment service.
        audit: Audit service.

    Returns:
        Empty 204 response.

    Raises:
        ForbiddenError: When a non-creator teacher attempts the delete.
    """
    await service.delete_assignment(current_user, assignment_id)
    await audit.record_event(
        school_id=_school(current_user),
        principal=current_user,
        request=request,
        action="ASSIGNMENT_DELETED",
        resource=str(assignment_id),
    )
    return Response(status_code=204)


@router.get(
    "/{assignment_id}/submissions",
    response_model=Page[SubmissionResponse],
    summary="List submissions (staff)",
)
async def list_submissions(
    assignment_id: UUID,
    current_user: CurrentUser,
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
    submission = await service.override_grade(current_user, assignment_id, submission_id, payload)
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
    task = await service.queue_grading(
        current_user, assignment_id, payload.mode if payload else "UNGRADED_ONLY"
    )
    return service.present_grading(task)


@router.post(
    "/{assignment_id}/grading-tasks/{task_id}/retry",
    response_model=BatchGradingTaskResponse,
    summary="Retry a failed batch grading task (staff)",
    dependencies=[Depends(rate_limit("5/minute"))],
)
async def retry_grading_task(
    assignment_id: UUID,
    task_id: UUID,
    current_user: StaffUser,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
) -> BatchGradingTaskResponse:
    """Requeue a terminally failed task with a fresh retry budget.

    Args:
        assignment_id: Assignment id.
        task_id: Terminal grading task id.
        current_user: Authenticated staff principal.
        service: Assignment service (manageability check).

    Returns:
        The requeued task's state.

    Raises:
        ConflictError: When the task is still queued or running.
    """
    task = await service.retry_grading(current_user, assignment_id, task_id)
    return service.present_grading(task)


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
    payloads = payload.questions if isinstance(payload, QuestionsCreateRequest) else [payload]
    created = await service.create_questions(current_user, assignment_id, payloads)
    return [QuestionResponse.model_validate(q) for q in created]


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
    await service.reorder_questions(current_user, assignment_id, payload.question_ids)
    return Response(status_code=204)


# ── Rubric comments ──────────────────────────────────────────────────────────


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
    question = await service.update_question(current_user, assignment_id, question_id, payload)
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


@router.get("/{assignment_id}/grading-task", response_model=BatchGradingTaskResponse | None)
async def latest_grading_task(
    assignment_id: UUID,
    current_user: StaffUser,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
) -> BatchGradingTaskResponse | None:
    """Read current or completed batch progress for a visible assignment."""
    task = await service.latest_grading(current_user, assignment_id)
    return service.present_grading(task) if task else None


def _sse_event(payload: str) -> str:
    """Format one named SSE event carrying a task-state JSON payload.

    Args:
        payload: Serialized ``BatchGradingTaskResponse`` JSON.

    Returns:
        SSE frame text.
    """
    return f"event: task\ndata: {payload}\n\n"


async def _latest_task_payload(
    container: Container, principal: UserPrincipal, assignment_id: UUID
) -> tuple[str | None, bool]:
    """Read the latest task snapshot under the principal's permissions.

    Opens a short-lived session per call (stream handlers must not hold a
    request-scoped session across the response body) and routes the read
    through the service so school scoping and staff checks stay authoritative.

    Args:
        container: App container (sessionmaker, settings).
        principal: Authenticated staff principal captured at subscribe time.
        assignment_id: Assignment id.

    Returns:
        Tuple of (serialized payload, terminal-status flag); ``None`` payload
        when the task is gone or no longer visible.

    Raises:
        AppError: When the principal lost visibility of the assignment.
    """
    async with session_scope(container.sessionmaker) as session:
        service = build_assignment_service(session, container)
        task = await service.latest_grading(principal, assignment_id)
        if task is None:
            return None, True
        payload = service.present_grading(task).model_dump_json(by_alias=True)
        return payload, task.status in TERMINAL_TASK_STATUSES


async def grading_task_stream(
    *,
    container: Container,
    principal: UserPrincipal,
    assignment_id: UUID,
    initial_payload: str | None,
    initial_terminal: bool,
) -> AsyncIterator[str]:
    """Yield SSE frames tracking an assignment's batch grading task.

    Wake-ups come from pub/sub publishes on the assignment channel; a
    periodic safety tick re-reads state anyway, covering missed events and
    Redis unavailability (the broker degrades timeouts to ``None``). Every
    emitted frame is a fresh authoritative DB read scoped to the subscriber.

    Args:
        container: App container.
        principal: Authenticated staff principal captured at subscribe time.
        assignment_id: Assignment id.
        initial_payload: Pre-validated snapshot serialized by the route (or
            ``None`` when no task exists yet).
        initial_terminal: Whether the initial snapshot is terminal.

    Yields:
        SSE frames: ``event: task`` payloads and comment heartbeats.
    """
    if initial_payload is not None:
        yield _sse_event(initial_payload)
        if initial_terminal:
            return
    broker = container.task_event_broker()
    channel = grading_task_channel(str(assignment_id))
    await broker.subscribe(channel)
    try:
        deadline = monotonic() + STREAM_MAX_SECONDS
        last_event = monotonic()
        while monotonic() < deadline:
            await broker.wait_for_event(channel, STREAM_TICK_SECONDS)
            try:
                payload, terminal = await _latest_task_payload(
                    container, principal, assignment_id
                )
            except AppError:
                # The subscriber lost visibility (assignment deleted/permission
                # revoked): close the stream instead of erroring mid-response.
                return
            if payload is None:
                return
            if payload != initial_payload:
                initial_payload = payload
                last_event = monotonic()
                yield _sse_event(payload)
                if terminal:
                    return
            elif monotonic() - last_event >= STREAM_HEARTBEAT_SECONDS:
                yield ": ping\n\n"
                last_event = monotonic()
    finally:
        await broker.unsubscribe(channel)


@router.get(
    "/{assignment_id}/grading-task/stream",
    response_model=None,
    summary="Stream batch grading progress (staff, server-sent events)",
    dependencies=[Depends(rate_limit("30/minute"))],
)
async def stream_grading_task(
    assignment_id: UUID,
    current_user: StaffUser,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
    container: Annotated[Container, Depends(get_container)],
) -> StreamingResponse:
    """Push batch grading task updates until the task is terminal.

    The initial snapshot is validated with the request-scoped service so
    authorization failures surface as HTTP errors before the stream opens;
    subsequent frames are scoped re-reads inside the stream.

    Args:
        assignment_id: Assignment id.
        current_user: Authenticated staff principal.
        service: Assignment service (initial authorization + snapshot).
        container: App container.

    Returns:
        A ``text/event-stream`` response.
    """
    task = await service.latest_grading(current_user, assignment_id)
    payload = service.present_grading(task).model_dump_json(by_alias=True) if task else None
    return StreamingResponse(
        grading_task_stream(
            container=container,
            principal=current_user,
            assignment_id=assignment_id,
            initial_payload=payload,
            initial_terminal=task is not None and task.status in TERMINAL_TASK_STATUSES,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{assignment_id}/attempts", response_model=Page[AttemptResponse])
async def list_assignment_attempts(
    assignment_id: UUID,
    current_user: CurrentUser,
    service: Annotated[AssessmentService, Depends(get_assessment_service)],
    params: Annotated[PageParams, Depends(page_params)],
) -> Page[AttemptResponse]:
    """List answer-free attempt progress for visible assignment results."""
    page = await service.list_attempts(current_user, assignment_id, params)
    return Page.of(
        [service.present_attempt(a, current_user) for a in page.items],
        page.total,
        params,
    )
