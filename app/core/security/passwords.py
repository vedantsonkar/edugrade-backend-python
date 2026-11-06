"""Password hashing port and bcrypt implementation.

The only module allowed to import ``bcrypt`` (AGENTS.md §2-D). Services hash
and verify through the :class:`PasswordHasher` protocol, keeping them free of
crypto-library specifics.
"""

from typing import Protocol, runtime_checkable

import bcrypt

BCRYPT_ROUNDS = 12
"""Cost factor; balances brute-force resistance with login latency."""


@runtime_checkable
class PasswordHasher(Protocol):
    """Port for one-way password hashing.

    Implementations MUST be deterministic-comparable (same plaintext and hash
    pair → same verification result) and MUST NOT raise on malformed hashes -
    verification of an invalid hash simply returns ``False``.
    """

    def hash(self, plain: str) -> str:
        """Hash a plaintext password.

        Args:
            plain: Plaintext password.

        Returns:
            Encoded hash string safe to persist.
        """
        ...

    def verify(self, plain: str, hashed: str) -> bool:
        """Verify a plaintext password against a stored hash.

        Args:
            plain: Plaintext password to check.
            hashed: Stored hash.

        Returns:
            ``True`` if the plaintext matches the hash.
        """
        ...


class BcryptPasswordHasher:
    """Default :class:`PasswordHasher` backed by bcrypt with per-hash salting."""

    def hash(self, plain: str) -> str:
        """Hash a plaintext password with bcrypt.

        Args:
            plain: Plaintext password.

        Returns:
            Encoded bcrypt hash.
        """
        salt = bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
        return bcrypt.hashpw(plain.encode("utf-8"), salt).decode("utf-8")

    def verify(self, plain: str, hashed: str) -> bool:
        """Verify a plaintext password against a bcrypt hash.

        Args:
            plain: Plaintext password to check.
            hashed: Stored bcrypt hash.

        Returns:
            ``True`` on match; ``False`` on mismatch or malformed hash.
        """
        try:
            return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
        except (ValueError, TypeError):
            return False
