"""Analytics/audit wire schemas: batched client-event ingestion."""

from pydantic import Field

from app.schemas.common import BaseRequest, BaseSchema


class AuditEventInput(BaseRequest):
    """One client-side event in a batch.

    Attributes:
        action: Machine-readable event name.
        resource: Affected resource identifier, when applicable.
        details: Structured payload.
    """

    action: str = Field(min_length=1, max_length=128)
    resource: str | None = Field(default=None, max_length=255)
    details: dict[str, object] | None = None


class BatchAuditRequest(BaseRequest):
    """Batch of client events (typically flushed by an interval hook).

    Attributes:
        events: 1-100 events per request.
    """

    events: list[AuditEventInput] = Field(min_length=1, max_length=100)


class BatchAuditResponse(BaseSchema):
    """Ingestion acknowledgement.

    Attributes:
        accepted: Number of events persisted.
    """

    accepted: int
