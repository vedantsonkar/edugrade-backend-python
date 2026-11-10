"""Authentication routes: login, refresh rotation, logout, session, OAuth."""

import hmac
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse

from app.api.v1.deps import Container, ContainerDep, get_auth_service, get_google_oauth_service
from app.core.errors import UnauthorizedError
from app.core.ratelimit.dependency import rate_limit
from app.core.security.guards import ACCESS_TOKEN_COOKIE, CurrentUser
from app.schemas.auth import (
    AuthSchoolResponse,
    AuthUserResponse,
    LoginRequest,
    LoginResponse,
    MeResponse,
    RefreshRequest,
    TokenPair,
)
from app.services.auth import AuthService, LoginResult
from app.services.oauth import GoogleOAuthService

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "refresh_token"
"""Cookie name for the rotating refresh token."""


def _cookie_params(container: Container) -> dict[str, Any]:
    """Build shared cookie attributes from settings.

    Args:
        container: App container.

    Returns:
        Keyword arguments for ``set_cookie``/``delete_cookie``.
    """
    settings = container.settings
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": settings.cookie_secure,
        "domain": settings.cookie_domain or None,
    }


def _set_cookies(
    response: Response, container: Container, access_token: str, refresh_token: str
) -> None:
    """Attach auth cookies to a response.

    Args:
        response: Response to mutate.
        container: App container (cookie flags).
        access_token: Access JWT.
        refresh_token: Refresh JWT.
    """
    settings = container.settings
    params = _cookie_params(container)
    response.set_cookie(
        ACCESS_TOKEN_COOKIE,
        access_token,
        max_age=settings.access_token_expire_minutes * 60,
        **params,
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        max_age=settings.refresh_token_expire_days * 86400,
        **params,
    )


def _clear_cookies(response: Response, container: Container) -> None:
    """Remove auth cookies from a response.

    Args:
        response: Response to mutate.
        container: App container (cookie flags).
    """
    for name in (ACCESS_TOKEN_COOKIE, REFRESH_COOKIE):
        response.delete_cookie(name, **_cookie_params(container))


def _to_login_response(
    result: LoginResult, expires_in: int, cookie_only: bool = False
) -> LoginResponse:
    """Map a service result onto the wire schema.

    Args:
        result: Login result.
        expires_in: Access token lifetime in seconds.
        cookie_only: Suppress explicit tokens for browser gateway requests.

    Returns:
        Response schema.
    """
    return LoginResponse(
        user=AuthUserResponse(
            id=str(result.user.id),
            email=result.user.email,
            name=result.user.name,
            role=result.user.role.value,
            avatar_url=result.user.avatar_url,
        ),
        school=AuthSchoolResponse(
            id=str(result.school.id),
            name=result.school.name,
            primary_color=result.school.primary_color,
            logo_url=result.school.logo_url,
            school_key=result.school.school_key,
        ),
        enable_logging=result.enable_logging,
        tokens=TokenPair(
            access_token=result.access_token,
            refresh_token=result.refresh.token,
            expires_in=expires_in,
        )
        if not cookie_only
        else None,
    )


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="Credentials login",
    dependencies=[Depends(rate_limit("10/minute"))],
)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    container: Container = ContainerDep,
) -> LoginResponse:
    """Authenticate with email + password and issue tokens/cookies.

    Args:
        payload: Login credentials and optional school/portal context.
        request: Browser gateway indicator.
        response: Outgoing response (cookies attached).
        auth: Auth service.
        container: App container.

    Returns:
        Session payload with tokens.
    """
    result = await auth.login(
        email=payload.email,
        password=payload.password,
        school_id=payload.school_id,
        login_type=payload.login_type,
        admin_key=payload.admin_key,
    )
    _set_cookies(response, container, result.access_token, result.refresh.token)
    return _to_login_response(
        result,
        container.settings.access_token_expire_minutes * 60,
        cookie_only=request.headers.get("x-edugrade-browser") == "1",
    )


