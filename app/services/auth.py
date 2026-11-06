"""Authentication use cases: credentials login, refresh rotation, logout.

Exposes :class:`AuthService` - the only place credential checking, token
issuance, and refresh-token lifecycle decisions are made. HTTP mechanics
(cookies, status codes) live in the auth routes.
"""

import logging
from dataclasses import dataclass
from uuid import UUID

import jwt

from app.core.clock import Clock
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, UnauthorizedError
from app.core.security.passwords import PasswordHasher
from app.core.security.principal import UserPrincipal
from app.core.security.tokens import (
    REFRESH_TOKEN_TYPE,
    RefreshTokenIssuance,
    TokenService,
    parse_expiry,
)
from app.domain.schools import School
from app.domain.users import RefreshToken, User
from app.repositories.schools import SchoolRepository
from app.repositories.users import RefreshTokenRepository, UserRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoginResult:
    """Outcome of a successful login or refresh.

    Attributes:
        user: Authenticated user (school eagerly loaded).
        school: User's school.
        enable_logging: Client-analytics flag from school config.
        refresh: Issued refresh token (token string, jti, expiry).
        access_token: Issued access token string.
    """

    user: User
    school: School
    enable_logging: bool
    refresh: RefreshTokenIssuance
    access_token: str

    @property
    def principal(self) -> UserPrincipal:
        """Principal describing the result's identity (for token minting)."""
        return UserPrincipal(
            id=UUID(str(self.user.id)),
            email=self.user.email,
            name=self.user.name,
            role=self.user.role.value,
            school_id=str(self.school.id),
            school_name=self.school.name,
            school_subdomain=self.school.subdomain,
            enable_logging=self.enable_logging,
        )


class AuthService:
    """Coordinates login, refresh rotation, and logout.

    Business rules:
      - accounts must be active and permit credentials login;
      - ``login_type="admin"`` rejects students (staff-only portal);
      - optional ``school_id`` must match the user's school;
      - refresh tokens are single-use: consuming one revokes it and issues a
        successor (rotation); logout revokes the presented token.
    """

    def __init__(
        self,
        users: UserRepository,
        refresh_tokens: RefreshTokenRepository,
        schools: SchoolRepository,
        hasher: PasswordHasher,
        token_service: TokenService,
        clock: Clock,
    ) -> None:
        """Inject collaborators.

        Args:
            users: User repository.
            refresh_tokens: Refresh-token repository.
            schools: School repository.
            hasher: Password hashing port.
            token_service: Token issuance/verification port.
            clock: Time source.
        """
        self._users = users
        self._refresh_tokens = refresh_tokens
        self._schools = schools
        self._hasher = hasher
        self._tokens = token_service
        self._clock = clock

    async def login(
        self,
        *,
        email: str,
        password: str,
        school_id: str | None,
        login_type: str | None,
    ) -> LoginResult:
        """Authenticate with email + password.

        Args:
            email: Account email.
            password: Plaintext password.
            school_id: Optional expected school id.
            login_type: Optional ``student``/``admin`` portal context.

        Returns:
            Login result with fresh tokens.

        Raises:
            UnauthorizedError: On unknown email, bad password, deactivated
                account, or credentials login not permitted.
            ForbiddenError: When a student attempts the admin portal.
            ConflictError: When the school context mismatches.
        """
        user = await self._users.get_by_email(email.strip())
        if user is None or not user.can_use_credentials:
            raise UnauthorizedError("Invalid email or password.")
        if not user.is_active:
            raise UnauthorizedError("This account has been deactivated.")
        if user.password_hash is None or not self._hasher.verify(
            password, user.password_hash
        ):
            raise UnauthorizedError("Invalid email or password.")

        if school_id is not None and str(user.school_id) != school_id:
            raise ConflictError("Account does not belong to the selected school.")
        if login_type == "admin" and user.role.value == "STUDENT":
            raise ForbiddenError("Students must sign in from the student portal.")

        school = user.school
        if school is None:
            raise NotFoundError("User's school could not be loaded.")
        config = school.config
        enable_logging = bool(config.enable_logging) if config else False

        user.last_login_at = self._clock.now()
        await self._users.session.flush()

        return await self._issue(user, school, enable_logging)

    async def refresh(self, refresh_token: str) -> LoginResult:
        """Rotate a refresh token and mint a new pair.

        The presented token must be valid, unrevoked, and unexpired; it is
        revoked in favor of a successor (rotation detects replay).

        Args:
            refresh_token: Refresh JWT from cookie or body.

        Returns:
            Login result with a fresh token pair.

        Raises:
            UnauthorizedError: On invalid, revoked, or expired tokens.
        """
        try:
            claims = self._tokens.decode(refresh_token, expected_type=REFRESH_TOKEN_TYPE)
        except jwt.InvalidTokenError as exc:
            raise UnauthorizedError("Invalid or expired refresh token.") from exc

        from uuid import UUID as _UUID

        jti = _UUID(str(claims["jti"]))
        row = await self._refresh_tokens.get_by_jti(jti)
        if row is None or row.revoked_at is not None:
            raise UnauthorizedError("Refresh token is no longer valid.")

        now = self._clock.now()
        if parse_expiry(claims) <= now:
            raise UnauthorizedError("Refresh token is no longer valid.")

        user = await self._users.get(UUID(str(row.user_id)))
        if user is None or not user.is_active:
            raise UnauthorizedError("Account is unavailable.")
        school = user.school
        if school is None:
            raise UnauthorizedError("Account's school is unavailable.")
        config = school.config
        enable_logging = bool(config.enable_logging) if config else False

        row.revoked_at = now
        result = await self._issue(user, school, enable_logging)
        row.replaced_by_id = result.refresh.jti
        await self._refresh_tokens.session.flush()
        return result

    async def logout(self, refresh_token: str | None) -> None:
        """Revoke the presented refresh token, if any.

        Idempotent: an absent or already-revoked token simply does nothing.

        Args:
            refresh_token: Refresh JWT from cookie or body.
        """
        if not refresh_token:
            return
        try:
            claims = self._tokens.decode(refresh_token, expected_type=REFRESH_TOKEN_TYPE)
        except jwt.InvalidTokenError:
            return
        from uuid import UUID as _UUID

        row = await self._refresh_tokens.get_by_jti(_UUID(str(claims["jti"])))
        if row is not None and row.revoked_at is None:
            row.revoked_at = self._clock.now()
            await self._refresh_tokens.session.flush()

    async def me(self, user_id: UUID) -> User:
        """Load the caller's fresh user row (role/active re-verification).

        Args:
            user_id: Caller id from the verified token.

        Returns:
            The user entity.

        Raises:
            UnauthorizedError: If the account no longer exists or is inactive.
        """
        user = await self._users.get(user_id)
        if user is None or not user.is_active:
            raise UnauthorizedError("Account is unavailable.")
        return user

    async def _issue(
        self, user: User, school: School, enable_logging: bool
    ) -> LoginResult:
        """Issue an access/refresh pair and persist the refresh row.

        Args:
            user: Authenticated user.
            school: User's school.
            enable_logging: Client-analytics flag.

        Returns:
            Complete login result.
        """
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
                jti=refresh.jti,
                user_id=str(user.id),
                expires_at=refresh.expires_at,
            )
        )
        return LoginResult(
            user=user,
            school=school,
            enable_logging=enable_logging,
            refresh=refresh,
            access_token=access,
        )
