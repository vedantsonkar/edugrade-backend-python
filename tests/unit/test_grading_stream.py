"""Unit tests for the grading-task SSE stream generator."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.api.v1.deps import Container, build_assignment_service
from app.api.v1.routes import assignments as assignments_routes
from app.api.v1.routes.assignments import grading_task_stream
from app.core.db.engine import session_scope
from app.core.events import grading_task_channel
from app.core.security.principal import UserPrincipal
from app.domain.assignments import Assignment
from app.domain.enums import (
    AssignmentType,
    BatchGradingTaskStatus,
    BatchGradingTrigger,
    GradingType,
    UserRole,
)
from app.domain.grading import BatchGradingTask
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.conftest import principal_for


async def _seed_assignment(
    sessionmaker: async_sessionmaker[AsyncSession], ids: dict[str, str]
) -> str:
    """Seed one offline assignment owned by the seeded teacher.

    Args:
        sessionmaker: Test session factory.
        ids: Seeded id mapping from the conftest fixture.

    Returns:
        The assignment id.
    """
    async with sessionmaker() as session:
        assignment = Assignment(
            school_id=ids["school"],
            teacher_id=ids["teacher"],
            type=AssignmentType.OFFLINE,
            title="Essay",
            subject="History",
            due_date=datetime.now(UTC) + timedelta(days=1),
            grading_type=GradingType.NUMBER,
        )
        session.add(assignment)
        await session.commit()
        return str(assignment.id)


async def _seed_task(
    sessionmaker: async_sessionmaker[AsyncSession],
    ids: dict[str, str],
    assignment_id: str,
    status: BatchGradingTaskStatus,
) -> str:
    """Seed a batch grading task in the given status.

    Args:
        sessionmaker: Test session factory.
        ids: Seeded id mapping.
        assignment_id: Owning assignment.
        status: Initial task status.

    Returns:
        The task id.
    """
    async with sessionmaker() as session:
        task = BatchGradingTask(
            assignment_id=assignment_id,
            school_id=ids["school"],
            trigger=BatchGradingTrigger.MANUAL,
            status=status,
            completed_at=datetime.now(UTC) if status is BatchGradingTaskStatus.COMPLETED else None,
        )
        session.add(task)
        await session.commit()
        return str(task.id)


async def _set_status(
    sessionmaker: async_sessionmaker[AsyncSession], task_id: str, status: BatchGradingTaskStatus
) -> None:
    """Transition the seeded task's status (simulating a worker pass).

    Args:
        sessionmaker: Test session factory.
        task_id: Task id.
        status: New status.
    """
    async with sessionmaker() as session:
        task = await session.get(BatchGradingTask, UUID(task_id))
        assert task is not None
        task.status = status
        await session.commit()


async def _snapshot(
    container: Container, principal: UserPrincipal, assignment_id: str
) -> tuple[str, bool]:
    """Build the initial stream payload exactly as the route does.

    Args:
        container: Test container.
        principal: Staff principal.
        assignment_id: Assignment id.

    Returns:
        Tuple of (payload JSON, terminal flag).
    """
    async with session_scope(container.sessionmaker) as session:
        service = build_assignment_service(session, container)
        task = await service.latest_grading(principal, UUID(assignment_id))
        assert task is not None
        payload = service.present_grading(task).model_dump_json(by_alias=True)
        return payload, task.status in assignments_routes.TERMINAL_TASK_STATUSES


async def _drain(gen: object) -> list[str]:
    """Collect remaining frames until the stream ends.

    Args:
        gen: The async generator under test.

    Returns:
        All frames yielded until exhaustion.
    """
    frames: list[str] = []
    async for frame in gen:  # type: ignore[union-attr]
        frames.append(frame)
    return frames


async def test_terminal_snapshot_yields_single_event(
    container_factory, seeded_ids: dict[str, str]
) -> None:
    """A terminal initial snapshot emits one frame and closes the stream."""
    container = container_factory()
    principal = principal_for(seeded_ids, UserRole.TEACHER)
    assignment_id = await _seed_assignment(container.sessionmaker, seeded_ids)
    await _seed_task(
        container.sessionmaker,
        seeded_ids,
        assignment_id,
        BatchGradingTaskStatus.COMPLETED,
    )
    payload, terminal = await _snapshot(container, principal, assignment_id)
    assert terminal

    gen = grading_task_stream(
        container=container,
        principal=principal,
        assignment_id=UUID(assignment_id),
        initial_payload=payload,
        initial_terminal=terminal,
    )

    frames = await _drain(gen)
    assert frames == [f"event: task\ndata: {payload}\n\n"]


async def test_pubsub_wake_up_emits_changed_state(
    container_factory, seeded_ids: dict[str, str]
) -> None:
    """A published event wakes the stream; the changed state is emitted."""
    container = container_factory()
    principal = principal_for(seeded_ids, UserRole.TEACHER)
    assignment_id = await _seed_assignment(container.sessionmaker, seeded_ids)
    task_id = await _seed_task(
        container.sessionmaker, seeded_ids, assignment_id, BatchGradingTaskStatus.QUEUED
    )
    payload, terminal = await _snapshot(container, principal, assignment_id)
    assert not terminal
    channel = grading_task_channel(assignment_id)

    gen = grading_task_stream(
        container=container,
        principal=principal,
        assignment_id=UUID(assignment_id),
        initial_payload=payload,
        initial_terminal=terminal,
    )
    first = await gen.__anext__()
    assert first == f"event: task\ndata: {payload}\n\n"

    # Simulate a worker pass: transition, then publish on the channel.
    await _set_status(container.sessionmaker, task_id, BatchGradingTaskStatus.RUNNING)
    broker = container.task_event_broker()
    await broker.publish(channel, '{"type":"grading-task","status":"RUNNING"}')
    second = await gen.__anext__()
    updated, _terminal = await _snapshot(container, principal, assignment_id)
    assert second == f"event: task\ndata: {updated}\n\n"
    assert "RUNNING" in second

    await gen.aclose()


async def test_terminal_after_change_closes_stream(
    container_factory, seeded_ids: dict[str, str]
) -> None:
    """When the re-read observes a terminal state, the stream closes itself."""
    container = container_factory()
    principal = principal_for(seeded_ids, UserRole.TEACHER)
    assignment_id = await _seed_assignment(container.sessionmaker, seeded_ids)
    task_id = await _seed_task(
        container.sessionmaker, seeded_ids, assignment_id, BatchGradingTaskStatus.RUNNING
    )
    payload, _terminal = await _snapshot(container, principal, assignment_id)
    channel = grading_task_channel(assignment_id)

    gen = grading_task_stream(
        container=container,
        principal=principal,
        assignment_id=UUID(assignment_id),
        initial_payload=payload,
        initial_terminal=False,
    )
    await gen.__anext__()

    await _set_status(container.sessionmaker, task_id, BatchGradingTaskStatus.COMPLETED)
    broker = container.task_event_broker()
    await broker.publish(channel, '{"type":"grading-task","status":"COMPLETED"}')

    frames = await _drain(gen)
    assert len(frames) == 1
    assert '"COMPLETED"' in frames[0]


async def test_heartbeat_and_deadline_close_idle_stream(
    container_factory, seeded_ids: dict[str, str], monkeypatch
) -> None:
    """Idle streams emit comment heartbeats and end at the lifetime cap."""
    monkeypatch.setattr(assignments_routes, "STREAM_TICK_SECONDS", 0.02)
    monkeypatch.setattr(assignments_routes, "STREAM_HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(assignments_routes, "STREAM_MAX_SECONDS", 0.2)
    container = container_factory()
    principal = principal_for(seeded_ids, UserRole.TEACHER)
    assignment_id = await _seed_assignment(container.sessionmaker, seeded_ids)
    await _seed_task(
        container.sessionmaker, seeded_ids, assignment_id, BatchGradingTaskStatus.QUEUED
    )
    payload, _terminal = await _snapshot(container, principal, assignment_id)

    gen = grading_task_stream(
        container=container,
        principal=principal,
        assignment_id=UUID(assignment_id),
        initial_payload=payload,
        initial_terminal=False,
    )
    frames = await _drain(gen)

    assert any(frame == ": ping\n\n" for frame in frames)
    assert not any(frame.startswith("event:") for frame in frames[1:])


async def test_missing_task_closes_stream(
    container_factory, seeded_ids: dict[str, str], monkeypatch
) -> None:
    """No task in scope: the stream ends without emitting any frame."""
    monkeypatch.setattr(assignments_routes, "STREAM_TICK_SECONDS", 0.02)
    container = container_factory()
    principal = principal_for(seeded_ids, UserRole.TEACHER)
    assignment_id = await _seed_assignment(container.sessionmaker, seeded_ids)

    gen = grading_task_stream(
        container=container,
        principal=principal,
        assignment_id=UUID(assignment_id),
        initial_payload=None,
        initial_terminal=False,
    )

    frames = await _drain(gen)
    assert frames == []
