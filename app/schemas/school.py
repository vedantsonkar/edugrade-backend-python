"""School wire schemas: branding, validation, config, settings."""

from datetime import datetime

from pydantic import Field

from app.schemas.common import BaseRequest, BaseSchema, IdStr


class SchoolBrandingResponse(BaseSchema):
    """Public school branding for the login page (no auth).

    Attributes:
        id: School id.
        name: Display name.
        subdomain: URL subdomain.
        primary_color: Brand hex color.
        logo_url: Logo storage key, if any.
    """

    id: IdStr
    name: str
    subdomain: str
    primary_color: str
    logo_url: str | None = None
    logo_download_url: str | None = None


class ValidateAdminRequest(BaseRequest):
    """Admin-key validation payload (used before onboarding flows).

    Attributes:
        admin_key: The school's onboarding secret.
        school_id: School to validate against (either this or subdomain).
        subdomain: Subdomain to resolve the school from.
    """

    admin_key: str = Field(min_length=1)
    school_id: IdStr | None = None
    subdomain: str | None = None


class ValidateAdminResponse(BaseSchema):
    """Result of admin-key validation.

    Attributes:
        valid: Whether the key matched.
        school_id: Matched school id, when valid.
        school_name: Matched school name, when valid.
        primary_color: Brand color, when valid.
    """

    valid: bool
    school_id: IdStr | None = None
    school_name: str | None = None
    primary_color: str | None = None


class SchoolConfigResponse(BaseSchema):
    """Feature configuration of the caller's school.

    Attributes:
        allow_multiple_class_teachers: Multi-class-teacher assignment flag.
        enable_logging: Client analytics flag.
        enable_vision_ocr: AI-OCR-over-attachments flag.
        current_academic_year: Academic year label.
    """

    allow_multiple_class_teachers: bool
    enable_logging: bool
    enable_vision_ocr: bool
    current_academic_year: str


class SchoolRefreshResponse(BaseSchema):
    """Fresh school branding for context refresh after admin edits.

    Attributes:
        school_name: Display name.
        school_primary_color: Brand color.
        school_logo_url: Logo storage key.
        school_subdomain: Subdomain.
    """

    school_name: str
    school_primary_color: str
    school_logo_url: str | None = None
    school_subdomain: str


class SchoolSettingsResponse(BaseSchema):
    """Full school + config view for the admin settings screen.

    Attributes:
        id: School id.
        name: Display name.
        subdomain: Subdomain.
        primary_color: Brand color.
        logo_url: Logo storage key.
        phone_country_code: Contact country code.
        phone_number: Contact phone.
        address / city / state / country / postal_code: Location fields.
        default_locale: Default locale.
        timezone: IANA timezone.
        config: Feature configuration block.
        created_at / updated_at: Timestamps.
    """

    id: IdStr
    name: str
    subdomain: str
    primary_color: str
    logo_url: str | None = None
    phone_country_code: str | None = None
    phone_number: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    postal_code: str | None = None
    default_locale: str | None = None
    timezone: str | None = None
    config: SchoolConfigResponse
    created_at: datetime
    updated_at: datetime


class SchoolSettingsUpdate(BaseRequest):
    """Partial update of school settings and config (admin only).

    All fields optional; ``None`` fields are left untouched.

    Attributes:
        name: New display name.
        subdomain: New subdomain (uniqueness enforced).
        primary_color: New brand color.
        logo_url: New logo storage key.
        phone_country_code / phone_number: Contact updates.
        address / city / state / country / postal_code: Location updates.
        default_locale / timezone: Preference updates.
        allow_multiple_class_teachers / enable_logging / enable_vision_ocr /
            current_academic_year: Config updates.
    """

    name: str | None = Field(default=None, max_length=255)
    subdomain: str | None = Field(default=None, max_length=63)
    primary_color: str | None = Field(default=None, max_length=9)
    logo_url: str | None = Field(default=None, max_length=512)
    phone_country_code: str | None = Field(default=None, max_length=8)
    phone_number: str | None = Field(default=None, max_length=15)
    address: str | None = Field(default=None, max_length=512)
    city: str | None = Field(default=None, max_length=128)
    state: str | None = Field(default=None, max_length=128)
    country: str | None = Field(default=None, max_length=2)
    postal_code: str | None = Field(default=None, max_length=16)
    default_locale: str | None = Field(default=None, max_length=10)
    timezone: str | None = Field(default=None, max_length=64)
    allow_multiple_class_teachers: bool | None = None
    enable_logging: bool | None = None
    enable_vision_ocr: bool | None = None
    current_academic_year: str | None = Field(default=None, max_length=16)
