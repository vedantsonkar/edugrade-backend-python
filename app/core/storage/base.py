"""Object-storage port.

The :class:`ObjectStorage` protocol decouples file handling from the backing
store (SeaweedFS via its S3 gateway today - AGENTS.md §2-O/D). All methods
are async; implementations must not block the event loop.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class PresignedUpload:
    """Direct-upload grant handed to a client.

    Attributes:
        key: Storage key the client must PUT to.
        url: Pre-signed URL to PUT the file to.
        expires_in: Seconds until the URL stops working.
    """

    key: str
    url: str
    expires_in: int


@dataclass(frozen=True)
class PresignedDownload:
    """Temporary read grant for a stored object.

    Attributes:
        key: Storage key being served.
        url: Pre-signed URL to GET the file from.
        expires_in: Seconds until the URL stops working.
    """

    key: str
    url: str
    expires_in: int


@dataclass(frozen=True)
class StoredObjectInfo:
    """Metadata returned by the object-store listing port."""

    key: str
    size: int
    last_modified: datetime | None


@runtime_checkable
class ObjectStorage(Protocol):
    """Port for object storage operations.

    Implementations MUST: treat keys as opaque strings produced by
    :mod:`app.core.storage.keys`; raise
    :class:`app.core.errors.StorageError` on backend failures; and never
    block the event loop.
    """

    async def presign_put(self, key: str, content_type: str | None) -> PresignedUpload:
        """Create a pre-signed PUT grant.

        Args:
            key: Destination storage key.
            content_type: Optional content type bound into the signature.

        Returns:
            Upload grant for the client.
        """
        ...

    async def presign_get(self, key: str) -> PresignedDownload:
        """Create a pre-signed GET grant for a stored object.

        Args:
            key: Storage key to serve.

        Returns:
            Download grant for the client.
        """
        ...

    async def get_bytes(self, key: str) -> bytes:
        """Read an object fully into memory.

        Args:
            key: Storage key to read.

        Returns:
            Object bytes.
        """
        ...

    async def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        """Store bytes at a key (server-side uploads: avatars, archives).

        Args:
            key: Destination storage key.
            data: Payload bytes.
            content_type: MIME type of the payload.
        """
        ...

    async def delete(self, key: str) -> None:
        """Delete an object; missing keys are treated as success (idempotent).

        Args:
            key: Storage key to delete.
        """
        ...

    async def list_objects(self, prefix: str) -> list[StoredObjectInfo]:
        """List object metadata beneath a scope-checked prefix."""
        ...

    async def list_keys(self, prefix: str) -> list[str]:
        """List keys under a prefix.

        Args:
            prefix: Key prefix to scan.

        Returns:
            Matching keys in lexicographic order.
        """
        ...

    async def check(self) -> None:
        """Verify backend reachability (used by the readiness probe).

        Raises:
            StorageError: If the backend is unreachable.
        """
        ...
