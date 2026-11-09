"""Security/isolation edge-case tests over the real HTTP surface.

Covers token hardening (forgery, type confusion, expiry), the dev-session
bypass being dead outside development, role guards, cross-tenant object
isolation, request-body tenant injection, and mid-session deactivation.
"""

import json
from datetime import timedelta
from typing import cast
from uuid import UUID

import pytest
from app.core.db.engine import session_scope
from app.domain.assignments import Assignment
from app.domain.enums import AssignmentType, AuthMethod, UserRole
from app.domain.schools import School
from app.domain.taxonomy import Grade
from app.domain.users import User
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.fakes import FakeClock


def _bearer(
    client: TestClient, email: str, password: str, admin_key: str | None = None
) -> dict[str, str]:
    """Login and return an Authorization header with the access token.

    Args:
        client: Test client.
        email: Account email.
        password: Password.
        admin_key: School admin key (required for staff accounts).

    Returns:
        Bearer Authorization header mapping.
    """
    payload: dict[str, object] = {"email": email, "password": password}
    if admin_key is not None:
        payload["adminKey"] = admin_key
    body = client.post("/api/v1/auth/login", json=payload).json()
    return {"Authorization": f"Bearer {body['tokens']['accessToken']}"}


@pytest.fixture
async def foreign_school(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    hasher: object,
    clock: FakeClock,
) -> dict[str, str]:
    """Seed a second tenant with its own grade and assignment.

    Args:
        db_sessionmaker: Session factory.
        hasher: Password hasher.
        clock: Fake clock for the assignment visibility window.

    Returns:
        Mapping with the foreign school/grade/assignment ids.
    """
    from app.core.security.passwords import BcryptPasswordHasher

    async with session_scope(db_sessionmaker) as session:
        school = School(
            name="Other Academy", subdomain="other-academy", admin_key="other-admin-key"
        )
        session.add(school)
        await session.flush()
        teacher = User(
            email="teacher@other.edu",
            name="Ora Other",
            role=UserRole.TEACHER,
            school_id=str(school.id),
            password_hash=cast(BcryptPasswordHasher, hasher).hash("Other@123"),
            auth_method=AuthMethod.CREDENTIALS,
        )
        session.add(teacher)
        await session.flush()
        grade = Grade(school_id=str(school.id), name="Grade 9", display_order=9)
        session.add(grade)
        await session.flush()
        assignment = Assignment(
            school_id=str(school.id),
            teacher_id=str(teacher.id),
            type=AssignmentType.OFFLINE,
            title="Foreign homework",
            subject="Math",
            due_date=cast(FakeClock, clock).now() + timedelta(days=1),
        )
        session.add(assignment)
        await session.flush()
        return {
            "school": str(school.id),
            "teacher": str(teacher.id),
            "grade": str(grade.id),
            "assignment": str(assignment.id),
        }


class TestTokenHardening:
    """Only genuine, unexpired access tokens authenticate."""

    def test_garbage_bearer_token_rejected(self, client: TestClient) -> None:
        """A malformed bearer token is a 401, never a 500."""
        response = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not.a.token"})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHORIZED"

    def test_refresh_token_cannot_act_as_access_token(
        self, client: TestClient, seeded_ids: dict[str, str]
    ) -> None:
        """Presenting a refresh token where an access token is required fails."""
        body = client.post(
            "/api/v1/auth/login",
            json={"email": "student@test.edu", "password": "Student@123"},
        ).json()
        refresh = body["tokens"]["refreshToken"]
        response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {refresh}"})
        assert response.status_code == 401

    def test_expired_access_token_rejected(
        self,
        client: TestClient,
        seeded_ids: dict[str, str],
        clock: FakeClock,
    ) -> None:
        """Tokens past their TTL stop authenticating immediately."""
        headers = _bearer(client, "student@test.edu", "Student@123")
        clock.advance(901)  # Fake access TTL is 900s.
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 401


class TestDevSessionBypass:
    """The development-only impersonation header is dead in every other env."""

    def test_dev_session_header_rejected_outside_development(
        self, client: TestClient, seeded_ids: dict[str, str]
    ) -> None:
        """A forged X-User-Session admin payload yields 401 (env != development)."""
        forged = json.dumps(
            {
                "id": seeded_ids["admin"],
                "email": "admin@test.edu",
                "name": "Ada Admin",
                "role": "ADMIN",
                "schoolId": seeded_ids["school"],
            }
        )
        response = client.get("/api/v1/admin/students", headers={"X-User-Session": forged})
        assert response.status_code == 401


class TestRoleGuards:
    """Route guards enforce the token's role claim."""

    def test_student_cannot_use_admin_endpoints(
        self, client: TestClient, seeded_ids: dict[str, str]
    ) -> None:
        """A valid student token is still 403 on admin-only routes."""
        headers = _bearer(client, "student@test.edu", "Student@123")
        assert client.get("/api/v1/admin/students", headers=headers).status_code == 403

    def test_teacher_blocked_from_admin_only_routes(
        self, client: TestClient, seeded_ids: dict[str, str]
    ) -> None:
        """Teachers are refused on admin-only mutations but read shared lists."""
        headers = _bearer(client, "teacher@test.edu", "Teacher@123", admin_key="test-admin-key")
        created = client.post("/api/v1/admin/grades", headers=headers, json={"name": "Grade 12"})
        assert created.status_code == 403
        assert client.get("/api/v1/admin/students", headers=headers).status_code == 200

    def test_anonymous_request_is_401_not_403(self, client: TestClient) -> None:
        """Unauthenticated callers get 401 before any role check runs."""
        assert client.get("/api/v1/admin/students").status_code == 401


