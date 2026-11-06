"""Audit/analytics use cases: event recording, listing, archive downloads."""

from uuid import UUID

from fastapi import Request

from app.core.pagination import Page, PageParams
from app.core.security.principal import UserPrincipal
from app.core.storage.base import ObjectStorage
from app.domain.audit import AuditLog
from app.repositories.audit import AuditLogRepository
from app.schemas.audit import AuditEventInput


class AuditService:
    """Writes and reads the append-only audit trail."""

    def __init__(self, logs: AuditLogRepository, storage: ObjectStorage) -> None:
        """Inject collaborators.

        Args:
            logs: Audit-log repository.
            storage: Object storage (archive downloads).
        """
        self._logs = logs
        self._storage = storage

    async def record(
        self,
        *,
        school_id: str | None,
        principal: UserPrincipal | None,
        request: Request | None,
        events: list[AuditEventInput],
    ) -> int:
        """Persist a batch of audit events attributed to the caller.

        Silent-failure philosophy is intentionally NOT used: ingestion is a
        first-class API and malformed events were already rejected by schema
        validation; DB errors surface as 500s so loss is visible.

        Args:
            school_id: Tenant from the principal (may be ``None`` for
                anonymous events from public pages).
            principal: Authenticated caller, when any.
            request: Request providing IP/user-agent provenance.
            events: Validated event payloads.

        Returns:
            Number of events persisted.
        """
        ip = request.client.host if (request and request.client) else None
        user_agent = request.headers.get("user-agent") if request else None
        for event in events:
            await self._logs.add_event(
                school_id=school_id or "unknown",
                action=event.action,
                user_id=str(principal.id) if principal else None,
                resource=event.resource,
                details=event.details,
                ip_address=ip,
                user_agent=user_agent,
            )
        return len(events)

    async def list_events(
        self,
        school_id: str,
        params: PageParams,
        *,
        action: str | None = None,
        user_id: str | None = None,
    ) -> Page[AuditLog]:
        """List a school's audit trail newest-first.

        Args:
            school_id: Tenant.
            params: Pagination window.
            action: Exact-action filter.
            user_id: Acting-user filter.

        Returns:
            Paginated logs.
        """
        rows, total = await self._logs.list_for_school(
            school_id, params, action=action, user_id=user_id
        )
        return Page.of(rows, total, params)

    async def download_archive(
        self, school_id: str, *, key: str
    ) -> tuple[str, str, int]:
        """Issue a pre-signed GET for an archived audit file.

        Args:
            school_id: Tenant (scoping re-validated against the key).
            key: Archive object key.

        Returns:
            Tuple of (key, url, expires_in).

        Raises:
            ForbiddenError: When the key does not belong to the school.
            NotFoundError: When the archive file does not exist.
        """
        from app.core.storage.keys import ensure_key_accessible

        ensure_key_accessible(key, _ScopedPrincipal(school_id))
        grant = await self._storage.presign_get(key)
        return grant.key, grant.url, grant.expires_in


class _ScopedPrincipal:
    """Minimal staff principal stand-in for key-scope validation.

    Attributes:
        id: Fixed synthetic id.
        role: ``ADMIN`` (archives are staff-only by route guard).
        school_id: School being served.
    """

    id = UUID(int=0)
    role = "ADMIN"

    def __init__(self, school_id: str) -> None:
        """Bind the tenant.

        Args:
            school_id: School the archive belongs to.
        """
        self.school_id = school_id
