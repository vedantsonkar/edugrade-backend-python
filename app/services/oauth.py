"""Server-side Google OAuth login flow.

Builds the consent redirect (state carries the school subdomain, signed and
short-lived), exchanges the callback code for an id token, upserts the user,
and issues application tokens. SSO rules: the account must be active, permit
Google login, and staff accounts must use the credentials portal.
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import httpx
import jwt

from app.core.clock import Clock
from app.core.config import Settings
from app.core.errors import ExternalServiceError, NotFoundError
from app.core.ids import IdProvider
from app.core.security.tokens import TokenService
from app.domain.enums import AuthMethod, UserRole
from app.domain.users import RefreshToken, User
from app.repositories.schools import SchoolRepository
from app.repositories.users import RefreshTokenRepository, UserRepository
from app.services.auth import LoginResult

if TYPE_CHECKING:
    from app.domain.schools import School

logger = logging.getLogger(__name__)

_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"
_STATE_TTL_SECONDS = 600


@dataclass(frozen=True)
class OAuthCallbackResult:
    """Result of completing the OAuth callback.

    Attributes:
        login: Login result when the user was authenticated/created.
        error: Client-safe error message when the flow must be rejected.
    """

    login: LoginResult | None
    error: str | None = None


class GoogleOAuthService:
    """Orchestrates the authorization-code flow against Google."""

    def __init__(
        self,
        *,
        settings: Settings,
        schools: SchoolRepository,
        users: UserRepository,
        refresh_tokens: RefreshTokenRepository,
        token_service: TokenService,
        clock: Clock,
        id_provider: IdProvider,
        http_client: httpx.AsyncClient,
    ) -> None:
        """Inject collaborators.

        Args:
            settings: Application settings (client id/secret, redirect URI).
            schools: School repository (state school resolution).
            users: User repository (identity upsert).
            refresh_tokens: Refresh-token repository (persist rotation rows).
            token_service: Port for state signing and token issuance.
            clock: Time source.
            id_provider: Id provider for state nonces.
            http_client: Async HTTP client for Google endpoints.
        """
        self._settings = settings
        self._schools = schools
        self._users = users
        self._refresh_tokens = refresh_tokens
        self._tokens = token_service
        self._clock = clock
        self._ids = id_provider
        self._http = http_client

    def authorization_url(self, subdomain: str | None) -> str:
        """Build the Google consent URL.

        Args:
            subdomain: School subdomain the login page belongs to; embedded
                in the signed state so the callback can resolve tenancy.

        Returns:
            Full consent URL to redirect the browser to.

        Raises:
            ExternalServiceError: When Google credentials are not configured.
        """
        if not self._settings.google_client_id or not self._settings.google_client_secret:
            raise ExternalServiceError("Google login is not configured.")
        state = self._make_state(subdomain)
        params: dict[str, str] = {
            "client_id": self._settings.google_client_id,
            "redirect_uri": self._settings.google_oauth_redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "offline",
            "prompt": "select_account",
        }
        return f"{_AUTH_ENDPOINT}?{urlencode(params)}"

    async def handle_callback(  # noqa: PLR0911 - OAuth guard chain
        self, *, code: str | None, state: str | None
    ) -> OAuthCallbackResult:
        """Complete the OAuth flow: verify state, exchange code, upsert user.

        Args:
            code: Authorization code from Google.
            state: Signed state from the redirect.

        Returns:
            Result carrying either a :class:`LoginResult` or a client-safe
            error message.
        """
        if not code or not state:
            return OAuthCallbackResult(login=None, error="Missing OAuth parameters.")
        try:
            school = await self._resolve_school_from_state(state)
        except (jwt.InvalidTokenError, KeyError, ValueError) as exc:
            logger.info("OAuth state rejected: %s", exc)
            return OAuthCallbackResult(login=None, error="Invalid OAuth state.")

        try:
            userinfo = await self._exchange_code(code)
        except ExternalServiceError as exc:
            return OAuthCallbackResult(login=None, error=str(exc.message))

        email = str(userinfo.get("email", "")).lower()
        if not userinfo.get("email_verified", False) and not email:
            return OAuthCallbackResult(login=None, error="Google account email unavailable.")

        user = await self._users.get_by_email(email)
        if user is None:
            if school is None:
                return OAuthCallbackResult(
                    login=None, error="Sign in from your school's login page."
                )
            user = await self._create_sso_user(
                email=email,
                name=str(userinfo.get("name") or email.split("@", 1)[0]),
                school=school,
            )
        error = self._sso_rejection(user)
        if error is not None:
            return OAuthCallbackResult(login=None, error=error)

        user.avatar_url = user.avatar_url or None
        user.last_login_at = self._clock.now()
        await self._users.session.flush()

        school_final = user.school  # FK guarantees presence
        config = school_final.config
        enable_logging = bool(config.enable_logging) if config else False
        result = await self._finalize_login(user, school_final, enable_logging)
        return OAuthCallbackResult(login=result)

    async def _finalize_login(
        self, user: User, school: "School", enable_logging: bool
    ) -> LoginResult:
        """Issue tokens for an SSO-validated user via the auth service.

        Args:
            user: Validated user.
            school: User's school.
            enable_logging: Analytics flag.

        Returns:
            Login result with fresh tokens.
        """
        from uuid import UUID

        from app.core.security.principal import UserPrincipal

        principal = UserPrincipal(
            id=UUID(str(user.id)),
            email=user.email,
            name=user.name,
            role=user.role.value,
            school_id=str(school.id),
            school_name=school.name,
            school_subdomain=school.subdomain,
            enable_logging=enable_logging,
        )
        access = self._tokens.create_access_token(principal)
        refresh = self._tokens.create_refresh_token(UUID(str(user.id)))
        await self._refresh_tokens.add(
            RefreshToken(
                jti=refresh.jti, user_id=str(user.id), expires_at=refresh.expires_at
            )
        )
        return LoginResult(
            user=user,
            school=school,
            enable_logging=enable_logging,
            refresh=refresh,
            access_token=access,
        )

    def _make_state(self, subdomain: str | None) -> str:
        """Sign a short-lived OAuth state token.

        Args:
            subdomain: School subdomain or ``None``.

        Returns:
            Signed state JWT.
        """
        payload: dict[str, Any] = {
            "sub": str(self._ids.new_id()),
            "school": subdomain,
            "exp": int(self._clock.now().timestamp()) + _STATE_TTL_SECONDS,
            "nonce": self._ids.new_id().hex,
        }
        return jwt.encode(payload, self._settings.jwt_secret, algorithm="HS256")

    async def _resolve_school_from_state(self, state: str) -> "School | None":
        """Verify the state token and resolve the referenced school.

        Args:
            state: Signed state JWT.

        Returns:
            The school, or ``None`` when the state carried no subdomain.

        Raises:
            jwt.InvalidTokenError: On signature/expiry failure.
            NotFoundError: When the referenced school does not exist.
        """
        payload: dict[str, Any] = jwt.decode(
            state, self._settings.jwt_secret, algorithms=["HS256"]
        )
        subdomain = payload.get("school")
        if not subdomain:
            return None
        school = await self._schools.get_by_subdomain(str(subdomain))
        if school is None:
            raise NotFoundError("School not found.")
        return school

    async def _exchange_code(self, code: str) -> dict[str, Any]:
        """Exchange the authorization code for userinfo.

        Args:
            code: Authorization code.

        Returns:
            Google userinfo payload.

        Raises:
            ExternalServiceError: On network or credential failures.
        """
        try:
            token_response = await self._http.post(
                _TOKEN_ENDPOINT,
                data={
                    "code": code,
                    "client_id": self._settings.google_client_id,
                    "client_secret": self._settings.google_client_secret,
                    "redirect_uri": self._settings.google_oauth_redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            token_response.raise_for_status()
            access_token = str(token_response.json()["access_token"])
            userinfo_response = await self._http.get(
                _USERINFO_ENDPOINT, headers={"Authorization": f"Bearer {access_token}"}
            )
            userinfo_response.raise_for_status()
            userinfo: dict[str, Any] = userinfo_response.json()
            return userinfo
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            logger.warning("Google code exchange failed: %s", exc)
            raise ExternalServiceError("Google sign-in failed. Please try again.") from exc

    async def _create_sso_user(
        self, *, email: str, name: str, school: "School"
    ) -> User:
        """Create a new SSO user in the resolved school.

        Args:
            email: Verified Google email.
            name: Display name.
            school: Tenant school (``None`` → rejected upstream).

        Returns:
            The created user.

        Raises:
            ForbiddenError: Never (callers pre-check the school context).
        """
        return await self._users.add(
            User(
                email=email,
                name=name,
                role=UserRole.STUDENT,
                school_id=str(school.id),
                auth_method=AuthMethod.SSO_GOOGLE,
            )
        )

    @staticmethod
    def _sso_rejection(user: User) -> str | None:
        """Evaluate SSO eligibility rules.

        Args:
            user: Candidate user.

        Returns:
            Client-safe rejection reason, or ``None`` when allowed.
        """
        if not user.is_active:
            return "This account has been deactivated."
        if not user.can_use_google:
            return "This account cannot sign in with Google."
        if user.role != UserRole.STUDENT:
            return "Staff accounts must use the staff login portal."
        return None