@router.post(
    "/refresh",
    response_model=LoginResponse,
    summary="Rotate refresh token",
)
async def refresh(
    request: Request,
    response: Response,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    container: Container = ContainerDep,
    payload: RefreshRequest | None = None,
) -> LoginResponse:
    """Rotate the refresh token (cookie or body) and mint a fresh pair.

    Args:
        request: Incoming request (refresh cookie).
        response: Outgoing response (cookies re-attached).
        auth: Auth service.
        container: App container.
        payload: Optional explicit refresh token.

    Returns:
        Session payload with tokens.

    Raises:
        UnauthorizedError: On invalid/revoked/expired refresh tokens.
    """
    token = (payload.refresh_token if payload else None) or request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise UnauthorizedError("No refresh token provided.")
    result = await auth.refresh(token)
    _set_cookies(response, container, result.access_token, result.refresh.token)
    return _to_login_response(
        result,
        container.settings.access_token_expire_minutes * 60,
        cookie_only=request.headers.get("x-edugrade-browser") == "1",
    )


@router.post("/logout", summary="Logout")
async def logout(
    request: Request,
    response: Response,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    container: Container = ContainerDep,
    payload: RefreshRequest | None = None,
) -> dict[str, str]:
    """Revoke the presented refresh token and clear cookies.

    Idempotent: missing/revoked tokens simply clear the cookies.

    Args:
        request: Incoming request (refresh cookie).
        response: Outgoing response.
        auth: Auth service.
        container: App container.
        payload: Optional explicit refresh token.

    Returns:
        Acknowledgement payload.
    """
    token = (payload.refresh_token if payload else None) or request.cookies.get(REFRESH_COOKIE)
    await auth.logout(token)
    _clear_cookies(response, container)
    return {"status": "logged_out"}


@router.get("/me", response_model=MeResponse, summary="Current session")
async def me(
    current_user: CurrentUser,
    auth: Annotated[AuthService, Depends(get_auth_service)],
) -> MeResponse:
    """Return the caller's fresh session snapshot.

    Args:
        current_user: Authenticated principal.
        auth: Auth service (fresh DB re-verification).

    Returns:
        Session payload.

    Raises:
        UnauthorizedError: When the account is gone or deactivated.
    """
    user = await auth.me(current_user.id)
    school = user.school
    config = school.config if school else None
    return MeResponse(
        id=str(user.id),
        email=user.email,
        name=user.name,
        role=user.role.value,
        avatar_url=user.avatar_url,
        school_id=str(user.school_id),
        school_name=school.name if school else None,
        school_primary_color=school.primary_color if school else None,
        school_logo_url=school.logo_url if school else None,
        school_key=school.school_key if school else None,
        enable_logging=bool(config.enable_logging) if config else False,
    )


@router.get("/google", summary="Google OAuth consent redirect")
async def google_start(
    request: Request,
    oauth: Annotated[GoogleOAuthService, Depends(get_google_oauth_service)],
    container: Container = ContainerDep,
) -> RedirectResponse:
    """Redirect the browser to Google's consent screen.

    Args:
        request: Incoming request (optional ``school`` query param carries
            the login page's school key into the signed state).
        oauth: OAuth service.
        container: Cookie configuration.

    Returns:
        Redirect response.
    """
    school_key = request.query_params.get("school")
    url = oauth.authorization_url(school_key)
    from urllib.parse import parse_qs, urlparse

    state = parse_qs(urlparse(url).query)["state"][0]
    response = RedirectResponse(url)
    response.set_cookie("oauth_state", state, max_age=600, **_cookie_params(container))
    return response


@router.get("/google/callback", summary="Google OAuth callback")
async def google_callback(
    request: Request,
    oauth: Annotated[GoogleOAuthService, Depends(get_google_oauth_service)],
    container: Container = ContainerDep,
) -> RedirectResponse:
    """Complete the OAuth flow and redirect back to the frontend.

    Args:
        request: Incoming request.
        oauth: OAuth service.
        container: App container.

    Returns:
        Redirect response to the frontend (``/auth/error`` carries the
        failure message; success sets cookies).
    """
    state = request.query_params.get("state", "")
    expected = request.cookies.get("oauth_state", "")
    if not state or not expected or not hmac.compare_digest(state, expected):
        return RedirectResponse(
            f"{container.settings.frontend_url.rstrip('/')}/auth/error?message=Invalid%20OAuth%20state"
        )
    result = await oauth.handle_callback(
        code=request.query_params.get("code"),
        state=request.query_params.get("state"),
    )
    frontend = container.settings.frontend_url.rstrip("/")
    if result.error or result.login is None:
        message = result.error or "Google sign-in failed."
        from urllib.parse import quote

        return RedirectResponse(f"{frontend}/auth/error?message={quote(message)}")
    response = RedirectResponse(f"{frontend}/auth/success")
    _set_cookies(response, container, result.login.access_token, result.login.refresh.token)
    return response
