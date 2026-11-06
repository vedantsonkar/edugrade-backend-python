"""Profile routes: read/update profile, avatar upload, password change."""

from typing import Annotated

from fastapi import APIRouter, Depends, UploadFile
from uuid_utils.compat import uuid7

from app.api.v1.deps import get_profile_service
from app.core.errors import PayloadTooLargeError
from app.core.ratelimit.dependency import rate_limit
from app.core.security.guards import CurrentUser
from app.schemas.profile import (
    PasswordUpdateRequest,
    ProfileResponse,
    ProfileUpdateRequest,
)
from app.services.profile import MAX_AVATAR_BYTES, ProfileService

router = APIRouter(prefix="/profile", tags=["profile"])


@router.get("", response_model=ProfileResponse, summary="Own profile")
async def get_profile(
    current_user: CurrentUser,
    service: Annotated[ProfileService, Depends(get_profile_service)],
) -> ProfileResponse:
    """Return the caller's full profile.

    Args:
        current_user: Authenticated principal.
        service: Profile service.

    Returns:
        Profile payload.
    """
    user = await service.get_profile(current_user.id)
    return ProfileResponse.model_validate(user)


@router.patch(
    "",
    response_model=ProfileResponse,
    summary="Update own profile",
    dependencies=[Depends(rate_limit("10/minute"))],
)
async def update_profile(
    payload: ProfileUpdateRequest,
    current_user: CurrentUser,
    service: Annotated[ProfileService, Depends(get_profile_service)],
) -> ProfileResponse:
    """Apply a partial profile update.

    Args:
        payload: Patch fields.
        current_user: Authenticated principal.
        service: Profile service.

    Returns:
        Updated profile.
    """
    user = await service.update_profile(current_user.id, payload)
    return ProfileResponse.model_validate(user)


@router.post(
    "/avatar",
    response_model=ProfileResponse,
    summary="Upload avatar image",
    dependencies=[Depends(rate_limit("10/minute"))],
)
async def upload_avatar(
    avatar: UploadFile,
    current_user: CurrentUser,
    service: Annotated[ProfileService, Depends(get_profile_service)],
) -> ProfileResponse:
    """Stream an avatar image into object storage and link it.

    Args:
        avatar: Uploaded multipart file.
        current_user: Authenticated principal.
        service: Profile service.

    Returns:
        Updated profile with the new avatar key.

    Raises:
        ValidationFailedError: On unsupported/empty uploads.
        PayloadTooLargeError: Beyond the 5 MiB cap.
    """
    content_type = avatar.content_type or "application/octet-stream"
    data = await avatar.read()
    if len(data) > MAX_AVATAR_BYTES:
        raise PayloadTooLargeError("Avatar exceeds the 5 MiB limit.")
    filename = avatar.filename or f"{uuid7()}.img"
    user = await service.upload_avatar(
        current_user.id,
        data=data,
        filename=filename,
        content_type=content_type,
    )
    return ProfileResponse.model_validate(user)


@router.post(
    "/password",
    summary="Change password",
    dependencies=[Depends(rate_limit("5/minute"))],
)
async def change_password(
    payload: PasswordUpdateRequest,
    current_user: CurrentUser,
    service: Annotated[ProfileService, Depends(get_profile_service)],
) -> dict[str, str]:
    """Rotate the caller's password after verifying the current one.

    Args:
        payload: Current + new password.
        current_user: Authenticated principal.
        service: Profile service.

    Returns:
        Acknowledgement payload.

    Raises:
        UnauthorizedError: On wrong current password.
        ForbiddenError: For SSO-only accounts.
    """
    await service.change_password(
        current_user.id,
        current_password=payload.current_password,
        new_password=payload.new_password,
    )
    return {"status": "password_changed"}