class TestTenantIsolation:
    """Objects of one school are invisible to authenticated users of another."""

    def test_teacher_cannot_read_foreign_assignment(
        self,
        client: TestClient,
        seeded_ids: dict[str, str],
        foreign_school: dict[str, str],
    ) -> None:
        """Fetching another tenant's assignment by id returns 404, not data."""
        headers = _bearer(client, "teacher@test.edu", "Teacher@123", admin_key="test-admin-key")
        response = client.get(
            f"/api/v1/assignments/{foreign_school['assignment']}", headers=headers
        )
        assert response.status_code == 404

    def test_admin_cannot_delete_foreign_grade(
        self,
        client: TestClient,
        seeded_ids: dict[str, str],
        foreign_school: dict[str, str],
    ) -> None:
        """A foreign-school grade id is indistinguishable from a missing one."""
        headers = _bearer(client, "admin@test.edu", "Admin@123", admin_key="test-admin-key")
        response = client.delete(f"/api/v1/admin/grades/{foreign_school['grade']}", headers=headers)
        assert response.status_code == 404

    def test_student_list_never_leaks_other_tenants(
        self,
        client: TestClient,
        seeded_ids: dict[str, str],
        foreign_school: dict[str, str],
    ) -> None:
        """The admin students list only ever contains the caller's school."""
        headers = _bearer(client, "admin@test.edu", "Admin@123", admin_key="test-admin-key")
        body = client.get("/api/v1/admin/students", headers=headers).json()
        emails = {item["email"] for item in body["items"]}
        assert emails == {"student@test.edu"}

    def test_cross_school_upload_download_grant_refused(
        self,
        client: TestClient,
        seeded_ids: dict[str, str],
        foreign_school: dict[str, str],
    ) -> None:
        """A key under another school's folder never yields a grant."""
        headers = _bearer(client, "student@test.edu", "Student@123")
        response = client.get(
            "/api/v1/files/presigned-url",
            params={"key": f"uploads/{foreign_school['school']}/report.pdf"},
            headers=headers,
        )
        assert response.status_code == 403

    def test_own_school_upload_download_grant_allowed(
        self, client: TestClient, seeded_ids: dict[str, str]
    ) -> None:
        """Keys inside the caller's own school folder do yield a grant."""
        headers = _bearer(client, "student@test.edu", "Student@123")
        response = client.get(
            "/api/v1/files/presigned-url",
            params={"key": f"uploads/{seeded_ids['school']}/report.pdf"},
            headers=headers,
        )
        assert response.status_code == 200

    def test_snapshot_of_foreign_assignment_hidden_from_student(
        self,
        client: TestClient,
        seeded_ids: dict[str, str],
        foreign_school: dict[str, str],
    ) -> None:
        """Even a self-named snapshot path under a foreign assignment is 404."""
        headers = _bearer(client, "student@test.edu", "Student@123")
        response = client.get(
            "/api/v1/files/presigned-url",
            params={
                "key": (
                    f"snapshots/{foreign_school['assignment']}/{seeded_ids['student']}/snap.jpg"
                )
            },
            headers=headers,
        )
        assert response.status_code == 404

    def test_traversal_style_key_rejected(
        self, client: TestClient, seeded_ids: dict[str, str]
    ) -> None:
        """Path-traversal payloads fail validation before any grant."""
        headers = _bearer(client, "student@test.edu", "Student@123")
        response = client.get(
            "/api/v1/files/presigned-url",
            params={"key": "uploads/../../etc/passwd"},
            headers=headers,
        )
        assert response.status_code == 422


class TestTenantContextInjection:
    """Tenant context comes from the token; bodies cannot smuggle it."""

    def test_school_id_in_request_body_rejected(
        self, client: TestClient, seeded_ids: dict[str, str]
    ) -> None:
        """Unknown body fields (like a forged schoolId) fail fast with 422."""
        headers = _bearer(client, "admin@test.edu", "Admin@123", admin_key="test-admin-key")
        response = client.post(
            "/api/v1/admin/grades",
            headers=headers,
            json={"name": "Grade 12", "schoolId": "some-other-school"},
        )
        assert response.status_code == 422


class TestMidSessionRevocation:
    """Account state is re-checked where freshness matters."""

    async def test_deactivated_account_session_killed_on_me(
        self,
        client: TestClient,
        seeded_ids: dict[str, str],
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        """/auth/me re-verifies the account and refuses deactivated users."""
        headers = _bearer(client, "student@test.edu", "Student@123")
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 200
        async with session_scope(db_sessionmaker) as session:
            user = await session.get(User, UUID(seeded_ids["student"]))
            assert user is not None
            user.is_active = False
        response = client.get("/api/v1/auth/me", headers=headers)
        assert response.status_code == 401
