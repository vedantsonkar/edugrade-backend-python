"""Idempotent development seeder.

Creates two isolated demo schools, each with config, taxonomy, a classroom,
an admin, a teacher, two students, and a quiz. Existing schools are skipped
independently, preserving their users and data. Run via ``make seed``.

Usage:
    python scripts/seed.py
"""

import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.core.db.engine import build_engine, build_sessionmaker
from app.core.security.passwords import BcryptPasswordHasher, PasswordHasher
from app.domain.assignments import (
    Assignment,
    AssignmentTarget,
    Question,
    QuestionOption,
)
from app.domain.enums import AssignmentType, AuthMethod, QuestionType, UserRole
from app.domain.schools import School, SchoolConfig
from app.domain.taxonomy import Classroom, Division, Grade
from app.domain.users import User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class DemoSchoolSeed:
    """Public development tenant identity and its sample assignment title."""

    name: str
    school_key: str
    admin_key: str
    primary_color: str
    assignment_title: str


@dataclass(frozen=True)
class DemoUserSeed:
    """Development account template, with email scoped to its school's domain."""

    email_prefix: str
    name: str
    role: UserRole
    password: str
    date_of_birth: datetime | None = None


DEMO_SCHOOLS = (
    DemoSchoolSeed(
        name="Demo Academy",
        school_key="demo-school",
        admin_key="demo-admin-key",
        primary_color="#3b82f6",
        assignment_title="Photosynthesis Basics",
    ),
    DemoSchoolSeed(
        name="Demo Academy 2",
        school_key="demo-school-2",
        admin_key="demo-admin-key-2",
        primary_color="#059669",
        assignment_title="Photosynthesis Basics (School 2)",
    ),
)

DEMO_USERS = (
    DemoUserSeed("admin", "Ada Admin", UserRole.ADMIN, "Admin@123"),
    DemoUserSeed("teacher", "Tara Teacher", UserRole.TEACHER, "Teacher@123"),
    DemoUserSeed(
        "student1",
        "Sam Student",
        UserRole.STUDENT,
        "20090101",
        datetime(2009, 1, 1, tzinfo=UTC),
    ),
    DemoUserSeed(
        "student2",
        "Sara Student",
        UserRole.STUDENT,
        "20090102",
        datetime(2009, 1, 2, tzinfo=UTC),
    ),
)


async def seed() -> None:
    """Seed both tenants in one transaction and release the database engine."""
    settings = get_settings()
    engine = build_engine(settings)
    sessionmaker = build_sessionmaker(engine)
    hasher = BcryptPasswordHasher()

    try:
        async with sessionmaker() as session:
            created = await seed_demo_schools(session, hasher, now=datetime.now(UTC))
            await session.commit()
        for definition in DEMO_SCHOOLS:
            status = "Created" if definition.school_key in created else "Already present; unchanged"
            print(f"{status}: {definition.name} (school key={definition.school_key})")
            print(f"  admin key   : {definition.admin_key} (seed default)")
            for account in DEMO_USERS:
                print(
                    f"  {account.email_prefix:12}: "
                    f"{account.email_prefix}@{definition.school_key}.edu / {account.password}"
                    " (seed default)"
                )
    finally:
        await engine.dispose()


async def seed_demo_schools(
    session: AsyncSession, hasher: PasswordHasher, *, now: datetime
) -> list[str]:
    """Create missing demo tenants without changing any existing school.

    Args:
        session: Caller-owned transaction; this function does not commit.
        hasher: Password hashing adapter for development credentials.
        now: Timezone-aware reference time for the quizzes' open window.

    Returns:
        School keys created in this transaction.
    """
    created: list[str] = []
    for definition in DEMO_SCHOOLS:
        existing = (
            await session.execute(
                select(School).where(School.school_key == definition.school_key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        await _create_demo_school(session, definition, hasher, now)
        created.append(definition.school_key)
    return created


async def _create_demo_school(
    session: AsyncSession, definition: DemoSchoolSeed, hasher: PasswordHasher, now: datetime
) -> None:
    """Build a tenant using only taxonomy and users from that tenant."""
    school = School(
        name=definition.name,
        school_key=definition.school_key,
        admin_key=definition.admin_key,
        primary_color=definition.primary_color,
    )
    session.add(school)
    await session.flush()

    session.add(
        SchoolConfig(
            school_id=str(school.id),
            enable_logging=True,
            current_academic_year="2025-2026",
        )
    )

    grade9 = Grade(school_id=str(school.id), name="Grade 9", display_order=9)
    grade10 = Grade(school_id=str(school.id), name="Grade 10", display_order=10)
    session.add_all([grade9, grade10])
    await session.flush()

    division_a = Division(school_id=str(school.id), name="A")
    division_b = Division(school_id=str(school.id), name="B")
    session.add_all([division_a, division_b])
    await session.flush()

    accounts: dict[str, User] = {}
    for account in DEMO_USERS:
        student = account.role == UserRole.STUDENT
        user = User(
            email=f"{account.email_prefix}@{definition.school_key}.edu",
            name=account.name,
            role=account.role,
            school_id=str(school.id),
            grade_id=str(grade9.id) if student else None,
            division_id=str(division_a.id) if student else None,
            password_hash=hasher.hash(account.password),
            auth_method=AuthMethod.CREDENTIALS,
            date_of_birth=account.date_of_birth,
        )
        session.add(user)
        accounts[account.email_prefix] = user
    await session.flush()

    classroom = Classroom(
        school_id=str(school.id),
        grade_id=str(grade9.id),
        division_id=str(division_a.id),
        class_teacher_id=str(accounts["teacher"].id),
        academic_year="2025-2026",
    )
    session.add(classroom)
    await session.flush()

    assignment = Assignment(
        school_id=str(school.id),
        teacher_id=str(accounts["teacher"].id),
        type=AssignmentType.ONLINE,
        title=definition.assignment_title,
        description="A quick check on photosynthesis.",
        subject="Biology",
        start_date=now - timedelta(hours=1),
        due_date=now + timedelta(days=7),
        time_limit=30,
    )
    session.add(assignment)
    await session.flush()

    session.add(AssignmentTarget(assignment_id=str(assignment.id), class_id=str(classroom.id)))

    q1 = Question(
        assignment_id=str(assignment.id),
        type=QuestionType.MCQ_SINGLE,
        text="Which pigment absorbs light in photosynthesis?",
        points=2,
        display_order=1,
        explanation="Chlorophyll is the primary light-absorbing pigment.",
    )
    session.add(q1)
    await session.flush()
    session.add_all(
        [
            QuestionOption(
                question_id=str(q1.id),
                text="Chlorophyll",
                is_correct=True,
                display_order=1,
            ),
            QuestionOption(
                question_id=str(q1.id),
                text="Melanin",
                is_correct=False,
                display_order=2,
            ),
            QuestionOption(
                question_id=str(q1.id),
                text="Hemoglobin",
                is_correct=False,
                display_order=3,
            ),
        ]
    )

    q2 = Question(
        assignment_id=str(assignment.id),
        type=QuestionType.FREE_TEXT,
        text="Explain, in two sentences, why plants need sunlight.",
        points=5,
        display_order=2,
    )
    session.add(q2)

    await session.flush()


if __name__ == "__main__":
    asyncio.run(seed())
