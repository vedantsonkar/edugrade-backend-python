"""Administration use cases: taxonomies, users, bulk CSV, stats, aggregates."""

import csv
import io
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

    async def create_division(
        self, school_id: str, payload: DivisionCreate
    ) -> Division:
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
        return await self._divisions.add(
            Division(school_id=school_id, name=payload.name)
        )

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

    async def create_classroom(
        self, school_id: str, payload: ClassroomCreate
    ) -> Classroom:
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
        if await self._classrooms.pair_exists(school_id, payload.grade_id, payload.division_id):
            raise ConflictError("A classroom for this grade and division exists.")

        academic_year = payload.academic_year
        if academic_year is None:
            config = await self._configs.get_for_school(school_id)
            academic_year = config.current_academic_year if config else "2025-2026"

        return await self._classrooms.add(
            Classroom(
                school_id=school_id,
                grade_id=payload.grade_id,
                division_id=payload.division_id,
                class_teacher_id=payload.class_teacher_id,
                academic_year=academic_year,
            )
        )

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
            classroom.class_teacher_id = (
                str(teacher_id) if teacher_id is not None else None
            )
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

    async def create_student(
        self, school_id: str, payload: StudentCreate
    ) -> User:
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

    async def create_teacher(
        self, school_id: str, payload: TeacherCreate
    ) -> User:
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
        password = payload.password or secrets.token_urlsafe(12)
        return await self._users.add(
            User(
                email=payload.email.strip().lower(),
                name=payload.name.strip(),
                role=UserRole.TEACHER,
                school_id=school_id,
                password_hash=self._hasher.hash(password),
                auth_method=AuthMethod.CREDENTIALS,
                phone_country_code=payload.phone_country_code,
                phone_number=payload.phone_number,
            )
        )

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

    async def set_user_status(
        self, school_id: str, user_id: UUID, is_active: bool
    ) -> User:
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

    async def bulk_import_users(  # noqa: PLR0912, PLR0915 - CSV row pipeline
        self, school_id: str, csv_content: str, *, dry_run: bool, role: UserRole
    ) -> BulkUserCsvResponse:
        """Import users from CSV (create or update by email).

        Expected columns: ``email,name`` plus ``grade_name,division_name`` for
        students. Rows update matching emails, create the rest.

        Args:
            school_id: Tenant.
            csv_content: Raw CSV text with a header row.
            dry_run: Validate only; persist nothing.
            role: Role assigned to created users.

        Returns:
            Aggregate outcome with per-row errors.

        Raises:
            ValidationFailedError: When the CSV is unreadable or headerless.
        """
        created = updated = skipped = 0
        errors: list[BulkUserRowResult] = []
        try:
            reader = csv.DictReader(io.StringIO(csv_content))
            if reader.fieldnames is None:
                raise ValidationFailedError("CSV must include a header row.")
            headers = [h.strip().lower() for h in reader.fieldnames]
            if "email" not in headers or "name" not in headers:
                raise ValidationFailedError("CSV requires 'email' and 'name' columns.")

            grade_names = {g.name.lower(): g for g in await self._grades.list_for_school(school_id)}
            division_names = {
                d.name.lower(): d for d in await self._divisions.list_for_school(school_id)
            }

            for index, row in enumerate(reader, start=2):
                email = (row.get("email") or "").strip().lower()
                name = (row.get("name") or "").strip()
                if not email or not name:
                    errors.append(
                        BulkUserRowResult(row=index, email=email or "-", status="error",
                                          message="email and name are required.")
                    )
                    continue
                existing = await self._users.get_by_email(email)
                if existing is not None:
                    if existing.role != role:
                        errors.append(
                            BulkUserRowResult(
                                row=index, email=email, status="error",
                                message=f"email belongs to a "
                                        f"{existing.role.value.lower()}.",
                            )
                        )
                        continue
                    existing.name = name
                    updated += 1
                    continue
                if role is UserRole.STUDENT:
                    grade_name = (row.get("grade_name") or "").strip().lower()
                    division_name = (row.get("division_name") or "").strip().lower()
                    grade = grade_names.get(grade_name)
                    division = division_names.get(division_name)
                    if grade is None:
                        errors.append(
                            BulkUserRowResult(row=index, email=email, status="error",
                                              message=f"unknown grade '{grade_name}'.")
                        )
                        continue
                    dob_raw = (row.get("date_of_birth") or "").strip()
                    date_of_birth = None
                    if dob_raw:
                        try:
                            from datetime import datetime as _dt

                            date_of_birth = _dt.strptime(dob_raw, "%Y-%m-%d")
                        except ValueError:
                            errors.append(
                                BulkUserRowResult(row=index, email=email, status="error",
                                                  message="date_of_birth must be YYYY-MM-DD.")
                            )
                            continue
                    if not dry_run:
                        await self._users.add(
                            User(
                                email=email,
                                name=name,
                                role=UserRole.STUDENT,
                                school_id=school_id,
                                grade_id=str(grade.id),
                                division_id=str(division.id) if division else None,
                                date_of_birth=date_of_birth,
                                password_hash=(
                                    self._hasher.hash(dob_raw.replace("-", ""))
                                    if dob_raw
                                    else self._hasher.hash(secrets.token_urlsafe(12))
                                ),
                                auth_method=AuthMethod.CREDENTIALS,
                            )
                        )
                    created += 1
                else:
                    if not dry_run:
                        await self._users.add(
                            User(
                                email=email,
                                name=name,
                                role=UserRole.TEACHER,
                                school_id=school_id,
                                password_hash=self._hasher.hash(secrets.token_urlsafe(12)),
                                auth_method=AuthMethod.CREDENTIALS,
                            )
                        )
                    created += 1
        except csv.Error as exc:
            raise ValidationFailedError(f"CSV parse error: {exc}") from exc

        if not dry_run:
            await self._session.flush()
        if errors:
            skipped = len(errors)
        return BulkUserCsvResponse(created=created, updated=updated, skipped=skipped, errors=errors)

    # ── Aggregated payloads ──────────────────────────────────────────────

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
            config=SchoolConfigResponse.model_validate(config) if config
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
            config=SchoolConfigResponse.model_validate(config) if config else SchoolConfigResponse(
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

    async def _require_user(
        self, school_id: str, user_id: UUID, role: UserRole
    ) -> User:
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
