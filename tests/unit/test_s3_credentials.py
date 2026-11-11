"""Verify the storage client selects the intended credential source."""

from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

import pytest
from app.core.config import Settings, describe_settings
from app.core.storage.s3 import SeaweedS3Storage


def test_aws_s3_uses_default_credential_chain() -> None:
    """AWS mode must not pass the local gateway's static credentials."""
    settings = Settings(aws_s3_use_instance_role=True, seaweed_s3_region="ap-southeast-2")
    with patch("app.core.storage.s3.boto3.client", return_value=MagicMock()) as client:
        SeaweedS3Storage(settings)

    client.assert_called_once()
    kwargs = client.call_args.kwargs
    assert kwargs["region_name"] == "ap-southeast-2"
    assert kwargs["endpoint_url"] == "https://s3.ap-southeast-2.amazonaws.com"
    assert "aws_access_key_id" not in kwargs
    assert "aws_secret_access_key" not in kwargs


def test_local_storage_keeps_gateway_credentials() -> None:
    """The default continues to use the configured S3-compatible gateway."""
    settings = Settings(aws_s3_use_instance_role=False)
    with patch("app.core.storage.s3.boto3.client", return_value=MagicMock()) as client:
        SeaweedS3Storage(settings)

    assert client.call_count == 2
    assert client.call_args_list[0].kwargs["endpoint_url"] == settings.seaweed_s3_endpoint
    assert client.call_args_list[0].kwargs["aws_access_key_id"] == settings.seaweed_s3_access_key_id


@pytest.mark.asyncio
async def test_aws_presigned_urls_use_regional_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Uploads and downloads must not redirect from the global S3 endpoint."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret-key")
    settings = Settings(
        aws_s3_use_instance_role=True,
        seaweed_s3_region="ap-southeast-2",
        seaweed_s3_bucket="test-upload-bucket",
    )
    storage = SeaweedS3Storage(settings)

    upload = await storage.presign_put("uploads/test.pdf", "application/pdf")
    download = await storage.presign_get("uploads/test.pdf")

    assert urlsplit(upload.url).hostname == "test-upload-bucket.s3.ap-southeast-2.amazonaws.com"
    assert urlsplit(download.url).hostname == "test-upload-bucket.s3.ap-southeast-2.amazonaws.com"


def test_database_url_is_redacted_from_startup_settings() -> None:
    """The connection string can contain a password and must not enter logs."""
    settings = Settings(database_url="postgresql+asyncpg://user:password@example/db")
    assert describe_settings(settings)["database_url"] == "***"
