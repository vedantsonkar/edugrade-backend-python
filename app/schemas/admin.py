"""Admin-module wire schemas: taxonomies, users, bulk CSV, stats, audit."""

from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import BaseRequest, BaseSchema, IdStr
from app.schemas.school import SchoolConfigResponse, SchoolSettingsResponse


class GradeResponse(BaseSchema):
    """Grade row.

    Attributes:
        id: Grade id.
        school_id: Owning school.
        name: Display name.
        display_order: Sort key.
        is_custom: School-defined name flag.
        created_at / updated_at: Timestamps.
    """

    id: IdStr
    school_id: IdStr
    name: str
    display_order: int
    is_custom: bool
    created_at: datetime
    updated_at: datetime


class GradeCreate(BaseRequest):
    """Create-grade payload.

    Attributes:
        name: Display name.
        display_order: Sort key.
        is_custom: Whether school-defined.
    """

    name: str = Field(min_length=1, max_length=100)
    display_order: int = 0
    is_custom: bool = False


class DivisionResponse(BaseSchema):
    """Division row.

    Attributes:
        id: Division id.
        school_id: Owning school.
        name: Display name.
        created_at / updated_at: Timestamps.
    """

    id: IdStr
    school_id: IdStr
    name: str
    created_at: datetime
    updated_at: datetime


class DivisionCreate(BaseRequest):
    """Create-division payload.

    Attributes:
        name: Display name.
    """

    name: str = Field(min_length=1, max_length=100)


class UserSummary(BaseSchema):
    """Compact user projection for nesting.

    Attributes:
        id: User id.
        name: Display name.
        email: Email.
        avatar_url: Avatar storage key.
        role: Role string value.
    """

    id: IdStr
    name: str
    email: str
    avatar_url: str | None = None
    role: str


class ClassroomResponse(BaseSchema):
    """Classroom row with optional related projections.

    Attributes:
        id: Classroom id.
        school_id: Owning school.
        grade_id / division_id: Taxonomy references.
        class_teacher_id: Leading teacher, if any.
        academic_year: Academic year.
        grade / division / class_teacher: Nested projections when loaded.
        created_at / updated_at: Timestamps.
    """

    id: IdStr
    school_id: IdStr
    grade_id: IdStr
    division_id: IdStr
    class_teacher_id: IdStr | None = None
    academic_year: str
    grade: GradeResponse | None = None
    division: DivisionResponse | None = None
    class_teacher: UserSummary | None = None
    created_at: datetime
    updated_at: datetime


class ClassroomCreate(BaseRequest):
    """Create-classroom payload.

    Attributes:
        grade_id: Grade reference.
        division_id: Division reference.
        class_teacher_id: Optional leading teacher.
        academic_year: Academic year label; defaults to school config.
    """

    grade_id: IdStr
    division_id: IdStr
    class_teacher_id: IdStr | None = None
    academic_year: str | None = None


class ClassroomUpdate(BaseRequest):
    """Update-classroom payload (class-teacher assignment).

    Attributes:
        class_teacher_id: New leading teacher; ``null`` clears the role.
    """

    class_teacher_id: IdStr | None = None


class StudentResponse(BaseSchema):
    """Student row for management listings.

    Attributes:
        id: User id.
        email / name: Identity.
        grade_id / division_id: Placement.
        grade / division: Nested taxonomy projections when loaded.
        roll_number / student_identifier: Bookkeeping.
        date_of_birth: DOB.
        auth_method: Allowed login methods.
        is_active: Activation state.
        last_login_at: Last login instant.
        phone_country_code / phone_number / preferred_locale: Contact prefs.
        created_at / updated_at: Timestamps.
    """

    id: IdStr
    email: str
    name: str
    role: str
    grade_id: IdStr | None = None
    division_id: IdStr | None = None
    grade: GradeResponse | None = None
    division: DivisionResponse | None = None
    roll_number: str | None = None
    student_identifier: str | None = None
    date_of_birth: datetime | None = None
    auth_method: str
    is_active: bool
    last_login_at: datetime | None = None
    phone_country_code: str | None = None
    phone_number: str | None = None
    preferred_locale: str | None = None
    created_at: datetime
    updated_at: datetime


class StudentCreate(BaseRequest):
    """Create-student payload.

    Attributes:
        email: Login email (unique).
        name: Display name.
        grade_id: Placement grade.
        division_id: Placement division.
        roll_number: Roll number.
        student_identifier: External identifier.
        date_of_birth: DOB; also the default password fallback.
        password: Optional explicit password; DOB or generated fallback used
            when omitted.
    """

    email: str = Field(max_length=255)
    name: str = Field(min_length=1, max_length=255)
    grade_id: IdStr
    division_id: IdStr | None = None
    roll_number: str | None = Field(default=None, max_length=64)
    student_identifier: str | None = Field(default=None, max_length=64)
    date_of_birth: datetime | None = None
    password: str | None = Field(default=None, max_length=1024)


class StudentUpdate(BaseRequest):
    """Partial student update; ``None`` fields untouched.

    Attributes:
        name / email: Identity updates.
        grade_id / division_id: Placement updates.
        roll_number / student_identifier: Bookkeeping updates.
        date_of_birth: DOB update.
        is_active: Activation toggle.
        phone_country_code / phone_number / preferred_locale: Contact updates.
        password: Password reset (hashed server-side).
    """

    name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    grade_id: IdStr | None = None
    division_id: IdStr | None = None
    roll_number: str | None = Field(default=None, max_length=64)
    student_identifier: str | None = Field(default=None, max_length=64)
    date_of_birth: datetime | None = None
    is_active: bool | None = None
    phone_country_code: str | None = Field(default=None, max_length=8)
    phone_number: str | None = Field(default=None, max_length=15)
    preferred_locale: str | None = Field(default=None, max_length=10)
    password: str | None = Field(default=None, max_length=1024)


