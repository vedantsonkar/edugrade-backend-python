"""File-storage use cases: pre-signed upload/download grants.

Keys are produced by :mod:`app.core.storage.keys` and every client-supplied
key passes :func:`ensure_key_accessible` before a read grant is issued.
"""

from uuid import UUID

from app.core.errors import ForbiddenError, NotFoundError, ValidationFailedError
from app.core.security.principal import UserPrincipal
from app.core.storage.base import ObjectStorage
from app.core.storage.keys import ensure_key_accessible, upload_key
from app.repositories.users import UserRepository
from app.services.assignments import AssignmentService


class FileService:
    """Grants clients direct access to object storage within their scope."""

    def __init__(
        self, storage: ObjectStorage, users: UserRepository, assignments: AssignmentService
    ) -> None:
        """Inject the storage port.

        Args:
            storage: Object storage implementation.
            users: Tenant-aware avatar ownership lookup.
            assignments: Assignment visibility for snapshot reads.
        """
        self._storage = storage
        self._users = users
        self._assignments = assignments

    async def create_upload_grant(
        self, principal: UserPrincipal, *, filename: str, content_type: str | None
    ) -> tuple[str, str, int]:
        """Issue a pre-signed PUT grant scoped to the caller's school.

        Args:
            principal: Authenticated caller.
            filename: Client filename (sanitized).
            content_type: Optional MIME type bound into the signature.

        Returns:
            Tuple of (key, url, expires_in).

        Raises:
            ForbiddenError: When the caller has no school context.
            ValidationFailedError: When the filename is unusable.
        """
        if principal.school_id is None:
            raise ForbiddenError("Caller has no school context.")
        key = upload_key(principal.school_id, filename)
        grant = await self._storage.presign_put(key, content_type)
        return grant.key, grant.url, grant.expires_in

    async def create_download_grant(
        self, principal: UserPrincipal, *, key: str
    ) -> tuple[str, str, int]:
        """Issue a pre-signed GET grant after validating the caller's scope.

        Args:
            principal: Authenticated caller.
            key: Storage key being requested.

        Returns:
            Tuple of (key, url, expires_in).

        Raises:
            ValidationFailedError: When the key is malformed.
            ForbiddenError: When the key is outside the caller's scope.
        """
        ensure_key_accessible(key, principal)
        parts = key.split("/")
        try:
            if parts[0] == "avatars":
                owner = await self._users.get(UUID(parts[1]))
                if owner is None or str(owner.school_id) != principal.school_id:
                    raise NotFoundError("Avatar not found.")
            elif parts[0] == "snapshots":
                await self._assignments.get_assignment(principal, UUID(parts[1]))
        except (ValueError, IndexError) as exc:
            raise ValidationFailedError("Invalid storage key.") from exc
        grant = await self._storage.presign_get(key)
        return grant.key, grant.url, grant.expires_in

    async def readiness_probe(self) -> None:
        """Expose the storage health check for ``/ready``.

        Raises:
            StorageError: When the backend is unreachable.
        """
        await self._storage.check()
