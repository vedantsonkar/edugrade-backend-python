"""File-storage wire schemas: pre-signed upload/download grants."""

from pydantic import Field

from app.schemas.common import BaseRequest, BaseSchema


class PresignUploadRequest(BaseRequest):
    """Request a direct-upload grant.

    Attributes:
        name: Client filename (sanitized into the storage key).
        type: MIME type; bound into the pre-signature when provided.
    """

    name: str = Field(min_length=1, max_length=255)
    type: str | None = Field(default=None, max_length=128)


class PresignUploadResponse(BaseSchema):
    """Direct-upload grant.

    Attributes:
        key: Storage key the client must log with the target entity.
        url: Pre-signed PUT URL.
        expires_in: Seconds until expiry.
    """

    key: str
    url: str
    expires_in: int


class PresignDownloadResponse(BaseSchema):
    """Temporary read grant for a stored object.

    Attributes:
        key: Storage key served.
        url: Pre-signed GET URL.
        expires_in: Seconds until expiry.
    """

    key: str
    url: str
    expires_in: int
