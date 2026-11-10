"""Exercise UAT seed completeness, repeatability and isolation."""

from datetime import datetime
from typing import cast

import pytest
from app.core.db.engine import session_scope
from app.core.security.passwords import BcryptPasswordHasher
from app.domain.assignments import Assignment, AssignmentTarget, Question
from app.domain.enums import UserRole
from app.domain.schools import School
from app.domain.taxonomy import Classroom
from app.domain.users import User
from fastapi.testclient import TestClient
from scripts.seed import CLASSES, DEMO_SCHOOLS, demo_users, seed_demo_schools
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.fakes import FakeClock


@pytest.fixture
async def demo_tenants(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    hasher: BcryptPasswordHasher,
    clock: FakeClock,
) -> dict[str, dict[str, str]]:
    async with session_scope(db_sessionmaker) as session:
        assert await seed_demo_schools(session, hasher, now=cast(datetime, clock.now())) == [
            school.school_key for school in DEMO_SCHOOLS
        ]
        result: dict[str, dict[str, str]] = {}
        for school in (await session.scalars(select(School))).all():
            users = (await session.scalars(select(User).where(User.school_id == school.id))).all()
            assignments = (
                await session.scalars(select(Assignment).where(Assignment.school_id == school.id))
            ).all()
            result[school.school_key] = {
                "school": str(school.id),
                **{u.email.split("@")[0]: str(u.id) for u in users},
                **{a.title: str(a.id) for a in assignments},
            }
        return result


async def test_seed_structure_and_rerun_preserve_data(
    demo_tenants: dict[str, dict[str, str]],
    db_sessionmaker: async_sessionmaker[AsyncSession],
    hasher: BcryptPasswordHasher,
    clock: FakeClock,
) -> None:
    async with session_scope(db_sessionmaker) as session:
        for definition in DEMO_SCHOOLS:
            school_id = demo_tenants[definition.school_key]["school"]
            users = (await session.scalars(select(User).where(User.school_id == school_id))).all()
            classes = (
                await session.scalars(select(Classroom).where(Classroom.school_id == school_id))
            ).all()
            assignments = (
                await session.scalars(select(Assignment).where(Assignment.school_id == school_id))
            ).all()
            assert len(users) == 21 and len(classes) == 4 and len(assignments) == 8
            assert all(definition.label in u.name for u in users)
            for classroom in classes:
                students = [
                    u
                    for u in users
                    if u.role == UserRole.STUDENT
                    and u.grade_id == classroom.grade_id
                    and u.division_id == classroom.division_id
                ]
                assert len(students) == 4
                assert any(
                    str(u.id) == str(classroom.class_teacher_id) and u.role == UserRole.TEACHER
                    for u in users
                )
                targets = (
                    await session.scalars(
                        select(AssignmentTarget).where(AssignmentTarget.class_id == classroom.id)
                    )
                ).all()
                assert len(targets) == 2
                assert all(
                    any(str(a.id) == str(t.assignment_id) for a in assignments) for t in targets
                )
            assert all(definition.label in a.title for a in assignments)
            assert (
                len(
                    (
                        await session.scalars(
                            select(Question).where(
                                Question.assignment_id.in_([a.id for a in assignments])
                            )
                        )
                    ).all()
                )
                == 8
            )
        user = (
            await session.scalars(select(User).where(User.email == "admin@riverdale-uat.edu"))
        ).one()
        user.name = "Edited admin"
        user.password_hash = hasher.hash("Changed@123")
    async with session_scope(db_sessionmaker) as session:
        assert await seed_demo_schools(session, hasher, now=cast(datetime, clock.now())) == []
        user = (
            await session.scalars(select(User).where(User.email == "admin@riverdale-uat.edu"))
        ).one()
        assert user.name == "Edited admin" and hasher.verify(
            "Changed@123", user.password_hash or ""
        )
        assert len((await session.scalars(select(User))).all()) == 42


def test_seeded_students_see_only_their_class_and_foreign_school_is_hidden(
    client: TestClient,
    demo_tenants: dict[str, dict[str, str]],
) -> None:
    for definition in DEMO_SCHOOLS:
        own = demo_tenants[definition.school_key]
        foreign = next(value for key, value in demo_tenants.items() if key != definition.school_key)
        for grade, division in CLASSES:
            account = next(
                u
                for u in demo_users(definition)
                if u.email_prefix == f"student{grade}{division.lower()}.1"
            )
            response = client.post(
                "/api/v1/auth/login",
                json={
                    "email": f"{account.email_prefix}@{definition.school_key}.edu",
                    "password": account.password,
                    "loginType": "student",
                    "schoolId": own["school"],
                },
            )
            assert response.status_code == 200, response.text
            items = client.get("/api/v1/assignments").json()["items"]
            assert len(items) == 2
            assert all(f"{definition.label} {grade}{division}" in item["title"] for item in items)
            foreign_assignment = next(
                value for key, value in foreign.items() if "Photosynthesis" in key
            )
            assert client.get(f"/api/v1/assignments/{foreign_assignment}").status_code == 404
            assert client.post("/api/v1/auth/logout", json={}).status_code == 200


def test_seeded_staff_logins(client: TestClient, demo_tenants: dict[str, dict[str, str]]) -> None:
    for definition in DEMO_SCHOOLS:
        for account in demo_users(definition):
            if account.role == UserRole.STUDENT:
                continue
            response = client.post(
                "/api/v1/auth/login",
                json={
                    "email": f"{account.email_prefix}@{definition.school_key}.edu",
                    "password": account.password,
                    "loginType": "admin",
                    "schoolId": demo_tenants[definition.school_key]["school"],
                    "adminKey": definition.admin_key,
                },
            )
            assert response.status_code == 200, response.text
            assert client.post("/api/v1/auth/logout", json={}).status_code == 200


async def test_existing_school_does_not_prevent_creating_other_school(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    hasher: BcryptPasswordHasher,
    clock: FakeClock,
) -> None:
    async with session_scope(db_sessionmaker) as session:
        session.add(School(name="Existing school", school_key="riverdale-uat", admin_key="changed"))
    async with session_scope(db_sessionmaker) as session:
        assert await seed_demo_schools(session, hasher, now=cast(datetime, clock.now())) == [
            "greenfield-uat"
        ]
        school = (
            await session.scalars(select(School).where(School.school_key == "riverdale-uat"))
        ).one()
        assert school.name == "Existing school" and school.admin_key == "changed"


def test_staff_keys_are_school_scoped(
    client: TestClient,
    demo_tenants: dict[str, dict[str, str]],
) -> None:
    for definition in DEMO_SCHOOLS:
        foreign = next(value for key, value in demo_tenants.items() if key != definition.school_key)
        response = client.post(
            "/api/v1/school/validate-admin",
            json={
                "schoolId": foreign["school"],
                "adminKey": definition.admin_key,
            },
        )
        assert response.status_code == 200 and response.json()["valid"] is False
