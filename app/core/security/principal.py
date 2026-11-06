"""Authenticated principal value object.

The principal is the denormalized identity carried in access tokens and
resolved into dependencies. Domain enums are deliberately not referenced here
to keep ``core/`` free of upward imports (AGENTS.md §6) - the role travels as
its string value and is parsed by consumers.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class UserPrincipal:
    """Identity of the authenticated caller.

    Attributes:
        id: Caller's user id.
        email: Caller's email.
        name: Display name.
        role: Role string value (``ADMIN`` / ``TEACHER`` / ``STUDENT``).
        school_id: School the caller belongs to; ``None`` only for anonymous
            callers produced by the optional dependency.
        school_name: Denormalized school name for branding.
        school_subdomain: Denormalized school subdomain.
        enable_logging: Whether client-side analytics logging is enabled for
            the caller's school.
    """

    id: UUID
    email: str
    name: str
    role: str
    school_id: str | None = None
    school_name: str | None = None
    school_subdomain: str | None = None
    enable_logging: bool = False

    @classmethod
    def from_claims(cls, claims: dict[str, object]) -> "UserPrincipal":
        """Build a principal from verified access-token claims.

        Args:
            claims: Decoded JWT claim mapping (must contain ``sub`` and ``role``).

        Returns:
            The principal described by the claims.

        Raises:
            KeyError: If mandatory claims are missing (never surfaces to
                clients - callers treat decode failures as 401).
        """
        raw_id = claims["sub"]
        return cls(
            id=UUID(str(raw_id)),
            email=str(claims.get("email", "")),
            name=str(claims.get("name", "")),
            role=str(claims["role"]),
            school_id=str(claims["schoolId"]) if "schoolId" in claims else None,
            school_name=(
                str(claims["schoolName"]) if "schoolName" in claims else None
            ),
            school_subdomain=(
                str(claims["schoolSubdomain"]) if "schoolSubdomain" in claims else None
            ),
            enable_logging=bool(claims.get("enableLogging", False)),
        )

    def to_claims(self) -> dict[str, Any]:
        """Serialize the principal into JWT claims.

        Returns:
            Claim mapping used as the base payload of access tokens.
        """
        claims: dict[str, Any] = {
            "sub": str(self.id),
            "email": self.email,
            "name": self.name,
            "role": self.role,
        }
        if self.school_id is not None:
            claims["schoolId"] = self.school_id
            claims["schoolName"] = self.school_name
            claims["schoolSubdomain"] = self.school_subdomain
            claims["enableLogging"] = self.enable_logging
        return claims
