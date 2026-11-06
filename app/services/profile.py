"""Profile use cases: read/update own profile, avatar upload, password change.

Avatar uploads stream the multipart file into object storage (the legacy
backend only accepted a URL string - the capability gap closed here).
"""

from uuid import UUID

from app.core.errors import ForbiddenError, UnauthorizedError, ValidationFailedError
from app.core.security.passwords import PasswordHasher
from app.core.storage.base import ObjectStorage
from app.core.storage.keys import avatar_key
from app.domain.users import User
from app.repositories.users import UserRepository
from app.schemas.profile import ProfileUpdateRequest

MAX_AVATAR_BYTES = 5 * 1024 * 1024
"""Avatar upload cap (5 MiB)."""

ALLOWED_AVATAR_TYPES = {"image/jpeg", "image/png", "image/webp"}
"""MIME types accepted for avatars."""


class ProfileService:
    """Self-service profile workflows for any authenticated user."""

    def __init__(
        self,
        *,
        users: UserRepository,
        hasher: PasswordHasher,
        storage: ObjectStorage,
    ) -> None:
        """Inject collaborators.

        Args:
            users: User repository.
            hasher: Password hashing port.
            storage: Object storage port.
        """
        self._users = users
        self._hasher = hasher
        self._storage = storage

    async def get_profile(self, user_id: UUID) -> User:
        """Load the caller's profile.

        Args:
            user_id: Caller id.

        Returns:
            The user entity.

        Raises:
            UnauthorizedError: When the account is gone/inactive.
        """
        user = await self._users.get(user_id)
        if user is None or not user.is_active:
            raise UnauthorizedError("Account is unavailable.")
        return user

    async def update_profile(self, user_id: UUID, payload: ProfileUpdateRequest) -> User:
        """Apply a partial profile update.

        Args:
            user_id: Caller id.
            payload: Patch payload.

        Returns:
            The updated user.

        Raises:
            ConflictError-proxy (ConflictError): Propagates from the email
                uniqueness check when the new email is taken.
        """
        user = await self.get_profile(user_id)
        fields = payload.model_dump(exclude_unset=True)
        if "email" in fields and fields["email"] is not None:
            new_email = str(fields["email"]).strip().lower()
            if new_email != user.email:
                existing = await self._users.get_by_email(new_email)
                if existing is not None:
                    from app.core.errors import ConflictError

                    raise ConflictError("This email is already in use.")
                user.email = new_email
        for key, value in fields.items():
            if key == "email":
                continue
            setattr(user, key, value)
        await self._users.session.flush()
        return user

    async def upload_avatar(
        self, user_id: UUID, *, data: bytes, filename: str, content_type: str
    ) -> User:
        """Store an avatar image and point the user's ``avatar_url`` at it.

        Args:
            user_id: Caller id.
            data: Image bytes.
            filename: Client filename.
            content_type: Image MIME type.

        Returns:
            The updated user.

        Raises:
            ValidationFailedError: On unsupported type or empty payload.
            PayloadTooLargeError-proxy: Raised via ValidationFailedError when
                the payload exceeds the cap (kept explicit in the route).
        """
        if content_type not in ALLOWED_AVATAR_TYPES:
            raise ValidationFailedError("Avatar must be a JPEG, PNG, or WebP image.")
        if not data:
            raise ValidationFailedError("Avatar file is empty.")
        if len(data) > MAX_AVATAR_BYTES:
            from app.core.errors import PayloadTooLargeError

            raise PayloadTooLargeError("Avatar exceeds the 5 MiB limit.")

        user = await self.get_profile(user_id)
        key = avatar_key(str(user_id), filename)
        await self._storage.put_bytes(key, data, content_type)
        user.avatar_url = key
        await self._users.session.flush()
        return user

    async def change_password(
        self, user_id: UUID, *, current_password: str, new_password: str
    ) -> None:
        """Rotate the caller's password after verifying the current one.

        Args:
            user_id: Caller id.
            current_password: Existing password.
            new_password: Replacement.

        Raises:
            ForbiddenError: When the account cannot use credentials login.
            UnauthorizedError: When the current password is wrong.
            ValidationFailedError: When no password is set (SSO-only account).
        """
        user = await self.get_profile(user_id)
        if not user.can_use_credentials:
            raise ForbiddenError("This account signs in with Google only.")
        if user.password_hash is None:
            raise ValidationFailedError("No password is set for this account.")
        if not self._hasher.verify(current_password, user.password_hash):
            raise UnauthorizedError("Current password is incorrect.")
        user.password_hash = self._hasher.hash(new_password)
        await self._users.session.flush()
