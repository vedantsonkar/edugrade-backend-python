"""Load runtime credentials from AWS Secrets Manager at process startup."""

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import boto3

_APPLICATION_FIELDS = frozenset({
    "JWT_SECRET", "CRON_SECRET", "GEMINI_API_KEY", "OPENAI_API_KEY",
    "GLM_OCR_API_KEY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
})


def load_runtime_secrets(environment: Mapping[str, str]) -> dict[str, str]:
    """Return runtime environment overrides without writing secrets to disk.

    Args:
        environment: Non-secret configuration with secret identifiers and DB host.

    Returns:
        Environment variables derived from the configured AWS secrets.
    """
    app_secret_id = environment.get("AWS_RUNTIME_SECRET_ID")
    db_secret_id = environment.get("AWS_DATABASE_SECRET_ID")
    if not app_secret_id and not db_secret_id:
        return {}
    if not app_secret_id or not db_secret_id:
        raise ValueError("Both AWS secret identifiers must be configured")

    client = boto3.client("secretsmanager", region_name=environment.get("AWS_REGION"))

    def read_secret(secret_id: str) -> dict[str, Any]:
        """Read and validate a JSON object without logging its contents."""
        response = client.get_secret_value(SecretId=secret_id)
        value: Any = json.loads(response["SecretString"])
        if not isinstance(value, dict):
            raise ValueError("AWS secret must contain a JSON object")
        return value

    app = read_secret(app_secret_id)
    overrides = {
        key: value
        for key, value in app.items()
        if key in _APPLICATION_FIELDS and isinstance(value, str)
    }
    if not overrides.get("JWT_SECRET") or not overrides.get("CRON_SECRET"):
        raise ValueError("AWS runtime secret must include JWT_SECRET and CRON_SECRET")
    db = read_secret(db_secret_id)
    host = environment["AWS_DATABASE_HOST"]
    database = environment.get("AWS_DATABASE_NAME", "edugrade")
    username = quote(str(db["username"]), safe="")
    password = quote(str(db["password"]), safe="")
    overrides["DATABASE_URL"] = (
        f"postgresql+asyncpg://{username}:{password}@{host}:5432/{database}?ssl=require"
    )
    return overrides
