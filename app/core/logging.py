"""Logging configuration and request observability middleware.

Sets up leveled logging and a middleware that stamps every request with a
unique ``X-Request-Id`` (echoed back to the client) and emits one structured
access line per request.
"""

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

logger = logging.getLogger("edugrade")


def configure_logging(env: str) -> None:
    """Configure root logging for the application process.

    Args:
        env: Active environment name; ``development`` enables DEBUG level.
    """
    level = logging.DEBUG if env == "development" else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    logging.getLogger("uvicorn.access").handlers.clear()
    logging.getLogger("uvicorn.access").propagate = False


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Assigns a request id to every request and logs access lines.

    The generated id is stored on ``request.state.request_id``, attached to the
    response as an ``X-Request-Id`` header, and included in the access log so
    client-reported ids can be traced to server logs.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the ASGI app.

        Args:
            app: The next ASGI application in the middleware stack.
        """
        super().__init__(app)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Stamp the request id and log the completed request.

        Args:
            request: Incoming request.
            call_next: Next handler in the chain.

        Returns:
            The downstream response with ``X-Request-Id`` set.
        """
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "%s %s -> %s (%.1f ms) [%s]",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            request_id,
        )
        response.headers["X-Request-Id"] = request_id
        return response
