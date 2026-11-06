"""Authentication and authorization dependencies (route guards).

Resolves the caller's identity from (in order): a development-only
``X-User-Session`` header, an ``Authorization: Bearer`` header, or the
``access_token`` cookie. Role guards build on the base principal dependency.

These functions are pure FastAPI dependencies and are the ONLY place HTTP
authentication mechanics (headers, cookies) are inspected.
"""

import json
import logging
from typing import Annotated

import jwt
from fastapi import Depends, Request
from uuid_utils.compat import uuid7

from app.core.errors import ForbiddenError, UnauthorizedError
from app.core.security.principal import UserPrincipal
from app.domain.enums import UserRole

logger = logging.getLogger(__name__)

BEARER_PREFIX = "bearer "
ACCESS_TOKEN_COOKIE = "access_token"
DEV_SESSION_HEADER = "x-user-session"


def _resolve_token(request: Request) -> str:
    """Extract the access token from the request.

    Precedence: Bearer header, then ``access_token`` cookie, then the
    development-only ``X-User-Session`` JSON header.

    Args:
        request: Incoming request.

    Returns:
        Raw token string.

    Raises:
        UnauthorizedError: If no credential is present.
    """
    container = request.app.state.container
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith(BEARER_PREFIX):
        return authorization[len(BEARER_PREFIX):].strip()

    cookie_token = request.cookies.get(ACCESS_TOKEN_COOKIE)
    if cookie_token:
        return cookie_token

    if container.settings.is_development:
        dev_header = request.headers.get(DEV_SESSION_HEADER)
        if dev_header:
            return f"dev:{dev_header}"

    raise UnauthorizedError("Authentication credentials were not provided.")


async def get_current_user(request: Request) -> UserPrincipal:
    """Resolve the authenticated principal or fail with 401.

    Resolution order (see :func:`_resolve_token`): Bearer header →
    ``access_token`` cookie → dev-only ``X-User-Session`` JSON header. The dev
    bypass is active only when ``ENV=development`` and never in staging/prod.

    Args:
        request: Incoming request.

    Returns:
        The authenticated principal.

    Raises:
        UnauthorizedError: If credentials are missing, malformed, or invalid.
    """
    container = request.app.state.container
    token = _resolve_token(request)

    if token.startswith("dev:"):
        try:
            session_data: object = json.loads(token[len("dev:"):])
        except json.JSONDecodeError as exc:
            raise UnauthorizedError("Malformed X-User-Session header.") from exc
        if not isinstance(session_data, dict):
            raise UnauthorizedError("Malformed X-User-Session header.")
        session_data["sub"] = session_data.get("id", str(uuid7()))
        return UserPrincipal.from_claims(session_data)

    try:
        claims = container.token_service.decode(token, expected_type="access")
    except jwt.InvalidTokenError as exc:
        raise UnauthorizedError("Invalid or expired session token.") from exc
    principal = UserPrincipal.from_claims(claims)
    request.state.principal = principal
    return principal


async def get_optional_user(request: Request) -> UserPrincipal | None:
    """Resolve the principal if credentials are present; ``None`` otherwise.

    Used for rate-limit keying and public endpoints that personalize when
    possible.

    Args:
        request: Incoming request.

    Returns:
        Principal, or ``None`` when the caller is anonymous.
    """
    try:
        return await get_current_user(request)
    except UnauthorizedError:
        return None


async def require_admin(
    current_user: Annotated[UserPrincipal, Depends(get_current_user)],
) -> UserPrincipal:
    """Guard allowing only school administrators.

    Args:
        current_user: Authenticated principal.

    Returns:
        The principal, guaranteed to be an admin.

    Raises:
        ForbiddenError: If the caller's role is not ``ADMIN``.
    """
    if current_user.role != UserRole.ADMIN.value:
        raise ForbiddenError("Administrator role required.")
    return current_user


async def require_teacher_or_admin(
    current_user: Annotated[UserPrincipal, Depends(get_current_user)],
) -> UserPrincipal:
    """Guard allowing staff (teachers or admins).

    Args:
        current_user: Authenticated principal.

    Returns:
        The principal, guaranteed to be staff.

    Raises:
        ForbiddenError: If the caller is a student.
    """
    if current_user.role not in (UserRole.TEACHER.value, UserRole.ADMIN.value):
        raise ForbiddenError("Teacher or administrator role required.")
    return current_user


async def require_student(
    current_user: Annotated[UserPrincipal, Depends(get_current_user)],
) -> UserPrincipal:
    """Guard allowing only students.

    Args:
        current_user: Authenticated principal.

    Returns:
        The principal, guaranteed to be a student.

    Raises:
        ForbiddenError: If the caller's role is not ``STUDENT``.
    """
    if current_user.role != UserRole.STUDENT.value:
        raise ForbiddenError("Student role required.")
    return current_user


CurrentUser = Annotated[UserPrincipal, Depends(get_current_user)]
"""Annotated alias for the authenticated-principal dependency."""

OptionalUser = Annotated[UserPrincipal | None, Depends(get_optional_user)]
"""Annotated alias for the optional-principal dependency."""

AdminUser = Annotated[UserPrincipal, Depends(require_admin)]
"""Annotated alias for the admin-only dependency."""

StaffUser = Annotated[UserPrincipal, Depends(require_teacher_or_admin)]
"""Annotated alias for the teacher-or-admin dependency."""

StudentUser = Annotated[UserPrincipal, Depends(require_student)]
"""Annotated alias for the student-only dependency."""
