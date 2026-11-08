"""SeaweedFS object storage implementation (S3-compatible gateway).

Uses boto3 against the SeaweedFS S3 gateway endpoint. boto3 is synchronous,
so every call is wrapped in ``asyncio.to_thread`` to keep the event loop free
(AGENTS.md §4 rule 9). This is the ONLY module that imports boto3.
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from types_boto3_s3 import S3Client

from app.core.config import Settings
from app.core.errors import AppError, NotFoundError, StorageError
from app.core.storage.base import PresignedDownload, PresignedUpload, StoredObjectInfo

logger = logging.getLogger(__name__)


class SeaweedS3Storage:
    """Default :class:`ObjectStorage` implementation backed by SeaweedFS S3.

    Pre-signed URLs are computed client-side (SigV4) so generation is cheap;
    data operations run on the boto3 client inside worker threads.
    """

    def __init__(self, settings: Settings) -> None:
        """Create the storage adapter.

        Args:
            settings: Settings providing endpoint, credentials, bucket, and
                pre-sign expiry.
        """
        self._bucket = settings.seaweed_s3_bucket
        self._presign_expire = settings.seaweed_s3_presign_expire_seconds
        from botocore.config import Config

        self._client: S3Client = boto3.client(
            "s3",
            endpoint_url=settings.seaweed_s3_endpoint,
            aws_access_key_id=settings.seaweed_s3_access_key_id,
            aws_secret_access_key=settings.seaweed_s3_secret_access_key,
            region_name=settings.seaweed_s3_region,
            config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1}),
        )

        # Sign against the browser-facing origin without rewriting signed URLs.
        self._signer: S3Client = boto3.client(
            "s3",
            endpoint_url=settings.seaweed_s3_public_endpoint or settings.seaweed_s3_endpoint,
            aws_access_key_id=settings.seaweed_s3_access_key_id,
            aws_secret_access_key=settings.seaweed_s3_secret_access_key,
            region_name=settings.seaweed_s3_region,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    def _translate(self, key: str, exc: Exception) -> AppError:
        """Map a boto3 failure onto the storage error hierarchy.

        Args:
            key: Key involved in the failed operation.
            exc: Original boto3 exception.

        Returns:
            A :class:`StorageError` (or :class:`NotFoundError` for 404s).
        """
        if isinstance(exc, ClientError):
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return NotFoundError(f"File not found: {key}")
        logger.warning("Storage operation failed for key %s: %s", key, exc)
        return StorageError(f"Storage operation failed for key: {key}")

    async def presign_put(self, key: str, content_type: str | None) -> PresignedUpload:
        """Create a pre-signed PUT grant for direct client uploads.

        Args:
            key: Destination storage key.
            content_type: Optional content type bound into the signature.

        Returns:
            Upload grant for the client.

        Raises:
            StorageError: If URL generation fails.
        """
        params: dict[str, Any] = {"Bucket": self._bucket, "Key": key}
        if content_type:
            params["ContentType"] = content_type
        try:
            url: str = await asyncio.to_thread(
                self._signer.generate_presigned_url,
                "put_object",
                Params=params,
                ExpiresIn=self._presign_expire,
            )
        except (ClientError, BotoCoreError) as exc:
            raise self._translate(key, exc) from exc
        return PresignedUpload(key=key, url=url, expires_in=self._presign_expire)

    async def presign_get(self, key: str) -> PresignedDownload:
        """Create a pre-signed GET grant for a stored object.

        Args:
            key: Storage key to serve.

        Returns:
            Download grant for the client.

        Raises:
            StorageError: If URL generation fails.
        """
        try:
            url: str = await asyncio.to_thread(
                self._signer.generate_presigned_url,
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=self._presign_expire,
            )
        except (ClientError, BotoCoreError) as exc:
            raise self._translate(key, exc) from exc
        return PresignedDownload(key=key, url=url, expires_in=self._presign_expire)

    async def get_bytes(self, key: str) -> bytes:
        """Read an object fully into memory.

        Args:
            key: Storage key to read.

        Returns:
            Object bytes.

        Raises:
            NotFoundError: If the key does not exist.
            StorageError: On any other backend failure.
        """

        def _read() -> bytes:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            body = response["Body"]
            return bytes(body.read())

        try:
            return await asyncio.to_thread(_read)
        except (ClientError, BotoCoreError) as exc:
            raise self._translate(key, exc) from exc

    async def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        """Store bytes at a key (server-side uploads).

        Args:
            key: Destination storage key.
            data: Payload bytes.
            content_type: MIME type of the payload.

        Raises:
            StorageError: On backend failure.
        """
        try:
            await asyncio.to_thread(
                self._client.put_object,
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
        except (ClientError, BotoCoreError) as exc:
            raise self._translate(key, exc) from exc

    async def delete(self, key: str) -> None:
        """Delete an object; missing keys are treated as success.

        Args:
            key: Storage key to delete.

        Raises:
            StorageError: On backend failure.
        """
        try:
            await asyncio.to_thread(self._client.delete_object, Bucket=self._bucket, Key=key)
        except (ClientError, BotoCoreError) as exc:
            raise self._translate(key, exc) from exc

    async def list_objects(self, prefix: str) -> list[StoredObjectInfo]:
        """List complete object metadata across all storage pages."""

        def collect() -> list[StoredObjectInfo]:
            rows: list[StoredObjectInfo] = []
            for page in self._client.get_paginator("list_objects_v2").paginate(
                Bucket=self._bucket, Prefix=prefix
            ):
                rows.extend(
                    StoredObjectInfo(
                        key=entry["Key"],
                        size=entry.get("Size", 0),
                        last_modified=entry.get("LastModified"),
                    )
                    for entry in page.get("Contents", [])
                )
            return rows

        try:
            return await asyncio.to_thread(collect)
        except (ClientError, BotoCoreError) as exc:
            raise self._translate(prefix, exc) from exc

    async def list_keys(self, prefix: str) -> list[str]:
        """List keys under a prefix.

        Args:
            prefix: Key prefix to scan.

        Returns:
            Matching keys in lexicographic order.

        Raises:
            StorageError: On backend failure.
        """

        def _list() -> list[str]:
            paginator = self._client.get_paginator("list_objects_v2")
            keys: list[str] = []
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                for entry in page.get("Contents", []):
                    keys.append(str(entry["Key"]))
            return sorted(keys)

        try:
            return await asyncio.to_thread(_list)
        except (ClientError, BotoCoreError) as exc:
            raise self._translate(prefix, exc) from exc

    async def initialize_bucket(self) -> None:
        """Create the development bucket; gateway flags own CORS on SeaweedFS 3.80."""

        def initialize() -> None:
            try:
                self._client.head_bucket(Bucket=self._bucket)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code not in {"404", "NoSuchBucket", "NotFound"}:
                    raise
                self._client.create_bucket(Bucket=self._bucket)

        await asyncio.to_thread(initialize)

    async def check(self) -> None:
        """Verify backend reachability for the readiness probe.

        Raises:
            StorageError: If the bucket is not accessible.
        """
        try:
            await asyncio.to_thread(self._client.head_bucket, Bucket=self._bucket)
        except (ClientError, BotoCoreError) as exc:
            raise StorageError("Object storage is unreachable.") from exc
