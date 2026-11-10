"""Unit tests for the batch grading worker on the provider port."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.core.ids import Uuid7Provider
from app.domain.assignments import Assignment
from app.domain.enums import (
    AssignmentType,
    BatchGradingTaskStatus,
    BatchGradingTrigger,
    GradingType,
    UserRole,
)
from app.domain.grading import BatchGradingTask, Submission
from app.domain.schools import School
from app.domain.users import User
from app.services.ai.batching import BatchGradeOutcome, BatchJobState
from app.services.ai.ocr import OcrResult
from app.workers.batch_grader import BatchGradingProcessor
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
) -> BatchGradingProcessor:
    """Build the worker with fakes.

    Args:
        sessionmaker: Test session factory.
        batch: Scripted batch client.

    Returns:
        Processor ready for passes.
    """
    return BatchGradingProcessor(
        sessionmaker=sessionmaker,
        storage=InMemoryStorage(),
        ocr=_NoopOcr(),  # type: ignore[arg-type]
        batch_client=batch,  # type: ignore[arg-type]
        clock=FakeClock(),
        ids=Uuid7Provider(),
    )


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
        processor = _processor(sessionmaker, batch)
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

    async def test_missing_response_counts_failed(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        task_id, _submission_ids = await _seed(sessionmaker, grading_type=GradingType.NUMBER)
        batch = FakeBatchGradingClient()
        processor = _processor(sessionmaker, batch)
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
        processor = _processor(sessionmaker, batch)
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
