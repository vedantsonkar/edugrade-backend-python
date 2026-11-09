"""Exercise the actual demo seeder and isolation between its two tenants."""

from datetime import datetime, timedelta
from typing import cast

import pytest
from app.core.db.engine import session_scope
from app.core.security.passwords import BcryptPasswordHasher
from app.domain.assignments import Assignment
from app.domain.enums import AuthMethod, UserRole
from app.domain.schools import School
from app.domain.taxonomy import Classroom
from app.domain.users import User
from fastapi.testclient import TestClient
from scripts.seed import DEMO_SCHOOLS, DEMO_USERS, DemoUserSeed, seed_demo_schools
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.fakes import FakeClock


@pytest.fixture
async def demo_tenants(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    hasher: BcryptPasswordHasher,
    clock: FakeClock,
) -> dict[str, dict[str, str]]:
    """Seed the production demo templates inside the isolated test database."""
    async with session_scope(db_sessionmaker) as session:
        created = await seed_demo_schools(session, hasher, now=cast(datetime, clock.now()))
        assert created == [definition.subdomain for definition in DEMO_SCHOOLS]
        schools = (await session.scalars(select(School))).all()
        tenants: dict[str, dict[str, str]] = {}
        for school in schools:
            users = (await session.scalars(select(User).where(User.school_id == school.id))).all()
            classroom = (
                await session.scalars(select(Classroom).where(Classroom.school_id == school.id))
            ).one()
            assignment = (
                await session.scalars(select(Assignment).where(Assignment.school_id == school.id))
            ).one()
            tenants[school.subdomain] = {
                "school": str(school.id),
                "class": str(classroom.id),
                "assignment": str(assignment.id),
                **{user.email.split("@")[0]: str(user.id) for user in users},
            }
        return tenants


async def test_seed_rerun_preserves_users_and_does_not_duplicate_data(
    demo_tenants: dict[str, dict[str, str]],
    db_sessionmaker: async_sessionmaker[AsyncSession],
    hasher: BcryptPasswordHasher,
    clock: FakeClock,
) -> None:
    """Reseeding preserves edited passwords, user IDs, and tenant data counts."""
    async with session_scope(db_sessionmaker) as session:
        user = (
            await session.scalars(select(User).where(User.email == "student1@demo-school.edu"))
        ).one()
        user.name = "Edited Student"
        user.password_hash = hasher.hash("Changed@123")
    async with session_scope(db_sessionmaker) as session:
        assert await seed_demo_schools(session, hasher, now=cast(datetime, clock.now())) == []
        assert len((await session.scalars(select(School))).all()) == 2
        users = (await session.scalars(select(User))).all()
        assert len(users) == 8
        assert len((await session.scalars(select(Classroom))).all()) == 2
        assert len((await session.scalars(select(Assignment))).all()) == 2
        user = next(user for user in users if user.email == "student1@demo-school.edu")
        assert str(user.id) == demo_tenants["demo-school"]["student1"]
        assert user.name == "Edited Student"
        assert user.password_hash is not None
        assert hasher.verify("Changed@123", user.password_hash)
        for definition in DEMO_SCHOOLS:
            classroom = (
                await session.scalars(
                    select(Classroom).where(
                        Classroom.school_id == demo_tenants[definition.subdomain]["school"]
                    )
                )
            ).one()
            assert str(classroom.class_teacher_id) == demo_tenants[definition.subdomain]["teacher"]


async def test_seed_adds_second_school_when_first_already_exists(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    hasher: BcryptPasswordHasher,
    clock: FakeClock,
) -> None:
    """An existing first tenant must not cause the entire seed command to exit."""
    async with session_scope(db_sessionmaker) as session:
        school = School(name="Existing Academy", subdomain="demo-school", admin_key="changed-key")
        session.add(school)
        await session.flush()
        school_id = str(school.id)
        session.add(
            User(
                email="admin@demo-school.edu",
                name="Existing Admin",
                role=UserRole.ADMIN,
                school_id=school_id,
                auth_method=AuthMethod.CREDENTIALS,
                password_hash=hasher.hash("Changed@123"),
            )
        )
    async with session_scope(db_sessionmaker) as session:
        created = await seed_demo_schools(session, hasher, now=cast(datetime, clock.now()))
        assert created == ["demo-school-2"]
        schools = (await session.scalars(select(School))).all()
        assert len(schools) == 2
        original = next(school for school in schools if school.subdomain == "demo-school")
        assert str(original.id) == school_id
        assert original.name == "Existing Academy"
        assert original.admin_key == "changed-key"
        original_user = (
            await session.scalars(select(User).where(User.school_id == school_id))
        ).one()
        assert original_user.password_hash is not None
        assert hasher.verify("Changed@123", original_user.password_hash)
        assert len((await session.scalars(select(User))).all()) == 5


