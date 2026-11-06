"""Rate-limit FastAPI dependency and response-header middleware.

Usage on a route::

    @router.post("", dependencies=[Depends(rate_limit("10/minute"))])

The dependency keys buckets by authenticated user id when available, falling
back to the client IP, records the check on ``request.state.rate_limit``, and
raises :class:`RateLimitError` (→ 429) when the bucket is exhausted.
:class:`RateLimitHeadersMiddleware` then attaches ``X-RateLimit-*`` headers.
"""

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.errors import RateLimitError
from app.core.ratelimit.base import RateLimitResult, parse_limit
from app.core.security.principal import UserPrincipal


def _client_ip(request: Request) -> str:
    """Determine the caller IP, honoring the first X-Forwarded-For hop.

    Args:
        request: Incoming request.

    Returns:
        Best-effort client IP string.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = request.client
    return client.host if client else "unknown"


def rate_limit(spec: str) -> Callable[[Request], Awaitable[None]]:
    """Build a dependency enforcing the given limit spec.

    Args:
        spec: Limit spec such as ``"30/minute"`` (see
            :func:`app.core.ratelimit.base.parse_limit`).

    Returns:
        An async FastAPI dependency evaluating the limit per request.
    """
    limit, window = parse_limit(spec)

    async def dependency(request: Request) -> None:
        """Evaluate the rate limit for this request.

        Args:
            request: Incoming request; must expose the app container on
                ``request.app.state.container``.

        Raises:
            RateLimitError: When the caller exceeded the configured limit.
        """
        container = request.app.state.container
        principal: UserPrincipal | None = getattr(request.state, "principal", None)
        identity = str(principal.id) if principal is not None else _client_ip(request)
        bucket = f"{request.url.path}:{identity}"
        result: RateLimitResult = await container.rate_limiter.check(
            bucket, limit, window
        )
        request.state.rate_limit = result
        if not result.allowed:
            raise RateLimitError(
                "Too many requests. Please slow down.",
                retry_after=result.retry_after,
            )

    return dependency


class RateLimitHeadersMiddleware(BaseHTTPMiddleware):
    """Attaches ``X-RateLimit-*`` headers when a check ran for the request."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the ASGI app.

        Args:
            app: The next ASGI application in the middleware stack.
        """
        super().__init__(app)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Copy the recorded rate-limit result onto the response headers.

        Args:
            request: Incoming request.
            call_next: Next handler in the chain.

        Returns:
            The downstream response, augmented with rate-limit headers when
            available.
        """
        response = await call_next(request)
        result: RateLimitResult | None = getattr(request.state, "rate_limit", None)
        if result is not None:
            response.headers["X-RateLimit-Limit"] = str(result.limit)
            response.headers["X-RateLimit-Remaining"] = str(result.remaining)
            response.headers["X-RateLimit-Reset"] = str(result.reset_after)
        return response
