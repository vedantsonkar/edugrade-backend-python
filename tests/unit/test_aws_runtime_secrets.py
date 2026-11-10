"""Tests for the AWS runtime secret provider."""

import json
from unittest.mock import MagicMock, patch

import pytest
from app.core.secrets.aws import load_runtime_secrets


def test_local_environment_does_not_call_aws() -> None:
    """Local development remains independent of AWS credentials."""
    with patch("app.core.secrets.aws.boto3.client") as client:
        assert load_runtime_secrets({}) == {}
    client.assert_not_called()


def test_secrets_are_loaded_and_database_password_is_url_encoded() -> None:
    """Only approved fields are injected and special passwords remain usable."""
    client = MagicMock()
    client.get_secret_value.side_effect = [
        {"SecretString": json.dumps({
            "JWT_SECRET": "jwt", "CRON_SECRET": "cron", "GEMINI_API_KEY": "key",
            "AWS_REGION": "bad",
        })},
        {"SecretString": json.dumps({"username": "admin", "password": "p@ss%word"})},
    ]
    env = {
        "AWS_RUNTIME_SECRET_ID": "app", "AWS_DATABASE_SECRET_ID": "db",
        "AWS_DATABASE_HOST": "db.local", "AWS_REGION": "ap-southeast-2",
    }
    with patch("app.core.secrets.aws.boto3.client", return_value=client):
        result = load_runtime_secrets(env)
    assert result["GEMINI_API_KEY"] == "key"
    assert "AWS_REGION" not in result
    assert result["DATABASE_URL"] == "postgresql+asyncpg://admin:p%40ss%25word@db.local:5432/edugrade?ssl=require"


def test_missing_secret_identifier_fails_closed() -> None:
    """Partial AWS configuration must never fall back to insecure defaults."""
    with pytest.raises(ValueError, match="Both AWS secret identifiers"):
        load_runtime_secrets({"AWS_RUNTIME_SECRET_ID": "app"})