@pytest.mark.parametrize("account", DEMO_USERS, ids=lambda user: user.email_prefix)
def test_demo_logins_and_assignment_reads_are_tenant_scoped(
    client: TestClient,
    demo_tenants: dict[str, dict[str, str]],
    account: DemoUserSeed,
) -> None:
    """Every seeded login sees its own quiz and cannot access a foreign quiz."""
    for definition in DEMO_SCHOOLS:
        own = demo_tenants[definition.subdomain]
        foreign = next(
            tenant for name, tenant in demo_tenants.items() if name != definition.subdomain
        )
        credentials = {
            "email": f"{account.email_prefix}@{definition.subdomain}.edu",
            "password": account.password,
            "loginType": "student" if account.role == UserRole.STUDENT else "admin",
            "schoolId": own["school"],
            **(
                {"adminKey": definition.admin_key}
                if account.role != UserRole.STUDENT
                else {}
            ),
        }
        response = client.post("/api/v1/auth/login", json=credentials)
        assert response.status_code == 200, response.text
        assert response.json()["school"]["id"] == own["school"]
        assert response.json()["user"]["id"] == own[account.email_prefix]
        listed = client.get("/api/v1/assignments")
        assert listed.status_code == 200, listed.text
        assert [item["id"] for item in listed.json()["items"]] == [own["assignment"]]
        for suffix in ("", "/questions", "/submissions", "/attempts"):
            response = client.get(f"/api/v1/assignments/{foreign['assignment']}{suffix}")
            assert response.status_code == 404, response.text
        denied = client.post(
            "/api/v1/auth/login", json={**credentials, "schoolId": foreign["school"]}
        )
        assert denied.status_code == 409, denied.text
        if account.role == UserRole.ADMIN:
            students = client.get("/api/v1/admin/students").json()
            assert students["total"] == 2
            assert {item["id"] for item in students["items"]} == {own["student1"], own["student2"]}
        assert client.post("/api/v1/auth/logout", json={}).status_code == 200


def test_demo_admin_keys_are_scoped_to_selected_school(
    client: TestClient, demo_tenants: dict[str, dict[str, str]]
) -> None:
    """A valid key for one school does not unlock the other school's staff tab."""
    for definition in DEMO_SCHOOLS:
        foreign = next(
            tenant for name, tenant in demo_tenants.items() if name != definition.subdomain
        )
        for school_id, expected in (
            (demo_tenants[definition.subdomain]["school"], True),
            (foreign["school"], False),
        ):
            response = client.post(
                "/api/v1/school/validate-admin",
                json={"schoolId": school_id, "adminKey": definition.admin_key},
            )
            assert response.status_code == 200, response.text
            assert response.json()["valid"] is expected


def test_demo_teacher_cannot_target_foreign_classroom(
    client: TestClient, demo_tenants: dict[str, dict[str, str]], clock: FakeClock
) -> None:
    """Cross-school classroom IDs cannot be used in assignment creation or filters."""
    own = demo_tenants["demo-school"]
    foreign = demo_tenants["demo-school-2"]
    definition = next(d for d in DEMO_SCHOOLS if d.subdomain == "demo-school")
    response = client.post(
        "/api/v1/auth/login",
        json={
            "email": "teacher@demo-school.edu",
            "password": "Teacher@123",
            "schoolId": own["school"],
            "adminKey": definition.admin_key,
        },
    )
    assert response.status_code == 200, response.text
    response = client.post(
        "/api/v1/assignments",
        json={
            "title": "Foreign classroom probe",
            "subject": "Science",
            "type": "ONLINE",
            "dueDate": (cast(datetime, clock.now()) + timedelta(days=1)).isoformat(),
            "timeLimit": 30,
            "classIds": [foreign["class"]],
        },
    )
    assert response.status_code == 422, response.text
    assert client.get(f"/api/v1/assignments?classIds={foreign['class']}").json()["total"] == 0
    response = client.patch(
        f"/api/v1/assignments/{foreign['assignment']}", json={"title": "Foreign edit"}
    )
    assert response.status_code == 404, response.text
