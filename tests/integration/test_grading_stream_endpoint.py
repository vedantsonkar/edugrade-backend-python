"""Integration test for the grading-task SSE endpoint."""

import asyncio
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.fakes import FakeClock
from tests.integration.test_frontend_contracts import assignment, login


def test_grading_task_stream_emits_terminal_snapshot_and_closes(
    client: TestClient,
    seeded_ids: dict[str, str],
    db_sessionmaker: async_sessionmaker[AsyncSession],
    clock: FakeClock,
) -> None:
    """A terminal task streams exactly one SSE frame, then the server closes."""
    from app.domain.enums import BatchGradingTaskStatus, BatchGradingTrigger
    from app.domain.grading import BatchGradingTask

    login(client, "teacher")
    assignment_id = assignment(client, seeded_ids, clock)

    async def seed_terminal_task() -> None:
        """Insert a terminal task for the API-created assignment."""
        async with db_sessionmaker() as session:
            session.add(
                BatchGradingTask(
                    assignment_id=assignment_id,
                    school_id=seeded_ids["school"],
                    trigger=BatchGradingTrigger.MANUAL,
                    status=BatchGradingTaskStatus.COMPLETED,
                    completed_at=datetime.now(UTC),
                )
            )
            await session.commit()

    asyncio.run(seed_terminal_task())

    with client.stream(
        "GET", f"/api/v1/assignments/{assignment_id}/grading-task/stream"
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(chunk for chunk in response.iter_text())

    assert body.count("event: task") == 1
    assert '"status":"COMPLETED"' in body
