"""List-pagination primitives shared by every collection endpoint.

Any endpoint that can return more than a page of rows (students in a batch,
assignments, audit logs, …) MUST paginate via :class:`PageParams` and
:class:`Page` rather than returning unbounded lists (AGENTS.md requirement).
"""

import math

from fastapi import Query
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class PageParams(BaseModel):
    """Validated pagination window for a list query.

    Attributes:
        page: 1-based page index.
        page_size: Rows per page, capped at :data:`MAX_PAGE_SIZE`.
    """

    model_config = ConfigDict(frozen=True)

    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE

    @property
    def offset(self) -> int:
        """Row offset for SQL ``LIMIT``/``OFFSET``."""
        return (self.page - 1) * self.page_size


async def page_params(
    page: int = Query(default=1, ge=1, description="1-based page number"),
    page_size: int = Query(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        alias="pageSize",
        description=f"Rows per page (1-{MAX_PAGE_SIZE})",
    ),
) -> PageParams:
    """FastAPI dependency producing a :class:`PageParams` from query params.

    Args:
        page: Requested page number.
        page_size: Requested page size (aliased to ``pageSize``).

    Returns:
        Validated pagination window.
    """
    return PageParams(page=page, page_size=page_size)


class Page[ItemT](BaseModel):
    """Paginated result envelope returned by every list endpoint.

    Attributes:
        items: Rows in the requested window.
        page: 1-based page index this window represents.
        page_size: Rows per page used for this window.
        total: Total rows matching the query across all pages.
        total_pages: Total number of pages available.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    items: list[ItemT]
    page: int
    page_size: int
    total: int
    total_pages: int

    @classmethod
    def of(
        cls,
        items: list[ItemT],
        total: int,
        params: PageParams,
    ) -> Page[ItemT]:
        """Build a page envelope from raw query results.

        Args:
            items: Rows belonging to this window.
            total: Total matching rows.
            params: Pagination parameters used for the query.

        Returns:
            Fully populated page envelope.
        """
        total_pages = math.ceil(total / params.page_size) if params.page_size else 0
        return cls(
            items=items,
            page=params.page,
            page_size=params.page_size,
            total=total,
            total_pages=total_pages,
        )
