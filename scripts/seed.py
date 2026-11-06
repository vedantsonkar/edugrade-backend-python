"""Idempotent development seeder.

Creates a demo school (with config, grades, divisions, one classroom), an
admin, a teacher, two students, and one online assignment with questions —
skipping any step whose anchor already exists. Run via ``make seed``.

Usage:
    python scripts/seed.py
"""

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.core.db.engine import build_engine, build_sessionmaker
from app.core.security.passwords import BcryptPasswordHasher
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

SEED_SUBDOMAIN = "demo-school"


async def seed() -> None:
    """Run the idempotent seed."""
    settings = get_settings()
    engine = build_engine(settings)
    sessionmaker = build_sessionmaker(engine)
    hasher = BcryptPasswordHasher()

    async with sessionmaker() as session:
        from sqlalchemy import select

        existing = (
            await session.execute(
                select(School).where(School.subdomain == SEED_SUBDOMAIN)
            )
        ).scalar_one_or_none()
        if existing is not None:
            print(f"Seed data already present (school subdomain={SEED_SUBDOMAIN}).")
            await session.rollback()
            return

        school = School(
            name="Demo Academy",
            subdomain=SEED_SUBDOMAIN,
            admin_key="demo-admin-key",
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

        classroom = Classroom(
            school_id=str(school.id),
            grade_id=str(grade9.id),
            division_id=str(division_a.id),
            academic_year="2025-2026",
        )
        session.add(classroom)

        admin = User(
            email="admin@demo-school.edu",
            name="Ada Admin",
            role=UserRole.ADMIN,
            school_id=str(school.id),
            password_hash=hasher.hash("Admin@123"),
            auth_method=AuthMethod.CREDENTIALS,
        )
        teacher = User(
            email="teacher@demo-school.edu",
            name="Tara Teacher",
            role=UserRole.TEACHER,
            school_id=str(school.id),
            password_hash=hasher.hash("Teacher@123"),
            auth_method=AuthMethod.CREDENTIALS,
        )
        student1 = User(
            email="student1@demo-school.edu",
            name="Sam Student",
            role=UserRole.STUDENT,
            school_id=str(school.id),
            grade_id=str(grade9.id),
            division_id=str(division_a.id),
            password_hash=hasher.hash("20090101"),
            auth_method=AuthMethod.CREDENTIALS,
            date_of_birth=datetime(2009, 1, 1, tzinfo=UTC),
        )
        student2 = User(
            email="student2@demo-school.edu",
            name="Sara Student",
            role=UserRole.STUDENT,
            school_id=str(school.id),
            grade_id=str(grade9.id),
            division_id=str(division_a.id),
            password_hash=hasher.hash("20090102"),
            auth_method=AuthMethod.CREDENTIALS,
            date_of_birth=datetime(2009, 1, 2, tzinfo=UTC),
        )
        session.add_all([admin, teacher, student1, student2])
        await session.flush()

        now = datetime.now(UTC)
        assignment = Assignment(
            school_id=str(school.id),
            teacher_id=str(teacher.id),
            type=AssignmentType.ONLINE,
            title="Photosynthesis Basics",
            description="A quick check on photosynthesis.",
            subject="Biology",
            start_date=now - timedelta(hours=1),
            due_date=now + timedelta(days=7),
            time_limit=30,
        )
        session.add(assignment)
        await session.flush()

        session.add(
            AssignmentTarget(assignment_id=str(assignment.id), class_id=str(classroom.id))
        )

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
                    question_id=str(q1.id), text="Chlorophyll", is_correct=True,
                    display_order=1,
                ),
                QuestionOption(
                    question_id=str(q1.id), text="Melanin", is_correct=False,
                    display_order=2,
                ),
                QuestionOption(
                    question_id=str(q1.id), text="Hemoglobin", is_correct=False,
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

        await session.commit()
        print("Seed complete:")
        print(f"  school      : {school.name} (subdomain={school.subdomain})")
        print("  admin       : admin@demo-school.edu / Admin@123")
        print("  teacher     : teacher@demo-school.edu / Teacher@123")
        print("  students    : student1@demo-school.edu / 20090101")
        print("                student2@demo-school.edu / 20090102")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
