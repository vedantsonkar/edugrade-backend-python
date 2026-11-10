"""Administration use cases: taxonomies, users, bulk CSV, stats, aggregates."""

import csv
import io
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.core.ids import IdProvider
from app.core.pagination import Page, PageParams
from app.core.security.passwords import PasswordHasher
from app.domain.enums import AuthMethod, UserRole
from app.domain.schools import School
from app.domain.taxonomy import Classroom, Division, Grade
from app.domain.users import User
from app.repositories.assignments import AssignmentRepository
from app.repositories.schools import SchoolConfigRepository, SchoolRepository
from app.repositories.taxonomy import (
    ClassroomRepository,
    DivisionRepository,
    GradeRepository,
)
from app.repositories.users import UserRepository
from app.schemas.admin import (
    BulkUserCsvResponse,
    BulkUserRowResult,
    ClassroomCreate,
    ClassroomUpdate,
    DivisionCreate,
    GradeCreate,
    StudentCreate,
    StudentUpdate,
    TeacherCreate,
    TeacherUpdate,
)
from app.schemas.school import SchoolConfigResponse


@dataclass(frozen=True)
class ClassTeachersPayload:
    """Aggregated data for the class-teacher assignment screen.

    Attributes:
        config: School feature config.
        classrooms: Classrooms with relations.
        teachers: Active-teacher picklist.
        grades / divisions: Taxonomy options for classroom creation.
    """

    config: SchoolConfigResponse
    classrooms: list[Classroom]
    teachers: list[User]
    grades: list[Grade]
    divisions: list[Division]


@dataclass(frozen=True)
class SchoolSetupPayload:
    """Aggregated data for the school-setup editor.

    Attributes:
        school: School entity (config loaded).
        grades: (grade, active-student-count) pairs, display-ordered.
        divisions: (division, active-student-count) pairs, name-ordered.
    """

    school: School
    grades: list[tuple[Grade, int]]
    divisions: list[tuple[Division, int]]


@dataclass(frozen=True)
class AdminStats:
    """Counters for the admin dashboard.

    Attributes:
        total_students / total_teachers / total_classes / total_assignments:
            Entity counts (users counted when active).
        classes_without_teacher: Classrooms missing a class teacher.
        config: School feature config.
    """

    total_students: int
    total_teachers: int
    total_classes: int
    total_assignments: int
    classes_without_teacher: int
    config: SchoolConfigResponse


@dataclass(frozen=True)
class TeacherCreated:
    """Creation result carrying the initial password only to the creator."""

    user: User
    generated_password: str


@dataclass(frozen=True)
class CsvUserChange:
    """Validated CSV row; no entity is changed during preview."""

    existing: User | None
    email: str
    name: str
    date_of_birth: datetime | None
    password: str | None
    grade_id: str | None
    division_id: str | None
    is_active: bool


