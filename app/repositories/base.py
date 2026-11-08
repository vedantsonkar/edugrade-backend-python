"""Generic repository base (Template Method for persistence).

Provides typed CRUD + pagination primitives shared by every repository.
Concrete repositories add model-specific queries; none of them make business
decisions (AGENTS.md §2-S). All rows flow as ORM entities - DTO mapping is
the service layer's job.
"""

from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.base import Base
from app.core.pagination import PageParams


class BaseRepository[ModelT: Base]:
    """Template repository for one ORM entity.

    Subclasses set :attr:`model` and receive a request-scoped session via
    constructor injection. Pagination expects statements that already carry
    filters, ordering, and eager-loading options.

    Attributes:
        model: The ORM entity class managed by this repository.
    """

    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session.

        Args:
            session: Request-scoped async session.
        """
        self.session = session

    async def get(self, entity_id: UUID) -> ModelT | None:
        """Fetch one entity by primary key.

        Args:
            entity_id: Primary key value.

        Returns:
            The entity, or ``None`` when absent.
        """
        return await self.session.get(self.model, entity_id)

    async def add(self, entity: ModelT) -> ModelT:
        """Stage a new entity and flush to assign defaults (ids, timestamps).

        Args:
            entity: Transient entity instance.

        Returns:
            The persisted instance.
        """
        self.session.add(entity)
        await self.session.flush()
        return entity

    async def delete(self, entity: ModelT) -> None:
        """Remove an entity.

        Args:
            entity: Persistent entity instance.
        """
        await self.session.delete(entity)

    async def paginate(
        self,
        stmt: Select[ModelT],
        params: PageParams,
    ) -> tuple[list[ModelT], int]:
        """Run a list statement with a count over the same filter.

        Args:
            stmt: Typed select with filters/ordering/load options; offset and
                limit are applied here.
            params: Pagination window.

        Returns:
            Tuple of (rows in window, total matching rows).
        """
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = (await self.session.execute(count_stmt)).scalar_one()
        rows = (
            (await self.session.execute(stmt.offset(params.offset).limit(params.page_size)))
            .scalars()
            .all()
        )
        return list(rows), int(total)
