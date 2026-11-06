"""Application assembly.

``create_app`` builds the FastAPI app: middleware stack, exception handlers,
routers, and the DI container on ``app.state``. Workers (batch grader, audit
archiver) are wired onto ``app.state`` here too - this module and
``app.api.v1.deps`` are the only composition points in the system.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.api.v1.deps import Container
from app.api.v1.router import api_router
from app.core.config import Settings, describe_settings, get_settings
from app.core.errors import AppError, app_error_handler, unhandled_error_handler
from app.core.logging import RequestIdMiddleware, configure_logging
from app.core.ratelimit.dependency import RateLimitHeadersMiddleware
from app.workers.audit_archiver import AuditArchiver
from app.workers.batch_grader import BatchGradingProcessor

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Assemble the FastAPI application from parsed settings.

    Args:
        settings: Optional settings override; defaults to env parsing.

    Returns:
        Configured application instance.
    """
    return build_app(Container.build(settings or get_settings()))


def build_app(container: Container) -> FastAPI:
    """Assemble the FastAPI application around a prebuilt container.

    The test seam: tests construct a container with fakes and call this
    directly (AGENTS.md §2-L - the app must run unmodified against fakes).

    Args:
        container: Fully wired dependency container.

    Returns:
        Configured application instance.
    """
    configure_logging(container.settings.env)
    logger.info(
        "Starting edugrade-backend: %s",
        describe_settings(container.settings),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Manage startup/shutdown of long-lived resources.

        Args:
            app: The running application.

        Yields:
            Control for the app's lifetime, then disposes resources.
        """
        logger.info("edugrade-backend up (env=%s)", container.settings.env)
        yield
        await container.dispose()
        logger.info("edugrade-backend shut down cleanly")

    app = FastAPI(
        title="EduGrade Backend API",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    # Composition: workers + container on app state (single wiring point).
    app.state.container = container
    app.state.batch_grader = BatchGradingProcessor(
        sessionmaker=container.sessionmaker,
        storage=container.storage,
        ocr=container.ocr_registry.get(container.settings.ocr_provider)
        or next(iter(container.ocr_registry.values())),
        gemini=container.batch_llm,
        clock=container.clock,
        ids=container.ids,
    )
    app.state.audit_archiver = AuditArchiver(
        sessionmaker=container.sessionmaker,
        storage=container.storage,
        clock=container.clock,
    )

    # Middleware (order matters: outermost first).
    app.add_middleware(RateLimitHeadersMiddleware)
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=container.settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Errors.
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_error_handler)

    def _validation_handler(_: Request, exc: Exception) -> JSONResponse:
        """Render request-validation failures in the canonical envelope.

        Args:
            _: Unused request.
            exc: Validation error (must be a ``RequestValidationError``).

        Returns:
            422 response with the error envelope.
        """
        assert isinstance(exc, RequestValidationError)
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_FAILED",
                    "message": "Request validation failed.",
                    "details": {"issues": exc.errors()},
                }
            },
        )

    app.add_exception_handler(RequestValidationError, _validation_handler)

    app.include_router(api_router, prefix="/api/v1")
    return app


app: FastAPI = create_app()
