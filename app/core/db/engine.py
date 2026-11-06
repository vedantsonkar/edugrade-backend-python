"""Async engine and session factory construction.

Pure factory functions - nothing here is imported by services. The
composition root (``app.api.v1.deps``) builds the engine once and hands
request-scoped sessions to repositories.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings


def build_engine(settings: Settings) -> AsyncEngine:
    """Create the async engine for the configured database.

    Pool sizing follows the settings; ``pool_pre_ping`` guards against
    stale connections behind poolers.

    Args:
        settings: Application settings providing the URL and pool sizes.

    Returns:
        A configured async SQLAlchemy engine.
    """
    kwargs: dict[str, object] = {"echo": settings.db_echo, "pool_pre_ping": True}
    if not settings.database_url.startswith("sqlite"):
        # Pool sizing applies to server-backed databases only; sqlite (tests,
        # quick smoke) rejects these kwargs.
        kwargs["pool_size"] = settings.db_pool_size
        kwargs["max_overflow"] = settings.db_max_overflow
    return create_async_engine(settings.database_url, **kwargs)


def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create the session factory bound to the engine.

    Args:
        engine: Engine to bind sessions to.

    Returns:
        Session factory producing expunge-on-commit sessions.
    """
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def session_scope(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Provide a transactional session, committing on success.

    Used by scripts, workers, and the request dependency. On error the
    session is rolled back and the exception propagates.

    Args:
        sessionmaker: Factory creating the session.

    Yields:
        An open async session wrapped in an implicit transaction.
    """
    async with sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
