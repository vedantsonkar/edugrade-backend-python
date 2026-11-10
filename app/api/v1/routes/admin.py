"""Admin routes: taxonomies, users, bulk CSV, stats, and the audit trail.

All routes require the ADMIN role except the staff-visible listing endpoints.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response

from app.api.v1.deps import (
    get_admin_service,
    get_audit_service,
    get_school_repo,
    get_school_service,
    get_user_repo,
)
from app.core.errors import ForbiddenError, NotFoundError
from app.core.pagination import Page, PageParams, page_params
from app.core.ratelimit.dependency import rate_limit
from app.core.security.guards import AdminUser, StaffUser
from app.domain.users import User
from app.repositories.schools import SchoolRepository
from app.repositories.users import UserRepository
from app.schemas.admin import (
    AdminStatsResponse,
    AuditArchiveResponse,
    AuditDownloadResponse,
    AuditLogResponse,
    BulkUserCsvRequest,
    BulkUserCsvResponse,
    ClassroomCreate,
    ClassroomResponse,
    ClassroomUpdate,
    ClassTeachersResponse,
    DivisionCreate,
    DivisionResponse,
    DivisionWithStudentCount,
    GradeCreate,
    GradeResponse,
    GradeWithStudentCount,
    SchoolSetupResponse,
    StudentCreate,
    StudentResponse,
    StudentUpdate,
    TeacherCreate,
    TeacherCreatedResponse,
    TeacherResponse,
    TeacherUpdate,
    UserCountsResponse,
    UserStatusUpdate,
    UserSummary,
)
from app.schemas.school import SchoolSettingsResponse, SchoolSettingsUpdate
from app.services.admin import AdminService
from app.services.audit import AuditService
from app.services.schools import SchoolService

router = APIRouter(prefix="/admin", tags=["admin"])


# ── Classrooms ───────────────────────────────────────────────────────────────


@router.get(
    "/classes",
    response_model=list[ClassroomResponse],
    summary="List classrooms (staff)",
)
async def list_classes(
    current_user: StaffUser,
    service: Annotated[AdminService, Depends(get_admin_service)],
) -> list[ClassroomResponse]:
    """List the school's classrooms with grade/division/teacher.

    Args:
        current_user: Authenticated staff principal.
        service: Admin service.

    Returns:
        Ordered classrooms.
    """
    classrooms = await service.list_classrooms(_school(current_user))
    return [ClassroomResponse.model_validate(c) for c in classrooms]


@router.post(
    "/classes",
    response_model=ClassroomResponse,
    summary="Create classroom",
)
async def create_class(
    payload: ClassroomCreate,
    current_user: AdminUser,
    request: Request,
    service: Annotated[AdminService, Depends(get_admin_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> ClassroomResponse:
    """Create a (grade, division) classroom.

    Args:
        payload: Classroom fields.
        current_user: Authenticated admin.
        request: Incoming request (audit provenance).
        service: Admin service.
        audit: Audit service.

    Returns:
        The created classroom.
    """
    classroom = await service.create_classroom(_school(current_user), payload)
    await audit.record_event(
        school_id=_school(current_user),
        principal=current_user,
        request=request,
        action="CLASS_CREATED",
        resource=str(classroom.id),
        details={
            "gradeId": payload.grade_id,
            "divisionId": payload.division_id,
            "academicYear": classroom.academic_year,
        },
    )
    return ClassroomResponse.model_validate(classroom)


@router.patch(
    "/classes/{class_id}",
    response_model=ClassroomResponse,
    summary="Update classroom (class teacher)",
)
async def update_class(
    class_id: UUID,
    payload: ClassroomUpdate,
    current_user: AdminUser,
    request: Request,
    service: Annotated[AdminService, Depends(get_admin_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> ClassroomResponse:
    """Assign or clear a classroom's class teacher.

    Args:
        class_id: Classroom id.
        payload: Update payload.
        current_user: Authenticated admin.
        request: Incoming request (audit provenance).
        service: Admin service.
        audit: Audit service.

    Returns:
        The updated classroom.
    """
    classroom = await service.update_classroom(_school(current_user), class_id, payload)
    await audit.record_event(
        school_id=_school(current_user),
        principal=current_user,
        request=request,
        action="CLASS_TEACHER_ASSIGNED",
        resource=str(classroom.id),
        details={"classTeacherId": classroom.class_teacher_id},
    )
    return ClassroomResponse.model_validate(classroom)


# ── Divisions / Grades ───────────────────────────────────────────────────────


@router.get(
    "/divisions",
    response_model=list[DivisionResponse],
    summary="List divisions",
)
async def list_divisions(
    current_user: AdminUser,
    service: Annotated[AdminService, Depends(get_admin_service)],
) -> list[DivisionResponse]:
    """List the school's divisions.

    Args:
        current_user: Authenticated admin.
        service: Admin service.

    Returns:
        Ordered divisions.
    """
    return [
        DivisionResponse.model_validate(d)
        for d in await service.list_divisions(_school(current_user))
    ]


@router.post(
    "/divisions",
    response_model=DivisionResponse,
    summary="Create division",
)
async def create_division(
    payload: DivisionCreate,
    current_user: AdminUser,
    request: Request,
    service: Annotated[AdminService, Depends(get_admin_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> DivisionResponse:
    """Create a division.

    Args:
        payload: Division fields.
        current_user: Authenticated admin.
        request: Incoming request (audit provenance).
        service: Admin service.
        audit: Audit service.

    Returns:
        The created division.
    """
    division = await service.create_division(_school(current_user), payload)
    await audit.record_event(
        school_id=_school(current_user),
        principal=current_user,
        request=request,
        action="DIVISION_CREATED",
        resource=str(division.id),
        details={"name": division.name},
    )
    return DivisionResponse.model_validate(division)


@router.delete(
    "/divisions/{division_id}",
    status_code=204,
    summary="Delete division",
)
async def delete_division(
    division_id: UUID,
    current_user: AdminUser,
    request: Request,
    service: Annotated[AdminService, Depends(get_admin_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> Response:
    """Delete a division (classrooms cascade).

    Args:
        division_id: Division id.
        current_user: Authenticated admin.
        request: Incoming request (audit provenance).
        service: Admin service.
        audit: Audit service.

    Returns:
        Empty 204 response.
    """
    await service.delete_division(_school(current_user), division_id)
    await audit.record_event(
        school_id=_school(current_user),
        principal=current_user,
        request=request,
        action="DIVISION_DELETED",
        resource=str(division_id),
    )
    return Response(status_code=204)


@router.get("/grades", response_model=list[GradeResponse], summary="List grades")
async def list_grades(
    current_user: AdminUser,
    service: Annotated[AdminService, Depends(get_admin_service)],
) -> list[GradeResponse]:
    """List the school's grades.

    Args:
        current_user: Authenticated admin.
        service: Admin service.

    Returns:
        Ordered grades.
    """
    return [
        GradeResponse.model_validate(g) for g in await service.list_grades(_school(current_user))
    ]


@router.post("/grades", response_model=GradeResponse, summary="Create grade")
async def create_grade(
    payload: GradeCreate,
    current_user: AdminUser,
    request: Request,
    service: Annotated[AdminService, Depends(get_admin_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> GradeResponse:
    """Create a grade.

    Args:
        payload: Grade fields.
        current_user: Authenticated admin.
        request: Incoming request (audit provenance).
        service: Admin service.
        audit: Audit service.

    Returns:
        The created grade.
    """
    grade = await service.create_grade(_school(current_user), payload)
    await audit.record_event(
        school_id=_school(current_user),
        principal=current_user,
        request=request,
        action="GRADE_CREATED",
        resource=str(grade.id),
        details={"name": grade.name},
    )
    return GradeResponse.model_validate(grade)


@router.delete("/grades/{grade_id}", status_code=204, summary="Delete grade")
async def delete_grade(
    grade_id: UUID,
    current_user: AdminUser,
    request: Request,
    service: Annotated[AdminService, Depends(get_admin_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> Response:
    """Delete a grade (classrooms cascade).

    Args:
        grade_id: Grade id.
        current_user: Authenticated admin.
        request: Incoming request (audit provenance).
        service: Admin service.
        audit: Audit service.

    Returns:
        Empty 204 response.
    """
    await service.delete_grade(_school(current_user), grade_id)
    await audit.record_event(
        school_id=_school(current_user),
        principal=current_user,
        request=request,
        action="GRADE_DELETED",
        resource=str(grade_id),
    )
    return Response(status_code=204)


# ── Students ─────────────────────────────────────────────────────────────────


@router.get(
    "/students",
    response_model=Page[StudentResponse],
    summary="List students (paginated, filterable; staff)",
)
async def list_students(
    current_user: StaffUser,
    service: Annotated[AdminService, Depends(get_admin_service)],
    params: Annotated[PageParams, Depends(page_params)],
    q: str | None = None,
    gradeId: str | None = None,
    divisionId: str | None = None,
    status: str | None = None,
) -> Page[StudentResponse]:
    """List students with search/filter/pagination.

    Args:
        current_user: Authenticated staff principal.
        service: Admin service.
        params: Pagination window.
        q: Name/email substring.
        gradeId: Grade filter.
        divisionId: Division filter.
        status: ``active``/``inactive`` filter.

    Returns:
        Paginated students.
    """
    page = await service.list_students(
        _school(current_user),
        params,
        q=q,
        grade_id=gradeId,
        division_id=divisionId,
        status=status,
    )
    return _student_page(page)


@router.post("/students", response_model=StudentResponse, summary="Create student")
async def create_student(
    payload: StudentCreate,
    current_user: AdminUser,
    request: Request,
    service: Annotated[AdminService, Depends(get_admin_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> StudentResponse:
    """Create a student account.

    Args:
        payload: Student fields.
        current_user: Authenticated admin.
        request: Incoming request (audit provenance).
        service: Admin service.
        audit: Audit service.

    Returns:
        The created student.
    """
    student = await service.create_student(_school(current_user), payload)
    await audit.record_event(
        school_id=_school(current_user),
        principal=current_user,
        request=request,
        action="STUDENT_CREATED",
        resource=str(student.id),
        details={"email": student.email, "name": student.name},
    )
    return StudentResponse.model_validate(student)


@router.patch(
    "/students/{student_id}",
    response_model=StudentResponse,
    summary="Update student",
)
async def update_student(
    student_id: UUID,
    payload: StudentUpdate,
    current_user: AdminUser,
    request: Request,
    service: Annotated[AdminService, Depends(get_admin_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> StudentResponse:
    """Apply a partial update to a student.

    Args:
        student_id: Student id.
        payload: Update payload.
        current_user: Authenticated admin.
        request: Incoming request (audit provenance).
        service: Admin service.
        audit: Audit service.

    Returns:
        The updated student.
    """
    student = await service.update_student(_school(current_user), student_id, payload)
    await audit.record_event(
        school_id=_school(current_user),
        principal=current_user,
        request=request,
        action="STUDENT_UPDATED",
        resource=str(student.id),
        details={"fields": sorted(payload.model_dump(exclude_unset=True))},
    )
    return StudentResponse.model_validate(student)


@router.post(
    "/students/bulk-csv",
    response_model=BulkUserCsvResponse,
    summary="Bulk import students via CSV",
    dependencies=[Depends(rate_limit("5/minute"))],
)
async def bulk_students(
    payload: BulkUserCsvRequest,
    current_user: AdminUser,
    request: Request,
    service: Annotated[AdminService, Depends(get_admin_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> BulkUserCsvResponse:
    """Import students from CSV (grade/division by name).

    Args:
        payload: CSV content + dry-run flag.
        current_user: Authenticated admin.
        request: Incoming request (audit provenance).
        service: Admin service.
        audit: Audit service.

    Returns:
        Per-row import outcome.
    """
    from app.domain.enums import UserRole

    result = await service.bulk_import_users(
        _school(current_user),
        payload.csv,
        dry_run=payload.dry_run,
        role=UserRole.STUDENT,
        operation=payload.operation,
        skip_invalid=payload.skip_invalid,
    )
    if not payload.dry_run:
        await audit.record_event(
            school_id=_school(current_user),
            principal=current_user,
            request=request,
            action="STUDENTS_IMPORTED",
            details={
                "operation": payload.operation,
                "created": result.created,
                "updated": result.updated,
                "skipped": result.skipped,
                "invalidRows": result.invalid_rows,
            },
        )
    return result


# ── Teachers ─────────────────────────────────────────────────────────────────


@router.get(
    "/teachers",
    response_model=Page[TeacherResponse],
    summary="List teachers (paginated, filterable)",
)
async def list_teachers(
    current_user: AdminUser,
    service: Annotated[AdminService, Depends(get_admin_service)],
    params: Annotated[PageParams, Depends(page_params)],
    q: str | None = None,
    status: str | None = None,
) -> Page[TeacherResponse]:
    """List teachers with search/filter/pagination.

    Args:
        current_user: Authenticated admin.
        service: Admin service.
        params: Pagination window.
        q: Name/email substring.
        status: ``active``/``inactive`` filter.

    Returns:
        Paginated teachers.
    """
    page = await service.list_teachers(_school(current_user), params, q=q, status=status)
    return Page[TeacherResponse](
        items=[TeacherResponse.model_validate(t) for t in page.items],
        page=page.page,
        page_size=page.page_size,
        total=page.total,
        total_pages=page.total_pages,
    )


@router.post("/teachers", response_model=TeacherCreatedResponse, summary="Create teacher")
async def create_teacher(
    payload: TeacherCreate,
    current_user: AdminUser,
    request: Request,
    service: Annotated[AdminService, Depends(get_admin_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> TeacherCreatedResponse:
    """Create a teacher account.

    Args:
        payload: Teacher fields.
        current_user: Authenticated admin.
        request: Incoming request (audit provenance).
        service: Admin service.
        audit: Audit service.

    Returns:
        The created teacher.
    """
    teacher = await service.create_teacher(_school(current_user), payload)
    await audit.record_event(
        school_id=_school(current_user),
        principal=current_user,
        request=request,
        action="TEACHER_CREATED",
        resource=str(teacher.user.id),
        details={"email": teacher.user.email, "name": teacher.user.name},
    )
    return TeacherCreatedResponse(
        **TeacherResponse.model_validate(teacher.user).model_dump(),
        generated_password=teacher.generated_password,
    )


@router.patch(
    "/teachers/{teacher_id}",
    response_model=TeacherResponse,
    summary="Update teacher",
)
async def update_teacher(
    teacher_id: UUID,
    payload: TeacherUpdate,
    current_user: AdminUser,
    request: Request,
    service: Annotated[AdminService, Depends(get_admin_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> TeacherResponse:
    """Apply a partial update to a teacher.

    Args:
        teacher_id: Teacher id.
        payload: Update payload.
        current_user: Authenticated admin.
        request: Incoming request (audit provenance).
        service: Admin service.
        audit: Audit service.

    Returns:
        The updated teacher.
    """
    teacher = await service.update_teacher(_school(current_user), teacher_id, payload)
    await audit.record_event(
        school_id=_school(current_user),
        principal=current_user,
        request=request,
        action="TEACHER_UPDATED",
        resource=str(teacher.id),
        details={"fields": sorted(payload.model_dump(exclude_unset=True))},
    )
    return TeacherResponse.model_validate(teacher)


@router.post(
    "/users/bulk-csv",
    response_model=BulkUserCsvResponse,
    summary="Bulk import teachers via CSV",
    dependencies=[Depends(rate_limit("5/minute"))],
)
async def bulk_teachers(
    payload: BulkUserCsvRequest,
    current_user: AdminUser,
    request: Request,
    service: Annotated[AdminService, Depends(get_admin_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> BulkUserCsvResponse:
    """Import teachers from CSV.

    Args:
        payload: CSV content + dry-run flag.
        current_user: Authenticated admin.
        request: Incoming request (audit provenance).
        service: Admin service.
        audit: Audit service.

    Returns:
        Per-row import outcome.
    """
    from app.domain.enums import UserRole

    result = await service.bulk_import_users(
        _school(current_user),
        payload.csv,
        dry_run=payload.dry_run,
        role=UserRole.STUDENT if payload.user_type == "student" else UserRole.TEACHER,
        operation=payload.operation,
        skip_invalid=payload.skip_invalid,
    )
    if not payload.dry_run:
        await audit.record_event(
            school_id=_school(current_user),
            principal=current_user,
            request=request,
            action="TEACHERS_IMPORTED",
            details={
                "operation": payload.operation,
                "created": result.created,
                "updated": result.updated,
                "skipped": result.skipped,
                "invalidRows": result.invalid_rows,
            },
        )
    return result


@router.post(
    "/users/{user_id}/status",
    response_model=StudentResponse,
    summary="Set user status",
)
async def set_user_status(
    user_id: UUID,
    payload: UserStatusUpdate,
    current_user: AdminUser,
    request: Request,
    service: Annotated[AdminService, Depends(get_admin_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> StudentResponse:
    """Activate or deactivate any user in the school.

    Args:
        user_id: Target user.
        payload: New status.
        current_user: Authenticated admin.
        request: Incoming request (audit provenance).
        service: Admin service.
        audit: Audit service.

    Returns:
        The updated user (student-shaped projection).
    """
    user = await service.set_user_status(_school(current_user), user_id, payload.is_active)
    await audit.record_event(
        school_id=_school(current_user),
        principal=current_user,
        request=request,
        action="USER_STATUS_CHANGED",
        resource=str(user.id),
        details={"isActive": payload.is_active, "role": str(user.role)},
    )
    return StudentResponse.model_validate(user)


# ── Aggregated payloads ──────────────────────────────────────────────────────


@router.get("/stats", response_model=AdminStatsResponse, summary="Dashboard stats")
async def stats(
    current_user: AdminUser,
    service: Annotated[AdminService, Depends(get_admin_service)],
    schools: Annotated[SchoolRepository, Depends(get_school_repo)],
) -> AdminStatsResponse:
    """Return dashboard counters for the school.

    Args:
        current_user: Authenticated admin.
        service: Admin service.
        schools: School repository.

    Returns:
        Aggregated stats.

    Raises:
        NotFoundError: When the admin's school vanished.
    """
    school = await schools.get(UUID(_school(current_user)))
    if school is None:
        raise NotFoundError("School not found.")
    result = await service.stats(school)
    return AdminStatsResponse(
        total_students=result.total_students,
        total_teachers=result.total_teachers,
        total_classes=result.total_classes,
        total_assignments=result.total_assignments,
        classes_without_teacher=result.classes_without_teacher,
        current_academic_year=result.config.current_academic_year,
        allow_multiple_class_teachers=result.config.allow_multiple_class_teachers,
    )


@router.get(
    "/class-teachers",
    response_model=ClassTeachersResponse,
    summary="Class-teacher assignment payload",
)
async def class_teachers(
    current_user: AdminUser,
    service: Annotated[AdminService, Depends(get_admin_service)],
    schools: Annotated[SchoolRepository, Depends(get_school_repo)],
) -> ClassTeachersResponse:
    """Aggregate classrooms, teachers, and taxonomy options.

    Args:
        current_user: Authenticated admin.
        service: Admin service.
        schools: School repository.

    Returns:
        Aggregated payload.
    """
    school = await schools.get(UUID(_school(current_user)))
    if school is None:
        raise NotFoundError("School not found.")
    payload = await service.class_teachers_payload(school)
    return ClassTeachersResponse(
        config=payload.config,
        classrooms=[ClassroomResponse.model_validate(c) for c in payload.classrooms],
        teachers=[UserSummary.model_validate(t) for t in payload.teachers],
        grades=[GradeResponse.model_validate(g) for g in payload.grades],
        divisions=[DivisionResponse.model_validate(d) for d in payload.divisions],
    )


@router.get(
    "/school/setup",
    response_model=SchoolSetupResponse,
    summary="School-setup payload",
)
async def school_setup(
    current_user: AdminUser,
    service: Annotated[AdminService, Depends(get_admin_service)],
    schools: Annotated[SchoolRepository, Depends(get_school_repo)],
) -> SchoolSetupResponse:
    """Aggregate school settings with per-grade/division student counts.

    Args:
        current_user: Authenticated admin.
        service: Admin service.
        schools: School repository.

    Returns:
        Aggregated payload.
    """
    school = await schools.get(UUID(_school(current_user)))
    if school is None:
        raise NotFoundError("School not found.")
    payload = await service.school_setup_payload(school)
    return SchoolSetupResponse(
        school=SchoolSettingsResponse.model_validate(payload.school),
        grades=[
            GradeWithStudentCount(grade=GradeResponse.model_validate(grade), student_count=count)
            for grade, count in payload.grades
        ],
        divisions=[
            DivisionWithStudentCount(
                division=DivisionResponse.model_validate(division),
                student_count=count,
            )
            for division, count in payload.divisions
        ],
    )


@router.get("/audit", response_model=Page[AuditLogResponse], summary="Audit trail")
async def audit_list(
    current_user: AdminUser,
    service: Annotated[AuditService, Depends(get_audit_service)],
    users: Annotated[UserRepository, Depends(get_user_repo)],
    params: Annotated[PageParams, Depends(page_params)],
    action: str | None = None,
    userId: str | None = None,
) -> Page[AuditLogResponse]:
    """List the school's audit events newest-first, with actor names resolved.

    Args:
        current_user: Authenticated admin.
        service: Audit service.
        users: User repository (actor display resolution).
        params: Pagination window.
        action: Exact-action filter.
        userId: Acting-user filter.

    Returns:
        Paginated audit logs.
    """
    page = await service.list_events(_school(current_user), params, action=action, user_id=userId)
    # Resolve acting users in one query; audit rows keep no FK so names must
    # be joined at read time (and may be gone after user deletion).
    actor_ids: list[UUID] = []
    for log in page.items:
        if log.user_id:
            try:
                actor_ids.append(UUID(log.user_id))
            except ValueError:
                continue
    actors = {str(u.id): u for u in await users.get_by_ids(actor_ids)}
    items: list[AuditLogResponse] = []
    for log in page.items:
        item = AuditLogResponse.model_validate(log)
        actor = actors.get(log.user_id or "")
        if actor is not None:
            item.user_name = actor.name
            item.user_email = actor.email
        items.append(item)
    return Page[AuditLogResponse](
        items=items,
        page=page.page,
        page_size=page.page_size,
        total=page.total,
        total_pages=page.total_pages,
    )


@router.get(
    "/audit/download",
    response_model=AuditDownloadResponse,
    summary="Download an archived audit file",
)
async def audit_download(
    key: str,
    current_user: AdminUser,
    service: Annotated[AuditService, Depends(get_audit_service)],
) -> AuditDownloadResponse:
    """Issue a pre-signed download for an archived audit file.

    Args:
        key: Archive object key.
        current_user: Authenticated admin.
        service: Audit service.

    Returns:
        Download grant.
    """
    key_out, url, expires_in = await service.download_archive(_school(current_user), key=key)
    return AuditDownloadResponse(key=key_out, url=url, expires_in=expires_in)


# ── helpers ──────────────────────────────────────────────────────────────────


def _school(principal: object) -> str:
    """Extract the caller's school id, failing closed.

    Args:
        principal: Authenticated principal.

    Returns:
        School id string.

    Raises:
        ForbiddenError: When no school context exists.
    """
    school_id = getattr(principal, "school_id", None)
    if not school_id:
        raise ForbiddenError("Caller has no school context.")
    return str(school_id)


def _student_page(page: Page[User]) -> Page[StudentResponse]:
    """Map a user page onto the student response schema.

    Args:
        page: Raw user page.

    Returns:
        Mapped page.
    """
    return Page[StudentResponse](
        items=[StudentResponse.model_validate(s) for s in page.items],
        page=page.page,
        page_size=page.page_size,
        total=page.total,
        total_pages=page.total_pages,
    )


@router.get("/school/settings", response_model=SchoolSettingsResponse)
async def get_school_settings(
    current_user: AdminUser,
    service: Annotated[SchoolService, Depends(get_school_service)],
) -> SchoolSettingsResponse:
    """Read the administrator's own school settings."""
    return SchoolSettingsResponse.model_validate(await service.get_settings(_school(current_user)))


