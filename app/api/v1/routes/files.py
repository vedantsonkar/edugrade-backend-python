"""File-storage routes: pre-signed upload/download grants."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.v1.deps import get_file_service
from app.core.ratelimit.dependency import rate_limit
from app.core.security.guards import CurrentUser
from app.schemas.files import PresignDownloadResponse, PresignUploadRequest, PresignUploadResponse
from app.services.files import FileService

router = APIRouter(prefix="/files", tags=["files"])


@router.post(
    "/presigned-url",
    response_model=PresignUploadResponse,
    summary="Request a direct-upload grant",
    dependencies=[Depends(rate_limit("30/minute"))],
)
async def create_upload_url(
    payload: PresignUploadRequest,
    current_user: CurrentUser,
    files: Annotated[FileService, Depends(get_file_service)],
) -> PresignUploadResponse:
    """Issue a pre-signed PUT URL scoped to the caller's school folder.

    Args:
        payload: Filename and optional MIME type.
        current_user: Authenticated principal.
        files: File service.

    Returns:
        Upload grant (key must be logged with the target entity).
    """
    key, url, expires_in = await files.create_upload_grant(
        current_user,
        filename=payload.name,
        content_type=payload.type,
    )
    return PresignUploadResponse(key=key, url=url, expires_in=expires_in)


@router.get(
    "/presigned-url",
    response_model=PresignDownloadResponse,
    summary="Request a temporary download grant",
    dependencies=[Depends(rate_limit("60/minute"))],
)
async def create_download_url(
    current_user: CurrentUser,
    files: Annotated[FileService, Depends(get_file_service)],
    key: Annotated[str, Query(min_length=1, max_length=512)],
) -> PresignDownloadResponse:
    """Issue a pre-signed GET URL after validating the caller's scope.

    Args:
        current_user: Authenticated principal.
        files: File service.
        key: Storage key being requested.

    Returns:
        Download grant.

    Raises:
        ForbiddenError: When the key is outside the caller's scope.
    """
    key_out, url, expires_in = await files.create_download_grant(current_user, key=key)
    return PresignDownloadResponse(key=key_out, url=url, expires_in=expires_in)
