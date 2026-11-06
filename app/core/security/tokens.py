"""Token issuance and verification port + HS256 implementation.

The :class:`TokenService` protocol abstracts JWT mechanics away from services.
:class:`Hs256TokenService` issues short-lived access tokens carrying the
denormalized :class:`UserPrincipal` claims, and opaque refresh tokens whose
``jti`` is persisted (rotation/revocation live in
``services.auth.RefreshTokenService``).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import jwt

from app.core.clock import Clock
from app.core.security.principal import UserPrincipal

ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"

ALGORITHM = "HS256"
"""Signing algorithm; HMAC-SHA256 with the configured shared secret."""


@dataclass(frozen=True)
class RefreshTokenIssuance:
    """Result of issuing a refresh token.

    Attributes:
        token: Encoded JWT string handed to the client.
        jti: Unique token id; the caller persists it for rotation/revocation.
        expires_at: Absolute expiry instant (UTC).
    """

    token: str
    jti: UUID
    expires_at: datetime


@runtime_checkable
class TokenService(Protocol):
    """Port for issuing and verifying authentication tokens.

    Implementations MUST verify signature, expiry, and token type on decode,
    raising :class:`jwt.InvalidTokenError` (or subclasses) on any failure.
    """

    def create_access_token(self, principal: UserPrincipal) -> str:
        """Issue a short-lived access token for the principal.

        Args:
            principal: Authenticated identity to embed as claims.

        Returns:
            Encoded access token.
        """
        ...

    def create_refresh_token(self, user_id: UUID) -> RefreshTokenIssuance:
        """Issue a long-lived refresh token for the user.

        Args:
            user_id: Owner of the refresh token.

        Returns:
            Issuance containing the encoded token, its ``jti`` and expiry.
        """
        ...

    def decode(self, token: str, expected_type: str) -> dict[str, Any]:
        """Decode and verify a token.

        Args:
            token: Encoded JWT.
            expected_type: Required ``typ`` claim (``access`` or ``refresh``).

        Returns:
            Verified claim mapping.

        Raises:
            jwt.InvalidTokenError: On bad signature, expiry, or type mismatch.
        """
        ...


class Hs256TokenService:
    """Default :class:`TokenService` using PyJWT with HS256 signing."""

    def __init__(
        self,
        *,
        secret: str,
        access_ttl: timedelta,
        refresh_ttl: timedelta,
        clock: Clock,
    ) -> None:
        """Configure the service.

        Args:
            secret: HMAC signing secret.
            access_ttl: Access token lifetime.
            refresh_ttl: Refresh token lifetime.
            clock: Time source for ``iat``/``exp`` claims.
        """
        self._secret = secret
        self._access_ttl = access_ttl
        self._refresh_ttl = refresh_ttl
        self._clock = clock

    def create_access_token(self, principal: UserPrincipal) -> str:
        """Issue a signed access token embedding the principal's claims.

        Args:
            principal: Identity to embed.

        Returns:
            Encoded access token valid for the configured access TTL.
        """
        now = self._clock.now()
        payload: dict[str, Any] = {
            **principal.to_claims(),
            "typ": ACCESS_TOKEN_TYPE,
            "iat": int(now.timestamp()),
            "exp": int((now + self._access_ttl).timestamp()),
        }
        return jwt.encode(payload, self._secret, algorithm=ALGORITHM)

    def create_refresh_token(self, user_id: UUID) -> RefreshTokenIssuance:
        """Issue a refresh token with a fresh ``jti``.

        Args:
            user_id: Owner of the token.

        Returns:
            Issuance data for persistence and client delivery.
        """
        now = self._clock.now()
        expires_at = now + self._refresh_ttl
        token_id = uuid_safe()
        payload: dict[str, Any] = {
            "sub": str(user_id),
            "typ": REFRESH_TOKEN_TYPE,
            "jti": str(token_id),
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
        }
        token = jwt.encode(payload, self._secret, algorithm=ALGORITHM)
        return RefreshTokenIssuance(
            token=token, jti=token_id, expires_at=expires_at
        )

    def decode(self, token: str, expected_type: str) -> dict[str, Any]:
        """Decode and verify a token's signature, expiry, and type.

        Args:
            token: Encoded JWT.
            expected_type: Required ``typ`` claim value.

        Returns:
            Verified claims.

        Raises:
            jwt.InvalidTokenError: On any verification failure.
        """
        claims: dict[str, Any] = jwt.decode(
            token,
            self._secret,
            algorithms=[ALGORITHM],
            options={"require": ["exp", "typ"]},
        )
        if claims.get("typ") != expected_type:
            raise jwt.InvalidTokenError(
                f"Expected {expected_type} token, got {claims.get('typ')!r}"
            )
        return claims


def uuid_safe() -> UUID:
    """Generate a UUIDv4 suitable for opaque token ids.

    Returns:
        Random UUID.
    """
    import uuid

    return uuid.uuid4()


def parse_expiry(claims: dict[str, Any]) -> datetime:
    """Convert a numeric ``exp`` claim into an aware UTC datetime.

    Args:
        claims: Verified claim mapping containing ``exp``.

    Returns:
        Timezone-aware expiry instant.
    """
    return datetime.fromtimestamp(int(claims["exp"]), tz=UTC)
