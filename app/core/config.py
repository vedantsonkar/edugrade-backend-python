"""Application configuration.

Defines :class:`Settings`, the single typed source of configuration, loaded
from environment variables (optionally a ``.env`` file). Read once via
:func:`get_settings` and injected downward - per AGENTS.md §2-D, services never
read environment variables or import this module's singleton themselves.
"""

from functools import lru_cache
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application settings loaded from the environment.

    Each attribute maps 1:1 to an environment variable (see ``.env.example``).
    Values are validated at startup; a malformed value aborts boot rather than
    surfacing as a runtime failure deep inside a request.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Runtime ──────────────────────────────────────────────────────────
    env: str = Field(default="development")
    frontend_url: str = Field(default="http://localhost:3000")
    backend_url: str = Field(default="http://localhost:8000")
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # ── Database ─────────────────────────────────────────────────────────
    database_url: str = Field(
        default="postgresql+asyncpg://edugrade:edugrade@localhost:5432/edugrade",
    )
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)
    db_echo: bool = Field(default=False)

    # ── Auth ─────────────────────────────────────────────────────────────
    jwt_secret: str = Field(default="change-me-to-a-long-random-string")
    access_token_expire_minutes: int = Field(default=15, ge=1)
    refresh_token_expire_days: int = Field(default=7, ge=1)
    cookie_domain: str | None = Field(default=None)
    cookie_secure: bool = Field(default=False)

    # ── Google OAuth ─────────────────────────────────────────────────────
    google_client_id: str | None = Field(default=None)
    google_client_secret: str | None = Field(default=None)
    google_oauth_redirect_uri: str = Field(
        default="http://localhost:3000/api/v1/auth/google/callback",
    )

    # ── AI grading ───────────────────────────────────────────────────────
    grading_provider: str = Field(default="gemini")
    """Which registered grading provider to use (``gemini`` | ``openai``)."""
    grading_temperature: float = Field(default=0.0, ge=0, le=2)
    """Sampling temperature for AI grading (0 = deterministic regrades)."""
    grading_seed: int | None = Field(default=None)
    """Optional sampling seed (best-effort reproducibility; OpenAI only)."""
    gemini_api_key: str | None = Field(default=None)
    grading_model: str = Field(default="gemini-2.5-flash-lite")
    openai_api_key: str | None = Field(default=None)
    openai_model: str = Field(default="gpt-4o-mini")
    ocr_provider: str = Field(default="glm")
    glm_ocr_api_key: str | None = Field(default=None)
    glm_ocr_endpoint: str = Field(
        default="https://api.z.ai/api/paas/v4/layout_parsing",
    )

    # ── Object storage (SeaweedFS S3 gateway) ────────────────────────────
    seaweed_s3_endpoint: str = Field(default="http://localhost:8333")
    seaweed_s3_public_endpoint: str | None = Field(default=None)
    seaweed_s3_access_key_id: str = Field(default="admin")
    seaweed_s3_secret_access_key: str = Field(default="seaweedfs")
    seaweed_s3_bucket: str = Field(default="edugrade-files")
    seaweed_s3_region: str = Field(default="us-east-1")
    seaweed_s3_presign_expire_seconds: int = Field(default=3600, ge=30)

    # ── Rate limiting / system ───────────────────────────────────────────
    redis_url: str = Field(default="redis://localhost:6379/0")
    cron_secret: str = Field(default="change-me-cron-secret")

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, value: object) -> object:
        """Accept CORS_ORIGINS as a JSON array or a comma-separated string.

        Args:
            value: Raw environment value (JSON list, comma string, or list).

        Returns:
            A list of origin strings.
        """
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                import json

                parsed: object = json.loads(stripped)
                return parsed
            return [origin.strip() for origin in stripped.split(",") if origin.strip()]
        return value

    @property
    def is_development(self) -> bool:
        """Whether the backend runs in development mode (enables dev bypasses)."""
        return self.env == "development"

    @property
    def cors_origin_list(self) -> list[str]:
        """Origins allowed by CORS, always including the configured frontend."""
        origins = list(self.cors_origins)
        if self.frontend_url not in origins:
            origins.append(self.frontend_url)
        return [origin for origin in origins if origin != "*"]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` singleton.

    Returns:
        Parsed and validated application settings.
    """
    return Settings()


def describe_settings(settings: Settings) -> dict[str, object]:
    """Build a redacted, loggable view of the settings.

    Secrets are replaced with fixed placeholders so configuration can be
    dumped into startup logs safely.

    Args:
        settings: Settings to describe.

    Returns:
        A JSON-serializable mapping with secret values masked.
    """
    secret_fields = (
        "jwt_secret",
        "google_client_secret",
        "gemini_api_key",
        "openai_api_key",
        "glm_ocr_api_key",
        "seaweed_s3_secret_access_key",
        "cron_secret",
    )
    data: dict[str, Any] = settings.model_dump()
    for field in secret_fields:
        if data.get(field):
            data[field] = "***"
    return data
