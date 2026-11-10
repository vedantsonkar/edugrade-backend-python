"""Unit tests for the batch grading worker on the provider port."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from app.core.errors import ConflictError
from app.core.ids import Uuid7Provider
from app.core.security.principal import UserPrincipal
from app.domain.assignments import Assignment
from app.domain.enums import (
    AssignmentType,
    BatchGradingTaskStatus,
    BatchGradingTrigger,
    GradingAttemptStatus,
    GradingType,
    UserRole,
)
from app.domain.grading import BatchGradingTask, GradingAttempt, Submission
from app.domain.schools import School
from app.domain.users import User
from app.repositories.assignments import (
    AssignmentRepository,
    AssignmentTargetRepository,
    QuestionRepository,
    RubricCommentRepository,
)
from app.repositories.grading import (
    AttachmentRepository,
    BatchGradingTaskRepository,
    GradingAttemptRepository,
    SubmissionRepository,
)
from app.repositories.taxonomy import ClassroomRepository
from app.services.ai.batching import (
    BatchGradeOutcome,
    BatchGradeRequest,
    BatchJobHandle,
    BatchJobSnapshot,
    BatchJobState,
)
from app.services.ai.ocr import OcrResult
from app.services.ai.schema import GradingResult
from app.services.assignments import AssignmentService
from app.workers.batch_grader import BatchGradingProcessor
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.fakes import FakeBatchGradingClient, FakeClock, InMemoryStorage


class _NoopOcr:
    async def extract_text(self, data: bytes, mime_type: str) -> OcrResult:
        _ = data, mime_type
        return OcrResult(text="", confidence=None)


async def _seed(
    sessionmaker: async_sessionmaker[AsyncSession], *, grading_type: GradingType
) -> tuple[str, list[str]]:
    """Seed school/teacher/students/assignment/submissions + a QUEUED task.

    Args:
        sessionmaker: Test session factory.
        grading_type: Grading type on the assignment.

    Returns:
        Tuple of (task id, submission ids in creation order).
    """
    async with sessionmaker() as session:
        school = School(name="Test School", school_key="test-school", admin_key="key-1")
        session.add(school)
        await session.flush()
        teacher = User(
            email="t@school.test", name="Teacher", role=UserRole.TEACHER, school_id=school.id
        )
        session.add(teacher)
        await session.flush()
        assignment = Assignment(
            school_id=school.id,
            teacher_id=teacher.id,
            type=AssignmentType.OFFLINE,
            title="Essay",
            subject="History",
            due_date=datetime.now(UTC) - timedelta(days=1),
            grading_type=grading_type,
        )
        session.add(assignment)
        await session.flush()
        submission_ids: list[str] = []
        for i in range(2):
            student = User(
                email=f"s{i}@school.test",
                name=f"Student {i}",
                role=UserRole.STUDENT,
                school_id=school.id,
            )
            session.add(student)
            await session.flush()
            submission = Submission(
                assignment_id=assignment.id,
                student_id=student.id,
                submitted_at=datetime.now(UTC),
            )
            session.add(submission)
            await session.flush()
            submission_ids.append(str(submission.id))
        task = BatchGradingTask(
            assignment_id=assignment.id,
            school_id=school.id,
            trigger=BatchGradingTrigger.MANUAL,
        )
        session.add(task)
        await session.commit()
        return str(task.id), submission_ids


def _processor(
    sessionmaker: async_sessionmaker[AsyncSession],
    batch: FakeBatchGradingClient,
    clock: FakeClock | None = None,
    events: object | None = None,
    **retry_policy: int,
) -> BatchGradingProcessor:
    """Build the worker with fakes.

    Args:
        sessionmaker: Test session factory.
        batch: Scripted batch client.
        clock: Optional shared fake clock.
        events: Optional event publisher fake.
        retry_policy: Overrides for ``max_attempts``/``fast_retries``/backoff.

    Returns:
        Processor ready for passes.
    """
    return BatchGradingProcessor(
        sessionmaker=sessionmaker,
        storage=InMemoryStorage(),
        ocr=_NoopOcr(),  # type: ignore[arg-type]
        batch_client=batch,  # type: ignore[arg-type]
        clock=clock or FakeClock(),
        ids=Uuid7Provider(),
        events=events,  # type: ignore[arg-type]
        **retry_policy,
    )


async def _attempts(
    sessionmaker: async_sessionmaker[AsyncSession], task_id: str
) -> list[GradingAttempt]:
    """Load a task's attempts oldest-first.

    Args:
        sessionmaker: Test session factory.
        task_id: Task id.

    Returns:
        Attempt rows ordered by attempt number.
    """
    async with sessionmaker() as session:
        stmt = (
            select(GradingAttempt)
            .where(GradingAttempt.task_id == task_id)
            .order_by(GradingAttempt.attempt_number)
        )
        rows = (await session.execute(stmt)).scalars().all()
        return list(rows)


def _aware(value: datetime) -> datetime:
    """Normalize a stored datetime to aware UTC (sqlite drops tzinfo).

    Args:
        value: Datetime from the ORM.

    Returns:
        Aware datetime.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _task_status(
    sessionmaker: async_sessionmaker[AsyncSession], task_id: str
) -> BatchGradingTask:
    """Reload a task row.

    Args:
        sessionmaker: Test session factory.
        task_id: Task id.

    Returns:
        The reloaded task.
    """
    async with sessionmaker() as session:
        task = await session.get(BatchGradingTask, uuid.UUID(task_id))
        assert task is not None
        return task


