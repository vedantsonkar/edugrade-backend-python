"""School routes: public branding, admin-key validation, config, refresh."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.v1.deps import get_school_service
from app.core.ratelimit.dependency import rate_limit
from app.core.security.guards import CurrentUser
from app.schemas.school import (
    SchoolBrandingResponse,
    SchoolConfigResponse,
    SchoolRefreshResponse,
    ValidateAdminRequest,
    ValidateAdminResponse,
)
from app.services.schools import SchoolService

router = APIRouter(prefix="/school", tags=["school"])


@router.get(
    "/config/public",
    response_model=SchoolBrandingResponse,
    summary="Public school branding by subdomain",
)
async def public_config(
    subdomain: Annotated[str, Query(min_length=1, max_length=63)],
    service: Annotated[SchoolService, Depends(get_school_service)],
) -> SchoolBrandingResponse:
    """Fetch a school's public branding for the login page (no auth).

    Args:
        subdomain: School subdomain.
        service: School service.

    Returns:
        Branding payload.

    Raises:
        NotFoundError: When the subdomain is unknown.
    """
    school = await service.get_public_branding(subdomain)
    return SchoolBrandingResponse(
        id=str(school.id),
        name=school.name,
        subdomain=school.subdomain,
        primary_color=school.primary_color,
        logo_url=school.logo_url,
        logo_download_url=await service.public_logo(school),
    )


@router.post(
    "/validate-admin",
    response_model=ValidateAdminResponse,
    summary="Validate school admin key",
    dependencies=[Depends(rate_limit("10/minute"))],
)
async def validate_admin(
    payload: ValidateAdminRequest,
    service: Annotated[SchoolService, Depends(get_school_service)],
) -> ValidateAdminResponse:
    """Validate an onboarding admin key (public, rate-limited).

    Args:
        payload: Key plus optional school id / subdomain.
        service: School service.

    Returns:
        Validation result (never reveals whether the key exists elsewhere).
    """
    valid, school = await service.validate_admin_key(
        admin_key=payload.admin_key,
        school_id=payload.school_id,
        subdomain=payload.subdomain,
    )
    if not valid or school is None:
        return ValidateAdminResponse(valid=False)
    return ValidateAdminResponse(
        valid=True,
        school_id=str(school.id),
        school_name=school.name,
        primary_color=school.primary_color,
    )


@router.get(
    "/config",
    response_model=SchoolConfigResponse,
    summary="Caller's school feature config",
)
async def config(
    current_user: CurrentUser,
    service: Annotated[SchoolService, Depends(get_school_service)],
) -> SchoolConfigResponse:
    """Fetch the caller's school configuration (auth required).

    Args:
        current_user: Authenticated principal.
        service: School service.

    Returns:
        Config payload.

    Raises:
        ForbiddenError: When the caller has no school context.
    """
    from app.core.errors import ForbiddenError

    if current_user.school_id is None:
        raise ForbiddenError("Caller has no school context.")
    config = await service.get_config(current_user.school_id)
    return SchoolConfigResponse.model_validate(config)


@router.post(
    "/refresh",
    response_model=SchoolRefreshResponse,
    summary="Refresh school branding context",
)
async def refresh(
    current_user: CurrentUser,
    service: Annotated[SchoolService, Depends(get_school_service)],
) -> SchoolRefreshResponse:
    """Return fresh school branding after admin edits (auth required).

    Args:
        current_user: Authenticated principal.
        service: School service.

    Returns:
        Branding payload.
    """
    school = await service.refresh_context(current_user)
    return SchoolRefreshResponse(
        school_name=school.name,
        school_primary_color=school.primary_color,
        school_logo_url=school.logo_url,
        school_subdomain=school.subdomain,
    )
