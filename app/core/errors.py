"""Application error hierarchy and wire-format conversion.

Every expected failure in the system is an :class:`AppError` subclass raised
from services/repositories. Route handlers never construct error responses
manually - the global handlers registered in ``app.main`` translate these into
the canonical ``{"error": {"code", "message", "details"}}`` JSON envelope.
"""


from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.status import HTTP_413_CONTENT_TOO_LARGE as HTTP_413
from starlette.status import HTTP_422_UNPROCESSABLE_CONTENT as HTTP_422


class AppError(Exception):
    """Base class for all expected application errors.

    Attributes:
        message: Human-readable, client-safe explanation.
        code: Stable machine-readable identifier (e.g. ``AUTH_INVALID_CREDENTIALS``).
        status_code: HTTP status the error maps to.
        details: Optional structured context rendered into the response body.
    """

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "INTERNAL_ERROR"

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        """Initialize the error.

        Args:
            message: Client-safe error explanation.
            details: Optional structured context included in the response body.
        """
        super().__init__(message)
        self.message = message
        self.details: dict[str, object] = details or {}

    def to_payload(self) -> dict[str, object]:
        """Serialize the error into the canonical JSON error envelope.

        Returns:
            A mapping with ``code``, ``message`` and optional ``details``.
        """
        payload: dict[str, object] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return {"error": payload}


class NotFoundError(AppError):
    """Requested entity does not exist (or is invisible to the caller)."""

    status_code = status.HTTP_404_NOT_FOUND
    code = "NOT_FOUND"


class UnauthorizedError(AppError):
    """Authentication is missing, invalid, or expired."""

    status_code = status.HTTP_401_UNAUTHORIZED
    code = "UNAUTHORIZED"


class ForbiddenError(AppError):
    """Authenticated caller lacks the required role or ownership."""

    status_code = status.HTTP_403_FORBIDDEN
    code = "FORBIDDEN"


class ConflictError(AppError):
    """Request violates a uniqueness constraint or state machine transition."""

    status_code = status.HTTP_409_CONFLICT
    code = "CONFLICT"


class ValidationFailedError(AppError):
    """Domain-level validation failed (beyond Pydantic request parsing)."""

    status_code = HTTP_422
    code = "VALIDATION_FAILED"


class RateLimitError(AppError):
    """Caller exceeded a rate limit.

    Attributes:
        retry_after: Seconds until the caller may retry.
    """

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "RATE_LIMITED"

    def __init__(self, message: str, *, retry_after: int) -> None:
        """Initialize the rate limit error.

        Args:
            message: Client-safe explanation.
            retry_after: Seconds the caller should wait before retrying.
        """
        super().__init__(message)
        self.retry_after = retry_after


class PayloadTooLargeError(AppError):
    """Uploaded payload exceeds the configured size cap."""

    status_code = HTTP_413
    code = "PAYLOAD_TOO_LARGE"


class StorageError(AppError):
    """Object storage (SeaweedFS) operation failed."""

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "STORAGE_ERROR"


class ExternalServiceError(AppError):
    """A third-party dependency (AI provider, OCR, OAuth) failed."""

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "EXTERNAL_SERVICE_ERROR"


async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    """Render any :class:`AppError` into the canonical error envelope.

    Args:
        _: Unused request.
        exc: The raised application error.

    Returns:
        JSON response with the error payload and the error's status code.
    """
    headers: dict[str, str] | None = None
    if isinstance(exc, RateLimitError):
        headers = {"Retry-After": str(exc.retry_after)}
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_payload(),
        headers=headers,
    )


async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler for unexpected exceptions.

    Logs the full traceback server-side and returns a generic 500 so internal
    details never leak to clients.

    Args:
        _: Unused request.
        exc: The unhandled exception.

    Returns:
        Generic 500 JSON response.
    """
    import logging

    logging.getLogger(__name__).exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": {"code": "INTERNAL_ERROR", "message": "Something went wrong."}},
    )
