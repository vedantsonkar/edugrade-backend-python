"""CRON/system webhook routes (key-protected by CRON_SECRET)."""

import hmac
import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request

from app.api.v1.deps import Container, ContainerDep
from app.core.errors import ForbiddenError
from app.core.ratelimit.dependency import rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cron", tags=["cron"])


def require_cron_secret(
    container: Container = ContainerDep,
    key: Annotated[str | None, Query(alias="key")] = None,
) -> None:
    """Guard shared by all CRON webhooks.

    Args:
        container: App container (secret).
        key: Presented secret.

    Raises:
        ForbiddenError: On missing/mismatched secrets.
    """
    if not key or not hmac.compare_digest(key, container.settings.cron_secret):
        raise ForbiddenError("Invalid cron key.")


@router.get(
    "/grading",
    summary="Trigger the batch grading worker pass",
    dependencies=[Depends(require_cron_secret), Depends(rate_limit("10/minute"))],
)
async def run_grading(
    request: Request, background_tasks: BackgroundTasks
) -> dict[str, str]:
    """Run one enqueue → start → poll pass in the background.

    Args:
        request: Incoming request (app state access).
        background_tasks: FastAPI background scheduler.

    Returns:
        Trigger acknowledgement.
    """
    processor = request.app.state.batch_grader

    async def _run() -> None:
        """Execute the worker pass with full logging."""
        try:
            stats = await processor.run_pass()
            logger.info("Batch grading pass: %s", stats)
        except Exception:
            logger.exception("Batch grading pass failed")

    background_tasks.add_task(_run)
    return {"status": "triggered", "message": "Batch grading pass scheduled."}


@router.get(
    "/archive",
    summary="Trigger the audit-log archiver",
    dependencies=[Depends(require_cron_secret), Depends(rate_limit("5/minute"))],
)
async def run_archive(
    request: Request, background_tasks: BackgroundTasks
) -> dict[str, str]:
    """Archive audit logs older than 24h to storage and purge them.

    Args:
        request: Incoming request (app state access).
        background_tasks: FastAPI background scheduler.

    Returns:
        Trigger acknowledgement.
    """
    archiver = request.app.state.audit_archiver

    async def _run() -> None:
        """Execute the archiver with full logging."""
        try:
            stats = await archiver.archive_and_purge()
            logger.info("Audit archive pass: %s", stats)
        except Exception:
            logger.exception("Audit archive pass failed")

    background_tasks.add_task(_run)
    return {"status": "triggered", "message": "Audit archiver scheduled."}
