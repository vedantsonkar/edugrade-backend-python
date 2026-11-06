"""Health and readiness probes."""

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request

from app.api.v1.deps import Container, ContainerDep
from app.core.db.base import utc_now

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness probe")
async def health(request: Request) -> dict[str, Any]:
    """Report service liveness and basic metadata.

    Args:
        request: Incoming request (for app state).

    Returns:
        Status payload.
    """
    container: Container = request.app.state.container
    return {
        "status": "ok",
        "env": container.settings.env,
        "service": "edugrade-backend",
        "time": utc_now().isoformat(),
    }


@router.get("/ready", summary="Readiness probe")
async def ready(container: Container = ContainerDep) -> dict[str, Any]:
    """Verify downstream dependencies (database, redis, object storage).

    Args:
        container: App container.

    Returns:
        Per-dependency status payload (200 when all green).
    """
    checks: dict[str, str] = {}

    async def _check(name: str, probe: Callable[[], Awaitable[None]] | None) -> None:
        """Run one readiness probe, capturing failures.

        Args:
            name: Dependency name.
            probe: Awaitable returning on success.
        """
        from sqlalchemy import text

        try:
            if name == "database":
                async with container.sessionmaker() as session:
                    await session.execute(text("SELECT 1"))
            elif probe is not None:
                await probe()
            checks[name] = "ok"
        except Exception as exc:
            checks[name] = f"error: {exc.__class__.__name__}"

    async def _redis() -> None:
        """Ping redis when configured.

        Raises:
            RuntimeError: When redis is not configured.
        """
        if container.redis is None:
            raise RuntimeError("redis not configured")
        await container.redis.ping()

    await _check("database", None)
    await _check("redis", _redis)
    await _check("storage", container.storage.check)
    ok = all(value == "ok" for value in checks.values())
    return {"status": "ready" if ok else "degraded", "checks": checks}
