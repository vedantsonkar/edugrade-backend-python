"""Analytics routes: batched client-side event ingestion."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.v1.deps import get_audit_service
from app.core.ratelimit.dependency import rate_limit
from app.core.security.guards import CurrentUser
from app.schemas.audit import BatchAuditRequest, BatchAuditResponse
from app.services.audit import AuditService

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.post(
    "/batch",
    response_model=BatchAuditResponse,
    summary="Ingest batched client events",
    dependencies=[Depends(rate_limit("30/minute"))],
)
async def ingest_batch(
    payload: BatchAuditRequest,
    current_user: CurrentUser,
    request: Request,
    service: Annotated[AuditService, Depends(get_audit_service)],
) -> BatchAuditResponse:
    """Persist a batch of client-side audit events.

    Args:
        payload: 1-100 events.
        current_user: Authenticated principal (attribution).
        request: Incoming request (IP/user-agent provenance).
        service: Audit service.

    Returns:
        Ingestion acknowledgement.
    """
    accepted = await service.record(
        school_id=current_user.school_id,
        principal=current_user,
        request=request,
        events=payload.events,
    )
    return BatchAuditResponse(accepted=accepted)