@router.patch("/school/settings", response_model=SchoolSettingsResponse)
async def patch_school_settings(
    payload: SchoolSettingsUpdate,
    current_user: AdminUser,
    request: Request,
    service: Annotated[SchoolService, Depends(get_school_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> SchoolSettingsResponse:
    """Update the administrator's own school and feature configuration.

    Args:
        payload: Settings patch.
        current_user: Authenticated admin.
        request: Incoming request (audit provenance).
        service: School service.
        audit: Audit service.

    Returns:
        The updated settings.
    """
    school = await service.update_settings(_school(current_user), payload)
    await audit.record_event(
        school_id=_school(current_user),
        principal=current_user,
        request=request,
        action="SCHOOL_SETTINGS_UPDATED",
        resource=str(school.id),
        details={"fields": sorted(payload.model_dump(exclude_unset=True))},
    )
    return SchoolSettingsResponse.model_validate(school)


@router.get("/user-counts", response_model=UserCountsResponse)
async def user_counts(
    current_user: AdminUser,
    service: Annotated[AdminService, Depends(get_admin_service)],
    role: str = "STUDENT",
) -> UserCountsResponse:
    """Return activation counters independent of list filters and pagination."""
    active, inactive = await service.user_counts(_school(current_user), role)
    return UserCountsResponse(active=active, inactive=inactive)


@router.get("/audit/archives", response_model=list[AuditArchiveResponse])
async def audit_archives(
    current_user: AdminUser,
    service: Annotated[AuditService, Depends(get_audit_service)],
) -> list[AuditArchiveResponse]:
    """List archived audit objects belonging to the administrator's school."""
    return [
        AuditArchiveResponse(key=row.key, size=row.size, last_modified=row.last_modified)
        for row in await service.list_archives(_school(current_user))
    ]