class AdminService:
    """School-scoped administration workflows (admin role enforced upstream)."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        schools: SchoolRepository,
        configs: SchoolConfigRepository,
        grades: GradeRepository,
        divisions: DivisionRepository,
        classrooms: ClassroomRepository,
        users: UserRepository,
        assignments: AssignmentRepository,
        hasher: PasswordHasher,
        ids: IdProvider,
    ) -> None:
        """Inject repositories and infrastructure ports.

        Args:
            session: Request-scoped session (transaction boundary owner).
            schools: School repository.
            configs: Config repository.
            grades: Grade repository.
            divisions: Division repository.
            classrooms: Classroom repository.
            users: User repository.
            assignments: Assignment repository (stats only).
            hasher: Password hashing port.
            ids: Id provider (fallback passwords, key generation).
        """
        self._session = session
        self._schools = schools
        self._configs = configs
        self._grades = grades
        self._divisions = divisions
        self._classrooms = classrooms
        self._users = users
        self._assignments = assignments
        self._hasher = hasher
        self._ids = ids

    # ── Taxonomy: grades ─────────────────────────────────────────────────

    async def list_grades(self, school_id: str) -> list[Grade]:
        """List a school's grades.

        Args:
            school_id: Tenant.

        Returns:
            Ordered grades.
        """
        return await self._grades.list_for_school(school_id)

    async def create_grade(self, school_id: str, payload: GradeCreate) -> Grade:
        """Create a grade in the school.

        Args:
            school_id: Tenant.
            payload: Grade fields.

        Returns:
            The created grade.

        Raises:
            ConflictError: When the name already exists.
        """
        if await self._grades.name_exists(school_id, payload.name):
            raise ConflictError(f"Grade '{payload.name}' already exists.")
        return await self._grades.add(
            Grade(
                school_id=school_id,
                name=payload.name,
                display_order=payload.display_order,
                is_custom=payload.is_custom,
            )
        )

    async def delete_grade(self, school_id: str, grade_id: UUID) -> None:
        """Delete a grade (classrooms cascade via FK).

        Args:
            school_id: Tenant.
            grade_id: Grade id.

        Raises:
            NotFoundError: When the grade is not in the school.
        """
        grade = await self._grades.get(grade_id)
        if grade is None or str(grade.school_id) != school_id:
            raise NotFoundError("Grade not found.")
        await self._grades.delete(grade)

    # ── Taxonomy: divisions ──────────────────────────────────────────────

    async def list_divisions(self, school_id: str) -> list[Division]:
        """List a school's divisions.

        Args:
            school_id: Tenant.

        Returns:
            Ordered divisions.
        """
        return await self._divisions.list_for_school(school_id)

    async def create_division(self, school_id: str, payload: DivisionCreate) -> Division:
        """Create a division in the school.

        Args:
            school_id: Tenant.
            payload: Division fields.

        Returns:
            The created division.

        Raises:
            ConflictError: When the name already exists.
        """
        if await self._divisions.name_exists(school_id, payload.name):
            raise ConflictError(f"Division '{payload.name}' already exists.")
        return await self._divisions.add(Division(school_id=school_id, name=payload.name))

    async def delete_division(self, school_id: str, division_id: UUID) -> None:
        """Delete a division (classrooms cascade via FK).

        Args:
            school_id: Tenant.
            division_id: Division id.

        Raises:
            NotFoundError: When the division is not in the school.
        """
        division = await self._divisions.get(division_id)
        if division is None or str(division.school_id) != school_id:
            raise NotFoundError("Division not found.")
        await self._divisions.delete(division)

    # ── Taxonomy: classrooms ─────────────────────────────────────────────

    async def list_classrooms(self, school_id: str) -> list[Classroom]:
        """List a school's classrooms with relations loaded.

        Args:
            school_id: Tenant.

        Returns:
            Ordered classrooms.
        """
        return await self._classrooms.list_for_school(school_id)

    async def create_classroom(self, school_id: str, payload: ClassroomCreate) -> Classroom:
        """Create a classroom from a (grade, division) pair.

        Args:
            school_id: Tenant.
            payload: Classroom fields.

        Returns:
            The created classroom.

        Raises:
            NotFoundError: When grade/division/teacher refs are invalid.
            ConflictError: When the pair already exists.
        """
        grade = await self._grades.get(UUID(payload.grade_id))
        division = await self._divisions.get(UUID(payload.division_id))
        if grade is None or str(grade.school_id) != school_id:
            raise NotFoundError("Grade not found.")
        if division is None or str(division.school_id) != school_id:
            raise NotFoundError("Division not found.")
        class_teacher = None
        if payload.class_teacher_id:
            class_teacher = await self._users.get(UUID(payload.class_teacher_id))
            if class_teacher is None or str(class_teacher.school_id) != school_id:
                raise NotFoundError("Teacher not found.")
        if await self._classrooms.pair_exists(school_id, payload.grade_id, payload.division_id):
            raise ConflictError("A classroom for this grade and division exists.")

        academic_year = payload.academic_year
        if academic_year is None:
            config = await self._configs.get_for_school(school_id)
            academic_year = config.current_academic_year if config else "2025-2026"

        classroom = await self._classrooms.add(
            Classroom(
                school_id=school_id,
                grade_id=payload.grade_id,
                division_id=payload.division_id,
                class_teacher_id=payload.class_teacher_id,
                academic_year=academic_year,
            )
        )
        # Attach the already-fetched relations so response validation reads
        # loaded attributes instead of lazy-loading outside the session
        # (which raises MissingGreenlet in async context).
        classroom.grade = grade
        classroom.division = division
        if class_teacher is not None:
            classroom.class_teacher = class_teacher
        return classroom

    async def update_classroom(
        self, school_id: str, classroom_id: UUID, payload: ClassroomUpdate
    ) -> Classroom:
        """Assign or clear a classroom's class teacher.

        Args:
            school_id: Tenant.
            classroom_id: Classroom id.
            payload: Update payload (``class_teacher_id`` may be ``None`` to
                clear; distinguish "absent" via exclude_unset upstream).

        Returns:
            The updated classroom.

        Raises:
            NotFoundError: When the classroom or teacher is invalid.
        """
        classroom = await self._classrooms.get(classroom_id)
        if classroom is None or str(classroom.school_id) != school_id:
            raise NotFoundError("Classroom not found.")
        fields = payload.model_dump(exclude_unset=True)
        if "class_teacher_id" in fields:
            teacher_id = fields["class_teacher_id"]
            if teacher_id is not None:
                teacher = await self._users.get(UUID(str(teacher_id)))
                if (
                    teacher is None
                    or str(teacher.school_id) != school_id
                    or teacher.role != UserRole.TEACHER
                ):
                    raise NotFoundError("Teacher not found.")
            classroom.class_teacher_id = str(teacher_id) if teacher_id is not None else None
        await self._classrooms.session.flush()
        return classroom

    # ── Students ─────────────────────────────────────────────────────────

    async def list_students(
        self,
        school_id: str,
        params: PageParams,
        *,
        q: str | None = None,
        grade_id: str | None = None,
        division_id: str | None = None,
        status: str | None = None,
    ) -> Page[User]:
        """List students with filters and pagination.

        Args:
            school_id: Tenant.
            params: Pagination window.
            q: Name/email substring.
            grade_id: Grade filter.
            division_id: Division filter.
            status: ``active``/``inactive`` filter.

        Returns:
            Paginated students.
        """
        stmt = await self._users.scoped(school_id, UserRole.STUDENT)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(User.name.ilike(like) | User.email.ilike(like))
        if grade_id:
            stmt = stmt.where(User.grade_id == grade_id)
        if division_id:
            stmt = stmt.where(User.division_id == division_id)
        if status == "active":
            stmt = stmt.where(User.is_active.is_(True))
        elif status == "inactive":
            stmt = stmt.where(User.is_active.is_(False))
        rows, total = await self._users.paginate(stmt, params)
        return Page.of(rows, total, params)

    async def create_student(self, school_id: str, payload: StudentCreate) -> User:
        """Create a student account.

        Password resolution order: explicit payload → date of birth → random
        secret (reported to the admin via the returned entity's hash only).

        Args:
            school_id: Tenant.
            payload: Student fields.

        Returns:
            The created user.

        Raises:
            ConflictError: When the email is taken.
            ValidationFailedError: When no password source is available.
        """
        await self._ensure_email_free(payload.email)
        grade = await self._grades.get(UUID(payload.grade_id))
        if grade is None or str(grade.school_id) != school_id:
            raise NotFoundError("Grade not found.")
        if payload.division_id:
            division = await self._divisions.get(UUID(payload.division_id))
            if division is None or str(division.school_id) != school_id:
                raise NotFoundError("Division not found.")

        password_hash = await self._resolve_password_hash(payload.password, payload.date_of_birth)
        return await self._users.add(
            User(
                email=payload.email.strip().lower(),
                name=payload.name.strip(),
                role=UserRole.STUDENT,
                school_id=school_id,
                grade_id=payload.grade_id,
                division_id=payload.division_id,
                roll_number=payload.roll_number,
                student_identifier=payload.student_identifier,
                date_of_birth=payload.date_of_birth,
                password_hash=password_hash,
                auth_method=AuthMethod.CREDENTIALS,
            )
        )

    async def update_student(
        self, school_id: str, student_id: UUID, payload: StudentUpdate
    ) -> User:
        """Apply a partial update to a student.

        Args:
            school_id: Tenant.
            student_id: Student id.
            payload: Update payload.

        Returns:
            The updated student.

        Raises:
            NotFoundError: When the student is not in the school.
            ConflictError: When the email is taken.
        """
        student = await self._require_user(school_id, student_id, UserRole.STUDENT)
        for field, entity_id in [("grade", payload.grade_id), ("division", payload.division_id)]:
            if entity_id is not None:
                try:
                    entity_uuid = UUID(entity_id)
                except ValueError as exc:
                    raise ValidationFailedError("Invalid grade or division id.") from exc
                entity = (
                    await self._grades.get(entity_uuid)
                    if field == "grade"
                    else await self._divisions.get(entity_uuid)
                )
                if entity is None or str(entity.school_id) != school_id:
                    raise NotFoundError("Grade or division not found in this school.")
        fields = payload.model_dump(exclude_unset=True)
        if "email" in fields and fields["email"] is not None:
            new_email = str(fields["email"]).strip().lower()
            if new_email != student.email:
                await self._ensure_email_free(new_email)
                student.email = new_email
        if "password" in fields and fields["password"] is not None:
            student.password_hash = self._hasher.hash(str(fields["password"]))
        for key, value in fields.items():
            if key in ("email", "password"):
                continue
            setattr(student, key, value)
        await self._users.session.flush()
        return student

    # ── Teachers ─────────────────────────────────────────────────────────

    async def list_teachers(
        self,
        school_id: str,
        params: PageParams,
        *,
        q: str | None = None,
        status: str | None = None,
    ) -> Page[User]:
        """List teachers with filters and pagination.

        Args:
            school_id: Tenant.
            params: Pagination window.
            q: Name/email substring.
            status: ``active``/``inactive`` filter.

        Returns:
            Paginated teachers.
        """
        stmt = await self._users.scoped(school_id, UserRole.TEACHER)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(User.name.ilike(like) | User.email.ilike(like))
        if status == "active":
            stmt = stmt.where(User.is_active.is_(True))
        elif status == "inactive":
            stmt = stmt.where(User.is_active.is_(False))
        rows, total = await self._users.paginate(stmt, params)
        return Page.of(rows, total, params)

    async def create_teacher(self, school_id: str, payload: TeacherCreate) -> TeacherCreated:
        """Create a teacher account.

        Args:
            school_id: Tenant.
            payload: Teacher fields.

        Returns:
            The created user.

        Raises:
            ConflictError: When the email is taken.
        """
        await self._ensure_email_free(payload.email)
        password = payload.password or (
            self._generated_password(payload.name, payload.date_of_birth)
            if payload.date_of_birth
            else secrets.token_urlsafe(12)
        )
        user = await self._users.add(
            User(
                email=payload.email.strip().lower(),
                name=payload.name.strip(),
                role=UserRole.TEACHER,
                classes_taught=[],
                school_id=school_id,
                date_of_birth=payload.date_of_birth,
                password_hash=self._hasher.hash(password),
                auth_method=AuthMethod.CREDENTIALS,
                phone_country_code=payload.phone_country_code,
                phone_number=payload.phone_number,
            )
        )

        return TeacherCreated(user=user, generated_password=password)

    async def update_teacher(
        self, school_id: str, teacher_id: UUID, payload: TeacherUpdate
    ) -> User:
        """Apply a partial update to a teacher.

        Args:
            school_id: Tenant.
            teacher_id: Teacher id.
            payload: Update payload.

        Returns:
            The updated teacher.

        Raises:
            NotFoundError: When the teacher is not in the school.
            ConflictError: When the email is taken.
        """
        teacher = await self._require_user(school_id, teacher_id, UserRole.TEACHER)
        fields = payload.model_dump(exclude_unset=True)
        if "email" in fields and fields["email"] is not None:
            new_email = str(fields["email"]).strip().lower()
            if new_email != teacher.email:
                await self._ensure_email_free(new_email)
                teacher.email = new_email
        if "password" in fields and fields["password"] is not None:
            teacher.password_hash = self._hasher.hash(str(fields["password"]))
        for key, value in fields.items():
            if key in ("email", "password"):
                continue
            setattr(teacher, key, value)
        await self._users.session.flush()
        return teacher

    async def set_user_status(self, school_id: str, user_id: UUID, is_active: bool) -> User:
        """Activate or deactivate any user in the school.

        Args:
            school_id: Tenant.
            user_id: Target user.
            is_active: New state.

        Returns:
            The updated user.

        Raises:
            NotFoundError: When the user is not in the school.
        """
        user = await self._users.get(user_id)
        if user is None or str(user.school_id) != school_id:
            raise NotFoundError("User not found.")
        user.is_active = is_active
        await self._users.session.flush()
        return user

    # ── Bulk CSV ─────────────────────────────────────────────────────────

    async def bulk_import_users(  # noqa: PLR0912, PLR0915 - validate all rows before mutation
        self,
        school_id: str,
        csv_content: str,
        *,
        dry_run: bool,
        role: UserRole,
        operation: str = "upsert",
        skip_invalid: bool = True,
    ) -> BulkUserCsvResponse:
        """Validate a complete CSV before applying tenant-scoped user changes.

        Dry runs never modify loaded ORM entities. Invalid files are atomic unless
        the administrator explicitly chooses to skip invalid rows.

        Raises:
            ValidationFailedError: If the operation or CSV header is invalid.
        """
        from datetime import UTC, datetime

        if operation not in {"upsert", "create", "update", "status"}:
            raise ValidationFailedError("Unknown CSV operation.")
        reader = csv.DictReader(io.StringIO(csv_content.lstrip("\ufeff")))
        if reader.fieldnames is None:
            raise ValidationFailedError("CSV must include a header row.")
        reader.fieldnames = [h.strip().lower() for h in reader.fieldnames]
        if "email" not in reader.fieldnames:
            raise ValidationFailedError("CSV requires an email column.")
        grades = {g.name.lower(): g for g in await self._grades.list_for_school(school_id)}
        divisions = {d.name.lower(): d for d in await self._divisions.list_for_school(school_id)}
        changes: list[CsvUserChange] = []
        errors: list[BulkUserRowResult] = []
        seen: set[str] = set()
        total = 0
        for index, row in enumerate(reader, start=2):
            total += 1
            email = (row.get("email") or "").strip().lower()
            name = (row.get("name") or "").strip()
            existing = await self._users.get_by_email(email)
            message = ""
            if "@" not in email or email in seen:
                message = "Invalid or duplicate email."
            elif existing and (str(existing.school_id) != school_id or existing.role != role):
                message = "Account is not available in this school and role."
            elif operation == "create" and existing:
                message = "Account already exists."
            elif operation in {"update", "status"} and not existing:
                message = "Account does not exist."
            elif not existing and not name:
                message = "Name is required."
            seen.add(email)
            dob_raw = (row.get("dob") or row.get("date_of_birth") or "").strip()
            dob = None
            if dob_raw:
                try:
                    dob = datetime.strptime(dob_raw, "%Y-%m-%d").replace(tzinfo=UTC)
                except ValueError:
                    message = "Date of birth must be YYYY-MM-DD."
            grade_raw = (row.get("grade") or row.get("grade_name") or "").strip().lower()
            division_raw = (row.get("division") or row.get("division_name") or "").strip().lower()
            grade = grades.get(grade_raw)
            division = divisions.get(division_raw)
            if role == UserRole.STUDENT and grade_raw and grade is None:
                message = "Unknown grade."
            if role == UserRole.STUDENT and division_raw and division is None:
                message = "Unknown division."
            active_raw = (row.get("isactive") or "").strip().lower()
            if operation == "status" and active_raw not in {"true", "false"}:
                message = "isActive must be true or false."
            password = (row.get("password") or "").strip()
            if not existing and not password and dob is None:
                message = "Provide a password or date of birth."
            if message:
                errors.append(
                    BulkUserRowResult(row=index, email=email, status="error", message=message)
                )
                continue
            changes.append(
                CsvUserChange(
                    existing=existing,
                    email=email,
                    name=name,
                    date_of_birth=dob,
                    password=password or None,
                    grade_id=str(grade.id) if grade else None,
                    division_id=str(division.id) if division else None,
                    is_active=active_raw == "true",
                )
            )
        can_apply = not dry_run and (not errors or skip_invalid)
        created = sum(change.existing is None for change in changes)
        updated = len(changes) - created
        if can_apply:
            for change in changes:
                user = change.existing
                if user is None:
                    password = change.password or (
                        self._generated_password(change.name, change.date_of_birth)
                        if change.date_of_birth
                        else secrets.token_urlsafe(16)
                    )
                    await self._users.add(
                        User(
                            email=change.email,
                            name=change.name,
                            role=role,
                            school_id=school_id,
                            grade_id=change.grade_id,
                            division_id=change.division_id,
                            date_of_birth=change.date_of_birth,
                            password_hash=self._hasher.hash(password),
                            auth_method=AuthMethod.CREDENTIALS,
                        )
                    )
                elif operation == "status":
                    user.is_active = change.is_active
                else:
                    if change.name:
                        user.name = change.name
                    if change.date_of_birth:
                        user.date_of_birth = change.date_of_birth
                    if change.grade_id:
                        user.grade_id = change.grade_id
                    if change.division_id:
                        user.division_id = change.division_id
                    if change.password:
                        user.password_hash = self._hasher.hash(change.password)
            await self._session.flush()
        return BulkUserCsvResponse(
            created=created if dry_run or can_apply else 0,
            updated=updated if dry_run or can_apply else 0,
            skipped=len(errors),
            errors=errors,
            valid=not errors,
            total_rows=total,
            valid_rows=len(changes),
            invalid_rows=len(errors),
        )

    async def user_counts(self, school_id: str, role: str) -> tuple[int, int]:
        """Read activation counts for a validated role in the caller's school."""
        try:
            selected = UserRole(role)
        except ValueError as exc:
            raise ValidationFailedError("Unknown user role.") from exc
        return await self._users.activation_counts(school_id, selected)

    async def class_teachers_payload(self, school: School) -> ClassTeachersPayload:
        """Aggregate the class-teacher screen's initial payload.

        Args:
            school: Tenant school (config loaded).

        Returns:
            Aggregated payload.
        """
        school_id = str(school.id)
        config = school.config
        return ClassTeachersPayload(
            config=SchoolConfigResponse.model_validate(config)
            if config
            else SchoolConfigResponse(
                allow_multiple_class_teachers=False,
                enable_logging=False,
                enable_vision_ocr=True,
                current_academic_year="2025-2026",
            ),
            classrooms=await self._classrooms.list_for_school(school_id),
            teachers=await self._classrooms.active_teacher_picklist(school_id),
            grades=await self._grades.list_for_school(school_id),
            divisions=await self._divisions.list_for_school(school_id),
        )

    async def school_setup_payload(self, school: School) -> SchoolSetupPayload:
        """Aggregate the school-setup editor's initial payload.

        Args:
            school: Tenant school (config loaded).

        Returns:
            Aggregated payload with per-grade/division student counts.
        """
        school_id = str(school.id)
        grade_counts = await self._users.count_by_grade(school_id)
        division_counts = await self._users.count_by_division(school_id)
        grades = await self._grades.list_for_school(school_id)
        divisions = await self._divisions.list_for_school(school_id)
        return SchoolSetupPayload(
            school=school,
            grades=[(g, grade_counts.get(str(g.id), 0)) for g in grades],
            divisions=[(d, division_counts.get(str(d.id), 0)) for d in divisions],
        )

    async def stats(self, school: School) -> AdminStats:
        """Compute dashboard counters for a school.

        Args:
            school: Tenant school.

        Returns:
            Aggregated stats.
        """
        school_id = str(school.id)
        config = school.config
        return AdminStats(
            total_students=await self._users.count_active(school_id, UserRole.STUDENT),
            total_teachers=await self._users.count_active(school_id, UserRole.TEACHER),
            total_classes=await self._classrooms.count_for_school(school_id),
            total_assignments=await self._assignments.count_for_school(school_id),
            classes_without_teacher=await self._classrooms.count_without_teacher(school_id),
            config=SchoolConfigResponse.model_validate(config)
            if config
            else SchoolConfigResponse(
                allow_multiple_class_teachers=False,
                enable_logging=False,
                enable_vision_ocr=True,
                current_academic_year="2025-2026",
            ),
        )

    # ── Internals ────────────────────────────────────────────────────────

    async def _ensure_email_free(self, email: str) -> None:
        """Raise when an email is already registered.

        Args:
            email: Candidate email.

        Raises:
            ConflictError: When taken.
        """
        if await self._users.get_by_email(email) is not None:
            raise ConflictError("A user with this email already exists.")

    async def _require_user(self, school_id: str, user_id: UUID, role: UserRole) -> User:
        """Fetch a user of a role enforced to the school.

        Args:
            school_id: Tenant.
            user_id: User id.
            role: Expected role.

        Returns:
            The user.

        Raises:
            NotFoundError: When missing or role-mismatched.
        """
        user = await self._users.get(user_id)
        if user is None or str(user.school_id) != school_id or user.role != role:
            raise NotFoundError("User not found.")
        return user

    @staticmethod
    def _generated_password(name: str, date_of_birth: datetime) -> str:
        """Build the auto-generated initial password ``Name + YYYYMMDD``.

        The name is reduced to ASCII letters and digits so the password
        survives CSV round-trips, shell logins, and form validation —
        apostrophes and hyphens in names must not leak into credentials.

        Args:
            name: Account holder's full name.
            date_of_birth: DOB whose digits suffix the password.

        Returns:
            The generated password (random token when the name yields nothing).
        """
        stem = re.sub(r"[^A-Za-z0-9]", "", name)
        if not stem:
            return secrets.token_urlsafe(12)
        return stem + date_of_birth.strftime("%Y%m%d")

    async def _resolve_password_hash(
        self, password: str | None, date_of_birth: datetime | None
    ) -> str | None:
        """Resolve a student's initial password hash.

        Precedence: explicit password → DOB digits (``YYYYMMDD``) → ``None``
        (account created without a password; SSO-style).

        Args:
            password: Explicit password, when given.
            date_of_birth: DOB fallback, when given.

        Returns:
            Hash, or ``None`` when no source exists.
        """
        if password:
            return self._hasher.hash(password)
        if date_of_birth is not None:
            return self._hasher.hash(date_of_birth.strftime("%Y%m%d"))
        return None
