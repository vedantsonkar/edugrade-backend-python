"""School use cases: branding, admin-key validation, config, settings."""

import hmac
from uuid import UUID

from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.core.security.principal import UserPrincipal
from app.core.storage.base import ObjectStorage
from app.domain.schools import School, SchoolConfig
from app.repositories.schools import SchoolConfigRepository, SchoolRepository
from app.schemas.school import SchoolSettingsUpdate


class SchoolService:
    """Tenant-school workflows shared by public and admin endpoints."""

    def __init__(
        self,
        schools: SchoolRepository,
        configs: SchoolConfigRepository,
        storage: ObjectStorage,
    ) -> None:
        """Inject repositories.

        Args:
            schools: School repository.
            configs: School-config repository.
            storage: Object storage signing port.
        """
        self._schools = schools
        self._configs = configs
        self._storage = storage

    async def get_public_branding(self, subdomain: str) -> School:
        """Resolve a school for the unauthenticated login page.

        Args:
            subdomain: School subdomain.

        Returns:
            The school.

        Raises:
            NotFoundError: When no school uses the subdomain.
        """
        school = await self._schools.get_by_subdomain(subdomain)
        if school is None:
            raise NotFoundError("School not found.")
        return school

    async def public_logo(self, school: School) -> str | None:
        """Resolve the logo already bound to a publicly visible school."""
        if not school.logo_url or school.logo_url.startswith(("https://", "http://")):
            return school.logo_url
        grant = await self._storage.presign_get(school.logo_url)
        return grant.url

    async def validate_admin_key(
        self,
        *,
        admin_key: str,
        school_id: str | None,
        subdomain: str | None,
    ) -> tuple[bool, School | None]:
        """Validate a school onboarding key (constant-time comparison).

        Exactly one of ``school_id``/``subdomain`` identifies the tenant;
        when neither is given the key alone is matched platform-wide.

        Args:
            admin_key: Presented key.
            school_id: Optional school id.
            subdomain: Optional subdomain.

        Returns:
            Tuple of (valid?, matched school or ``None``).
        """
        school: School | None = None
        if subdomain:
            school = await self._schools.get_by_subdomain(subdomain)
        elif school_id:
            school = await self._schools.get(UUID(school_id))
        if school is not None:
            valid = hmac.compare_digest(school.admin_key, admin_key)
            return valid, (school if valid else None)

        candidate = await self._schools.get_by_admin_key(admin_key)
        if candidate is not None and hmac.compare_digest(candidate.admin_key, admin_key):
            return True, candidate
        return False, None

    async def get_config(self, school_id: str) -> SchoolConfig:
        """Fetch the feature config of a school.

        Args:
            school_id: Tenant.

        Returns:
            The config.

        Raises:
            NotFoundError: When no config is provisioned.
        """
        config = await self._configs.get_for_school(school_id)
        if config is None:
            raise NotFoundError("School configuration not found.")
        return config

    async def get_settings(self, school_id: str) -> School:
        """Fetch the full school for admin settings.

        Args:
            school_id: Tenant.

        Returns:
            The school (config loaded).

        Raises:
            NotFoundError: When the school does not exist.
        """
        school = await self._schools.get(UUID(school_id))
        if school is None:
            raise NotFoundError("School not found.")
        return school

    async def update_settings(self, school_id: str, updates: SchoolSettingsUpdate) -> School:
        """Apply a partial settings update to school + config.

        Args:
            school_id: Tenant (caller must be the school's admin - enforced
                at the route layer).
            updates: Patch payload.

        Returns:
            The updated school.

        Raises:
            ConflictError: When the new subdomain is already taken.
        """
        school = await self.get_settings(school_id)

        data = updates.model_dump(exclude_unset=True, by_alias=False)
        school_fields = {
            "name",
            "subdomain",
            "primary_color",
            "logo_url",
            "phone_country_code",
            "phone_number",
            "address",
            "city",
            "state",
            "country",
            "postal_code",
            "default_locale",
            "timezone",
        }
        config_fields = {
            "allow_multiple_class_teachers",
            "enable_logging",
            "enable_vision_ocr",
            "current_academic_year",
        }

        if "subdomain" in data and data["subdomain"] is not None:
            new_subdomain = str(data["subdomain"]).lower()
            if new_subdomain != school.subdomain:
                existing = await self._schools.get_by_subdomain(new_subdomain)
                if existing is not None:
                    raise ConflictError("Subdomain is already in use.")
                school.subdomain = new_subdomain

        logo = data.get("logo_url")
        if (
            isinstance(logo, str)
            and logo
            and not logo.startswith(("http://", "https://", f"uploads/{school_id}/"))
        ):
            raise ForbiddenError("Logo must belong to this school.")
        for key, value in data.items():
            if key in school_fields and (value is not None or key == "logo_url"):
                setattr(school, key, value)

        if any(key in config_fields for key in data):
            config = await self._configs.get_for_school(school_id)
            if config is None:
                raise NotFoundError("School configuration not found.")
            for key, value in data.items():
                if key in config_fields and value is not None:
                    setattr(config, key, value)

        await self._schools.session.flush()
        return school

    async def refresh_context(self, principal: UserPrincipal) -> School:
        """Reload the caller's school branding (context refresh).

        Args:
            principal: Authenticated caller.

        Returns:
            The school.

        Raises:
            ForbiddenError: When the principal carries no school.
            NotFoundError: When the school vanished.
        """
        if principal.school_id is None:
            raise ForbiddenError("Caller has no school context.")
        school = await self._schools.get(UUID(principal.school_id))
        if school is None:
            raise NotFoundError("School not found.")
        return school
