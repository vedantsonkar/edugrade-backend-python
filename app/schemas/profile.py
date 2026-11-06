"""Profile wire schemas: read/update, avatar, password change."""

from datetime import datetime

from pydantic import Field

from app.schemas.admin import DivisionResponse, GradeResponse
from app.schemas.common import BaseRequest, BaseSchema, IdStr


class ProfileResponse(BaseSchema):
    """Full profile of the authenticated user.

    Attributes:
        id: User id.
        email: Email.
        name: Display name.
        role: Role string value.
        avatar_url: Avatar storage key.
        grade_id / division_id: Student placement.
        grade / division: Nested projections when loaded.
        roll_number / student_identifier: Student bookkeeping.
        date_of_birth: DOB.
        auth_method: Allowed login methods (drives password-change UX).
        phone_country_code / phone_number / preferred_locale: Contact prefs.
        created_at: Registration instant.
    """

    id: IdStr
    email: str
    name: str
    role: str
    avatar_url: str | None = None
    grade_id: IdStr | None = None
    division_id: IdStr | None = None
    grade: GradeResponse | None = None
    division: DivisionResponse | None = None
    roll_number: str | None = None
    student_identifier: str | None = None
    date_of_birth: datetime | None = None
    auth_method: str
    phone_country_code: str | None = None
    phone_number: str | None = None
    preferred_locale: str | None = None
    created_at: datetime


class ProfileUpdateRequest(BaseRequest):
    """Partial profile update; ``None`` fields untouched.

    Attributes:
        name: Display name.
        email: Email (uniqueness enforced).
        phone_country_code / phone_number: Contact.
        preferred_locale: Locale preference.
        date_of_birth: DOB.
    """

    name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    phone_country_code: str | None = Field(default=None, max_length=8)
    phone_number: str | None = Field(default=None, max_length=15)
    preferred_locale: str | None = Field(default=None, max_length=10)
    date_of_birth: datetime | None = None


class PasswordUpdateRequest(BaseRequest):
    """Password change after verifying the current password.

    Attributes:
        current_password: Existing password.
        new_password: Replacement (min 8 chars).
    """

    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=8, max_length=1024)
