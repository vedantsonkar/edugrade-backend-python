"""Idempotent development seeder.

Creates two isolated UAT schools with multiple classrooms, staff, students,
and a mix of online assessments and offline assignments. Existing schools are skipped
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
    label: str


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
        "Riverdale Public School",
        "riverdale-uat",
        "riverdale-uat-admin-key",
        "#3b82f6",
        "Riverdale",
    ),
    DemoSchoolSeed(
        "Greenfield International School",
        "greenfield-uat",
        "greenfield-uat-admin-key",
        "#059669",
        "Greenfield",
    ),
)

# Every classroom has a separate teacher and four students. Labels in display
# names make both tenant and classroom boundaries visible in the interface.
CLASSES = ((8, "A"), (8, "B"), (9, "A"), (9, "B"))
TEACHER_NAMES = ("Ananya Sharma", "Vikram Mehta", "Priya Nair", "Rohan Kapoor")
STUDENT_NAMES = (
    ("Aarav Patel", "Diya Shah", "Kabir Gupta", "Meera Rao"),
    ("Ishaan Joshi", "Anika Iyer", "Reyansh Das", "Tara Malhotra"),
    ("Arjun Singh", "Saanvi Verma", "Aditya Menon", "Riya Desai"),
    ("Dev Khanna", "Myra Sethi", "Kunal Bhat", "Nisha Jain"),
)


def demo_users(definition: DemoSchoolSeed) -> tuple[DemoUserSeed, ...]:
    """Return predictable login credentials and labeled names for a school."""
    users = [
        DemoUserSeed("admin", f"Kavita Rao ({definition.label} Admin)", UserRole.ADMIN, "Admin@123")
    ]
    for index, (grade, division) in enumerate(CLASSES):
        class_label = f"{definition.label} {grade}{division}"
        users.append(
            DemoUserSeed(
                f"teacher{grade}{division.lower()}",
                f"{TEACHER_NAMES[index]} ({class_label})",
                UserRole.TEACHER,
                "Teacher@123",
            )
        )
        for number, name in enumerate(STUDENT_NAMES[index], start=1):
            dob = datetime(2011 if grade == 9 else 2012, index + 1, number, tzinfo=UTC)
            users.append(
                DemoUserSeed(
                    f"student{grade}{division.lower()}.{number}",
                    f"{name} ({class_label})",
                    UserRole.STUDENT,
                    f"Student@{grade}{division.lower()}{number}",
                    dob,
                )
            )
    return tuple(users)


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
            for account in demo_users(definition):
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
            await session.execute(select(School).where(School.school_key == definition.school_key))
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
            current_academic_year=f"{now.year}-{now.year + 1}",
        )
    )

    grades = {
        number: Grade(school_id=str(school.id), name=f"Grade {number}", display_order=number)
        for number in (8, 9)
    }
    divisions = {name: Division(school_id=str(school.id), name=name) for name in ("A", "B")}
    session.add_all([*grades.values(), *divisions.values()])
    await session.flush()

    accounts: dict[str, User] = {}
    for account in demo_users(definition):
        class_match = next(
            (
                (grade, division)
                for grade, division in CLASSES
                if account.email_prefix.startswith(f"student{grade}{division.lower()}.")
            ),
            None,
        )
        user = User(
            email=f"{account.email_prefix}@{definition.school_key}.edu",
            name=account.name,
            role=account.role,
            school_id=str(school.id),
            grade_id=str(grades[class_match[0]].id) if class_match else None,
            division_id=str(divisions[class_match[1]].id) if class_match else None,
            password_hash=hasher.hash(account.password),
            auth_method=AuthMethod.CREDENTIALS,
            date_of_birth=account.date_of_birth,
        )
        session.add(user)
        accounts[account.email_prefix] = user
    await session.flush()

    academic_year = f"{now.year}-{now.year + 1}"
    for grade, division in CLASSES:
        teacher = accounts[f"teacher{grade}{division.lower()}"]
        classroom = Classroom(
            school_id=str(school.id),
            grade_id=str(grades[grade].id),
            division_id=str(divisions[division].id),
            class_teacher_id=str(teacher.id),
            academic_year=academic_year,
        )
        session.add(classroom)
        await session.flush()
        label = f"{definition.label} {grade}{division}"
        topics = (
            (
                "Science",
                "Photosynthesis and plant nutrition",
                "Explain how leaves make food and why light is essential.",
            ),
            (
                "Mathematics",
                "Linear equations in everyday life",
                "Show your working for three real-world linear equation problems.",
            ),
        )
        for index, (subject, title, description) in enumerate(topics):
            online = index == 0
            assignment = Assignment(
                school_id=str(school.id),
                teacher_id=str(teacher.id),
                type=AssignmentType.ONLINE if online else AssignmentType.OFFLINE,
                title=f"{title} — {label}",
                description=description,
                subject=subject,
                start_date=now - timedelta(days=2),
                due_date=now + timedelta(days=7 + index * 3),
                time_limit=30 if online else None,
                auto_grade_on_due_date=False,
            )
            session.add(assignment)
            await session.flush()
            session.add(
                AssignmentTarget(assignment_id=str(assignment.id), class_id=str(classroom.id))
            )
            if online:
                question = Question(
                    assignment_id=str(assignment.id),
                    type=QuestionType.MCQ_SINGLE,
                    text="Which pigment absorbs light for photosynthesis?",
                    points=2,
                    display_order=1,
                    explanation="Chlorophyll absorbs light energy.",
                )
                session.add(question)
                await session.flush()
                session.add_all(
                    [
                        QuestionOption(
                            question_id=str(question.id),
                            text=text,
                            is_correct=correct,
                            display_order=order,
                        )
                        for order, (text, correct) in enumerate(
                            (
                                ("Chlorophyll", True),
                                ("Melanin", False),
                                ("Haemoglobin", False),
                                ("Keratin", False),
                            ),
                            start=1,
                        )
                    ]
                )
                session.add(
                    Question(
                        assignment_id=str(assignment.id),
                        type=QuestionType.FREE_TEXT,
                        text="Describe how a plant uses sunlight to make food.",
                        points=5,
                        display_order=2,
                    )
                )
    await session.flush()


if __name__ == "__main__":
    asyncio.run(seed())