@pytest.fixture
def sessionmaker(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    """Reuse the conftest sqlite sessionmaker.

    Args:
        db_sessionmaker: Conftest sessionmaker.

    Returns:
        The sessionmaker.
    """
    return db_sessionmaker


class TestStartQueuedTasks:
    async def test_task_transitions_to_running(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        task_id, submission_ids = await _seed(sessionmaker, grading_type=GradingType.NUMBER)
        batch = FakeBatchGradingClient()
        processor = _processor(sessionmaker, batch)

        started = await processor.start_queued_grading_tasks()

        assert started == 1
        assert len(batch.created_jobs) == 1
        assert [r.key for r in batch.created_jobs[0]] == sorted(
            r.key for r in batch.created_jobs[0]
        )
        task = await _task_status(sessionmaker, task_id)
        assert task.status is BatchGradingTaskStatus.RUNNING
        assert task.total_submissions == 2
        assert task.request_mappings is not None
        assert set(task.request_mappings.values()) == set(submission_ids)
        assert task.gemini_input_file_name == "input-1"


class TestPollRunningTasks:
    async def test_success_finalizes_completed(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        task_id, submission_ids = await _seed(sessionmaker, grading_type=GradingType.NUMBER)
        batch = FakeBatchGradingClient()
        processor = _processor(sessionmaker, batch)
        await processor.start_queued_grading_tasks()

        finalized = await processor.poll_running_grading_tasks()

        assert finalized == 1
        task = await _task_status(sessionmaker, task_id)
        assert task.status is BatchGradingTaskStatus.COMPLETED
        assert task.graded_submissions == 2
        assert task.failed_submissions == 0
        assert batch.disposed == [("job-1", "input-1")]
        async with sessionmaker() as session:
            for sid in submission_ids:
                submission = await session.get(Submission, uuid.UUID(sid))
                assert submission is not None
                assert submission.grade == 4.0
                assert submission.ai_summary == "Excellent work."

    async def test_per_request_failure_isolated_partial(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        task_id, _submission_ids = await _seed(sessionmaker, grading_type=GradingType.NUMBER)
        batch = FakeBatchGradingClient()
        processor = _processor(sessionmaker, batch, max_attempts=1)
        await processor.start_queued_grading_tasks()
        keys = list((await _task_status(sessionmaker, task_id)).request_mappings or {})
        batch.outcomes = [
            BatchGradeOutcome(key=keys[0], error="malformed output"),
            BatchGradeOutcome.failed(keys[1], "provider error"),
        ]

        finalized = await processor.poll_running_grading_tasks()

        assert finalized == 1
        task = await _task_status(sessionmaker, task_id)
        assert task.status is BatchGradingTaskStatus.PARTIAL
        assert task.graded_submissions == 0
        assert task.failed_submissions == 2
        assert task.failure_details is not None
        assert len(task.failure_details) == 2

    async def test_per_request_failure_enters_retry_queue(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        task_id, _submission_ids = await _seed(sessionmaker, grading_type=GradingType.NUMBER)
        batch = FakeBatchGradingClient()
        processor = _processor(sessionmaker, batch)
        await processor.start_queued_grading_tasks()
        keys = list((await _task_status(sessionmaker, task_id)).request_mappings or {})
        batch.outcomes = [
            BatchGradeOutcome(key=keys[0], error="malformed output"),
            BatchGradeOutcome.failed(keys[1], "provider error"),
        ]

        await processor.poll_running_grading_tasks()

        task = await _task_status(sessionmaker, task_id)
        assert task.status is BatchGradingTaskStatus.QUEUED
        assert task.retry_count == 1

    async def test_missing_response_counts_failed(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        task_id, _submission_ids = await _seed(sessionmaker, grading_type=GradingType.NUMBER)
        batch = FakeBatchGradingClient()
        processor = _processor(sessionmaker, batch, max_attempts=1)
        await processor.start_queued_grading_tasks()
        keys = list((await _task_status(sessionmaker, task_id)).request_mappings or {})
        batch.outcomes = [BatchGradeOutcome(key=keys[0], error="no text")]

        await processor.poll_running_grading_tasks()

        task = await _task_status(sessionmaker, task_id)
        assert task.status is BatchGradingTaskStatus.PARTIAL
        assert task.graded_submissions == 0
        assert task.failed_submissions == 2
        assert task.failure_details is not None
        errors = {detail["error"] for detail in task.failure_details}
        assert "no text" in errors
        assert "no response" in errors

    async def test_failed_job_state_fails_task(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        task_id, _submission_ids = await _seed(sessionmaker, grading_type=GradingType.NUMBER)
        batch = FakeBatchGradingClient()
        batch.state = BatchJobState.FAILED
        processor = _processor(sessionmaker, batch, max_attempts=1)
        await processor.start_queued_grading_tasks()

        finalized = await processor.poll_running_grading_tasks()

        assert finalized == 1
        task = await _task_status(sessionmaker, task_id)
        assert task.status is BatchGradingTaskStatus.FAILED
        assert "FAILED" in (task.error_message or "")

    async def test_running_job_not_finalized(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        task_id, _submission_ids = await _seed(sessionmaker, grading_type=GradingType.NUMBER)
        batch = FakeBatchGradingClient()
        batch.state = BatchJobState.RUNNING
        processor = _processor(sessionmaker, batch)
        await processor.start_queued_grading_tasks()

        finalized = await processor.poll_running_grading_tasks()

        assert finalized == 0
        task = await _task_status(sessionmaker, task_id)
        assert task.status is BatchGradingTaskStatus.RUNNING


class TestAlphabetGrading:
    async def test_letter_derived_from_anchor(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        task_id, submission_ids = await _seed(sessionmaker, grading_type=GradingType.ALPHABET)
        batch = FakeBatchGradingClient()
        processor = _processor(sessionmaker, batch)
        await processor.start_queued_grading_tasks()
        await processor.poll_running_grading_tasks()

        task = await _task_status(sessionmaker, task_id)
        assert task.status is BatchGradingTaskStatus.COMPLETED
        async with sessionmaker() as session:
            submission = await session.get(Submission, uuid.UUID(submission_ids[0]))
            assert submission is not None
            assert submission.ai_grade_letter == "A"
            assert submission.grade is None


class _ExplodingBatch:
    """Batch client whose ``create_job`` always fails (provider outage)."""

    async def create_job(self, requests: Sequence[BatchGradeRequest]) -> BatchJobHandle:
        """Always fail.

        Args:
            requests: Unused requests.

        Raises:
            RuntimeError: Simulated provider outage.
        """
        raise RuntimeError("provider unavailable")

    async def poll(self, job_name: str) -> BatchJobSnapshot:
        """Unreachable in these tests.

        Args:
            job_name: Unused job name.
        """
        raise AssertionError("poll should not be reached")

    async def collect(
        self, job_name: str, snapshot: BatchJobSnapshot
    ) -> list[BatchGradeOutcome]:
        """Unreachable in these tests.

        Args:
            job_name: Unused job name.
            snapshot: Unused snapshot.
        """
        raise AssertionError("collect should not be reached")

    async def dispose(
        self, job_name: str, input_ref: str | None, snapshot: BatchJobSnapshot
    ) -> None:
        """Nothing to clean up.

        Args:
            job_name: Unused job name.
            input_ref: Unused input ref.
            snapshot: Unused snapshot.
        """
        _ = job_name, input_ref, snapshot


class TestRetryOnFailure:
    async def test_start_failure_requeues_with_attempt_history(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        task_id, _submission_ids = await _seed(sessionmaker, grading_type=GradingType.NUMBER)
        clock = FakeClock()
        processor = _processor(sessionmaker, _ExplodingBatch(), clock)  # type: ignore[arg-type]

        started = await processor.start_queued_grading_tasks()

        assert started == 0
        task = await _task_status(sessionmaker, task_id)
        assert task.status is BatchGradingTaskStatus.QUEUED
        assert task.retry_count == 1
        assert _aware(task.next_retry_at) == clock.now()
        assert task.completed_at is None
        history = await _attempts(sessionmaker, task_id)
        assert [a.status for a in history] == [
            GradingAttemptStatus.FAILED,
            GradingAttemptStatus.PENDING,
        ]
        assert [a.attempt_number for a in history] == [1, 2]
        assert history[0].error_message == "Failed to start batch grading."

    async def test_backoff_after_fast_retries_then_gate_withholds(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        task_id, _submission_ids = await _seed(sessionmaker, grading_type=GradingType.NUMBER)
        clock = FakeClock()
        processor = _processor(
            sessionmaker,
            _ExplodingBatch(),  # type: ignore[arg-type]
            clock,
            max_attempts=5,
            fast_retries=1,
            backoff_base_seconds=60,
            backoff_cap_seconds=900,
        )

        await processor.start_queued_grading_tasks()  # retry 1: immediate
        task = await _task_status(sessionmaker, task_id)
        assert _aware(task.next_retry_at) == clock.now()

        await processor.start_queued_grading_tasks()  # retry 2: backoff 60s
        task = await _task_status(sessionmaker, task_id)
        assert task.retry_count == 2
        assert _aware(task.next_retry_at) == clock.now() + timedelta(seconds=60)

        started = await processor.start_queued_grading_tasks()  # gate still closed
        assert started == 0
        task = await _task_status(sessionmaker, task_id)
        assert task.retry_count == 2

    async def test_terminal_failed_after_budget_exhausted(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        task_id, _submission_ids = await _seed(sessionmaker, grading_type=GradingType.NUMBER)
        batch = FakeBatchGradingClient()
        batch.state = BatchJobState.FAILED
        processor = _processor(sessionmaker, batch, max_attempts=3, fast_retries=2)

        for _ in range(3):  # initial run + 2 retries, each: start → poll
            await processor.start_queued_grading_tasks()
            await processor.poll_running_grading_tasks()

        task = await _task_status(sessionmaker, task_id)
        assert task.status is BatchGradingTaskStatus.FAILED
        assert task.retry_count == 3
        assert task.next_retry_at is None
        assert task.completed_at is not None
        assert "Provider job state: FAILED" in (task.error_message or "")
        history = await _attempts(sessionmaker, task_id)
        assert len(history) == 3
        assert all(a.status is GradingAttemptStatus.FAILED for a in history)

    async def test_provider_failure_recovers_on_next_attempt(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        task_id, submission_ids = await _seed(sessionmaker, grading_type=GradingType.NUMBER)
        batch = FakeBatchGradingClient()
        batch.state = BatchJobState.FAILED
        processor = _processor(sessionmaker, batch)

        await processor.start_queued_grading_tasks()
        await processor.poll_running_grading_tasks()
        task = await _task_status(sessionmaker, task_id)
        assert task.status is BatchGradingTaskStatus.QUEUED

        batch.state = BatchJobState.SUCCEEDED
        await processor.start_queued_grading_tasks()
        await processor.poll_running_grading_tasks()

        task = await _task_status(sessionmaker, task_id)
        assert task.status is BatchGradingTaskStatus.COMPLETED
        assert task.retry_count == 1
        history = await _attempts(sessionmaker, task_id)
        assert [a.status for a in history] == [
            GradingAttemptStatus.FAILED,
            GradingAttemptStatus.SUCCESS,
        ]
        async with sessionmaker() as session:
            submission = await session.get(Submission, uuid.UUID(submission_ids[0]))
            assert submission is not None and submission.grade == 4.0

    async def test_partial_retries_only_ungraded_and_preserves_grades(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        task_id, submission_ids = await _seed(sessionmaker, grading_type=GradingType.NUMBER)
        batch = FakeBatchGradingClient()
        processor = _processor(sessionmaker, batch)
        await processor.start_queued_grading_tasks()
        keys = list((await _task_status(sessionmaker, task_id)).request_mappings or {})
        batch.outcomes = [
            BatchGradeOutcome(
                key=keys[0],
                result=GradingResult(
                    score=4.0, summary="Excellent work.", feedback="Clear and complete."
                ),
            ),
            BatchGradeOutcome.failed(keys[1], "provider error"),
        ]

        await processor.poll_running_grading_tasks()

        task = await _task_status(sessionmaker, task_id)
        assert task.status is BatchGradingTaskStatus.QUEUED  # entered the retry queue
        assert task.retry_count == 1
        history = await _attempts(sessionmaker, task_id)
        assert history[0].status is GradingAttemptStatus.FAILED
        assert history[0].graded_submissions == 1
        assert history[0].failed_submissions == 1
        assert history[1].status is GradingAttemptStatus.PENDING
        async with sessionmaker() as session:
            first = await session.get(Submission, uuid.UUID(submission_ids[0]))
            assert first is not None and first.grade == 4.0

        batch.outcomes = None  # second run: default success per request
        await processor.start_queued_grading_tasks()
        await processor.poll_running_grading_tasks()

        task = await _task_status(sessionmaker, task_id)
        assert task.status is BatchGradingTaskStatus.COMPLETED
        assert task.retry_count == 1
        assert len(batch.created_jobs) == 2
        assert len(batch.created_jobs[1]) == 1  # only the ungraded leftover
        history = await _attempts(sessionmaker, task_id)
        assert [a.status for a in history] == [
            GradingAttemptStatus.FAILED,
            GradingAttemptStatus.SUCCESS,
        ]
        async with sessionmaker() as session:
            second = await session.get(Submission, uuid.UUID(submission_ids[1]))
            assert second is not None and second.grade == 4.0

    async def test_empty_scope_finalizes_completed(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        task_id, submission_ids = await _seed(sessionmaker, grading_type=GradingType.NUMBER)
        async with sessionmaker() as session:
            for sid in submission_ids:
                submission = await session.get(Submission, uuid.UUID(sid))
                assert submission is not None
                submission.grade = 5.0
            await session.commit()
        batch = FakeBatchGradingClient()
        processor = _processor(sessionmaker, batch)

        started = await processor.start_queued_grading_tasks()

        assert started == 0
        assert len(batch.created_jobs) == 0
        task = await _task_status(sessionmaker, task_id)
        assert task.status is BatchGradingTaskStatus.COMPLETED
        history = await _attempts(sessionmaker, task_id)
        assert len(history) == 1
        assert history[0].status is GradingAttemptStatus.SUCCESS


class _RecordingPublisher:
    """Capture pub/sub publishes for assertions."""

    def __init__(self) -> None:
        """Start with an empty event log."""
        self.events: list[tuple[str, str]] = []

    async def publish(self, channel: str, payload: str) -> None:
        """Record the publish.

        Args:
            channel: Channel key.
            payload: Event payload.
        """
        self.events.append((channel, payload))


class _FailingPublisher:
    """Publisher whose backend is down."""

    async def publish(self, channel: str, payload: str) -> None:
        """Always fail.

        Args:
            channel: Unused channel key.
            payload: Unused payload.

        Raises:
            RuntimeError: Simulated broker outage.
        """
        raise RuntimeError("broker down")


class TestEventPublishing:
    async def test_transitions_publish_state_events(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        task_id, _submission_ids = await _seed(sessionmaker, grading_type=GradingType.NUMBER)
        publisher = _RecordingPublisher()
        batch = FakeBatchGradingClient()
        batch.state = BatchJobState.FAILED
        processor = _processor(sessionmaker, batch, events=publisher, max_attempts=1)

        await processor.start_queued_grading_tasks()
        await processor.poll_running_grading_tasks()

        task = await _task_status(sessionmaker, task_id)
        channels = {channel for channel, _payload in publisher.events}
        assert channels == {f"grading-task:{task.assignment_id}"}
        payloads = [payload for _channel, payload in publisher.events]
        assert any('"status":"RUNNING"' in payload for payload in payloads)
        assert any('"status":"FAILED"' in payload for payload in payloads)
        assert len(publisher.events) == 2

    async def test_publish_failure_does_not_break_grading(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        task_id, _submission_ids = await _seed(sessionmaker, grading_type=GradingType.NUMBER)
        batch = FakeBatchGradingClient()
        batch.state = BatchJobState.FAILED
        processor = _processor(sessionmaker, batch, events=_FailingPublisher(), max_attempts=1)

        await processor.start_queued_grading_tasks()
        await processor.poll_running_grading_tasks()

        task = await _task_status(sessionmaker, task_id)
        assert task.status is BatchGradingTaskStatus.FAILED


class TestManualRetry:
    async def _failed_task(
        self, sessionmaker: async_sessionmaker[AsyncSession], max_attempts: int = 2
    ) -> tuple[str, str, str, str, FakeClock]:
        """Drive a seeded task to terminal FAILED via the worker.

        Args:
            sessionmaker: Test session factory.
            max_attempts: Retry budget for the driving processor.

        Returns:
            Tuple of (task id, assignment id, teacher id, school id, clock).
        """
        task_id, _submission_ids = await _seed(sessionmaker, grading_type=GradingType.NUMBER)
        clock = FakeClock()
        batch = FakeBatchGradingClient()
        batch.state = BatchJobState.FAILED
        processor = _processor(sessionmaker, batch, clock, max_attempts=max_attempts)
        for _ in range(max_attempts):
            await processor.start_queued_grading_tasks()
            await processor.poll_running_grading_tasks()
        async with sessionmaker() as session:
            task = await session.get(BatchGradingTask, uuid.UUID(task_id))
            assert task is not None
            assignment = await session.get(Assignment, uuid.UUID(str(task.assignment_id)))
            assert assignment is not None
            return (
                task_id,
                str(assignment.id),
                str(assignment.teacher_id),
                str(task.school_id),
                clock,
            )

    def _service(self, session: AsyncSession, teacher_id: str, school_id: str) -> AssignmentService:
        """Build the assignment service over one session.

        Args:
            session: Open session.
            teacher_id: Assignment creator (manageability check).
            school_id: Tenant.

        Returns:
            Service with fakes.
        """
        return AssignmentService(
            assignments=AssignmentRepository(session),
            targets=AssignmentTargetRepository(session),
            questions=QuestionRepository(session),
            comments=RubricCommentRepository(session),
            submissions=SubmissionRepository(session),
            attachments=AttachmentRepository(session),
            classrooms=ClassroomRepository(session),
            clock=FakeClock(),
            tasks=BatchGradingTaskRepository(session),
            attempts=GradingAttemptRepository(session),
        )

    def _principal(self, teacher_id: str, school_id: str) -> UserPrincipal:
        """Build a teacher principal.

        Args:
            teacher_id: Authenticated user id.
            school_id: Tenant.

        Returns:
            Staff principal.
        """
        return UserPrincipal(
            id=uuid.UUID(teacher_id),
            email="t@school.test",
            name="Teacher",
            role=UserRole.TEACHER.value,
            school_id=school_id,
            school_name="Test School",
            school_key="test-school",
            enable_logging=False,
        )

    async def test_retry_requeues_failed_task_with_fresh_budget(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        task_id, assignment_id, teacher_id, school_id, _clock = await self._failed_task(
            sessionmaker
        )
        async with sessionmaker() as session:
            service = self._service(session, teacher_id, school_id)
            principal = self._principal(teacher_id, school_id)

            task = await service.retry_grading(
                principal, uuid.UUID(assignment_id), uuid.UUID(task_id)
            )

            assert task.status is BatchGradingTaskStatus.QUEUED
            assert task.retry_count == 0
            assert task.next_retry_at is None
            assert task.error_message is None
            assert task.completed_at is None
            history = await _attempts(sessionmaker, task_id)
            assert history[-1].status is GradingAttemptStatus.PENDING
            assert history[-1].attempt_number == 3

    async def test_retry_rejects_active_task(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        task_id, _submission_ids = await _seed(sessionmaker, grading_type=GradingType.NUMBER)
        async with sessionmaker() as session:
            task = await session.get(BatchGradingTask, uuid.UUID(task_id))
            assert task is not None
            assignment = await session.get(Assignment, uuid.UUID(str(task.assignment_id)))
            assert assignment is not None
            assignment_id = str(assignment.id)
            teacher_id = str(assignment.teacher_id)
            school_id = str(task.school_id)
        async with sessionmaker() as session:
            service = self._service(session, teacher_id, school_id)
            principal = self._principal(teacher_id, school_id)

            with pytest.raises(ConflictError):
                await service.retry_grading(
                    principal, uuid.UUID(assignment_id), uuid.UUID(task_id)
                )