class TeacherCreate(BaseRequest):
    """Create-teacher payload.

    Attributes:
        email: Login email (unique).
        name: Display name.
        password: Optional explicit password.
        phone_country_code / phone_number: Contact.
    """

    email: str = Field(max_length=255)
    name: str = Field(min_length=1, max_length=255)
    password: str | None = Field(default=None, max_length=1024)
    phone_country_code: str | None = Field(default=None, max_length=8)
    phone_number: str | None = Field(default=None, max_length=15)


class TeacherUpdate(BaseRequest):
    """Partial teacher update; ``None`` fields untouched.

    Attributes:
        name / email: Identity updates.
        is_active: Activation toggle.
        phone_country_code / phone_number: Contact updates.
        password: Password reset (hashed server-side).
    """

    name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None
    phone_country_code: str | None = Field(default=None, max_length=8)
    phone_number: str | None = Field(default=None, max_length=15)
    password: str | None = Field(default=None, max_length=1024)


class UserStatusUpdate(BaseRequest):
    """Activate/deactivate a user account.

    Attributes:
        is_active: New activation state.
    """

    is_active: bool


class BulkUserCsvRequest(BaseRequest):
    """Bulk CSV import payload (users or students).

    Attributes:
        csv: Raw CSV content; header row required.
        dry_run: When true, validate and report without persisting.
    """

    csv: str = Field(min_length=1)
    dry_run: bool = False


class BulkUserRowResult(BaseSchema):
    """Outcome for one CSV row.

    Attributes:
        row: 1-based CSV row number (excluding header).
        email: Parsed email.
        status: ``created`` / ``updated`` / ``skipped`` / ``error``.
        message: Human-readable detail.
    """

    row: int
    email: str
    status: str
    message: str


class BulkUserCsvResponse(BaseSchema):
    """Aggregate result of a bulk CSV import.

    Attributes:
        created: Rows created.
        updated: Rows updated.
        skipped: Rows skipped (dry run or no-op).
        errors: Failed rows with reasons.
    """

    created: int
    updated: int
    skipped: int
    errors: list[BulkUserRowResult]


class AdminStatsResponse(BaseSchema):
    """Dashboard counters for the admin overview.

    Attributes:
        total_students: Active students.
        total_teachers: Active teachers.
        total_classes: Classrooms.
        total_assignments: Assignments.
        classes_without_teacher: Classrooms missing a class teacher.
        current_academic_year: Configured academic year.
        allow_multiple_class_teachers: Config flag.
    """

    total_students: int
    total_teachers: int
    total_classes: int
    total_assignments: int
    classes_without_teacher: int
    current_academic_year: str | None = None
    allow_multiple_class_teachers: bool = False


class GradeWithStudentCount(BaseSchema):
    """Grade plus its active-student count (school setup screen).

    Attributes:
        grade: Grade row.
        student_count: Active students in the grade.
    """

    grade: GradeResponse
    student_count: int


class DivisionWithStudentCount(BaseSchema):
    """Division plus its active-student count (school setup screen).

    Attributes:
        division: Division row.
        student_count: Active students in the division.
    """

    division: DivisionResponse
    student_count: int


class SchoolSetupResponse(BaseSchema):
    """Aggregated payload for the school-setup editor.

    Attributes:
        school: School + config.
        grades: Grades with student counts.
        divisions: Divisions with student counts.
    """

    school: SchoolSettingsResponse
    grades: list[GradeWithStudentCount]
    divisions: list[DivisionWithStudentCount]


class ClassTeachersResponse(BaseSchema):
    """Aggregated payload for the class-teacher assignment screen.

    Attributes:
        config: School feature config.
        classrooms: Classrooms with grade/division/teacher.
        teachers: Active teachers (picklist).
        grades: Grade options for creating classrooms.
        divisions: Division options for creating classrooms.
    """

    config: SchoolConfigResponse
    classrooms: list[ClassroomResponse]
    teachers: list[UserSummary]
    grades: list[GradeResponse]
    divisions: list[DivisionResponse]


class AuditLogResponse(BaseSchema):
    """Audit event row.

    Attributes:
        id: Log id.
        user_id: Acting user, when known.
        school_id: Tenant.
        action: Event name.
        resource: Affected resource, when applicable.
        details: Structured payload.
        ip_address / user_agent: Request provenance.
        created_at: Event instant.
    """

    id: IdStr
    user_id: IdStr | None = None
    school_id: IdStr
    action: str
    resource: IdStr | None = None
    details: dict[str, object] | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    created_at: datetime


class AuditDownloadResponse(BaseSchema):
    """Presigned download grant for an archived audit file.

    Attributes:
        key: Archived object key.
        url: Pre-signed GET URL.
        expires_in: Seconds until expiry.
    """

    key: str
    url: str
    expires_in: int


class UserStatusPath(BaseRequest):
    """Path-parameter carrier for status updates (documentation aid).

    Attributes:
        user_id: Target user id.
    """

    user_id: UUID

class TeacherResponse(BaseSchema):
    """Teacher row for management listings.

    Attributes:
        id: User id.
        email / name: Identity.
        role: Role string value.
        avatar_url: Avatar storage key.
        is_active: Activation state.
        classes_taught: Classroom ids led by this teacher.
        phone_country_code / phone_number: Contact.
        last_login_at: Last login instant.
        created_at / updated_at: Timestamps.
    """

    id: IdStr
    email: str
    name: str
    role: str
    avatar_url: str | None = None
    is_active: bool
    classes_taught: list[str] = []
    phone_country_code: str | None = None
    phone_number: str | None = None
    last_login_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
