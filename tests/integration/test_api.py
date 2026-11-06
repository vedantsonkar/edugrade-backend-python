"""Integration tests: full API surface on sqlite + fakes.

Covers: health, auth lifecycle (login/refresh/logout/me), rate limiting,
admin CRUD + pagination, assignment/question flows with student
sanitization, the assessment attempt workflow, submissions, and files.
"""


from app.core.db.engine import session_scope
from app.domain.enums import UserRole
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _login(client: TestClient, email: str, password: str) -> dict[str, object]:
    """Login via the API and return the JSON body.

    Args:
        client: Test client.
        email: Account email.
        password: Password.

    Returns:
        Response payload.
    """
    response = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    return response.json()


class TestHealth:
    """Liveness/readiness."""

    def test_health(self, client: TestClient) -> None:
        """/health reports ok."""
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_ready_lists_checks(self, client: TestClient) -> None:
        """/ready reports per-dependency status."""
        response = client.get("/api/v1/ready")
        assert response.status_code == 200
        checks = response.json()["checks"]
        assert set(checks) == {"database", "redis", "storage"}


class TestAuthFlow:
    """Login → me → refresh → logout."""

    def test_login_me_refresh_logout(
        self, client: TestClient, seeded_ids: dict[str, str]
    ) -> None:
        """The full auth lifecycle works and rotates tokens."""
        body = _login(client, "student@test.edu", "Student@123")
        assert body["user"]["role"] == "STUDENT"
        assert body["school"]["subdomain"] == "test-academy"
        assert body["enableLogging"] is True

        me = client.get("/api/v1/auth/me")
        assert me.status_code == 200
        assert me.json()["email"] == "student@test.edu"

        refreshed = client.post("/api/v1/auth/refresh")
        assert refreshed.status_code == 200
        new_refresh = client.cookies.get("refresh_token")
        assert new_refresh

        logout = client.post("/api/v1/auth/logout")
        assert logout.status_code == 200

        # The rotated-away token must now be dead.
        replay = client.post("/api/v1/auth/refresh")
        assert replay.status_code == 401

    def test_login_wrong_password_401(self, client: TestClient) -> None:
        """Bad credentials return 401 with the error envelope."""
        response = client.post(
            "/api/v1/auth/login",
            json={"email": "student@test.edu", "password": "nope"},
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHORIZED"

    def test_anonymous_me_401(self, client: TestClient) -> None:
        """/me without credentials returns 401."""
        assert client.get("/api/v1/auth/me").status_code == 401

    def test_bearer_token_accepted(
        self, client: TestClient, seeded_ids: dict[str, str]
    ) -> None:
        """Authorization: Bearer works as an alternative to cookies."""
        body = _login(client, "teacher@test.edu", "Teacher@123")
        token = body["tokens"]["accessToken"]
        me = client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert me.status_code == 200
        assert me.json()["role"] == "TEACHER"

    def test_public_school_branding(
        self, client: TestClient, seeded_ids: dict[str, str]
    ) -> None:
        """/school/config/public resolves branding without auth."""
        response = client.get("/api/v1/school/config/public?subdomain=test-academy")
        assert response.status_code == 200
        assert response.json()["name"] == "Test Academy"

    def test_validate_admin_key(
        self, client: TestClient, seeded_ids: dict[str, str]
    ) -> None:
        """Admin-key validation succeeds and fails correctly."""
        ok = client.post(
            "/api/v1/school/validate-admin",
            json={"adminKey": "test-admin-key", "subdomain": "test-academy"},
        )
        assert ok.status_code == 200 and ok.json()["valid"] is True
        bad = client.post(
            "/api/v1/school/validate-admin",
            json={"adminKey": "wrong", "subdomain": "test-academy"},
        )
        assert bad.status_code == 200 and bad.json()["valid"] is False


class TestRateLimiting:
    """Redis-shaped limiting enforced over HTTP."""

    def test_login_rate_limited(
        self, client: TestClient, seeded_ids: dict[str, str]
    ) -> None:
        """The 11th login within a window is rate limited (10/minute)."""
        for _ in range(10):
            response = client.post(
                "/api/v1/auth/login",
                json={"email": "student@test.edu", "password": "Student@123"},
            )
            assert response.status_code == 200
        limited = client.post(
            "/api/v1/auth/login",
            json={"email": "student@test.edu", "password": "Student@123"},
        )
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "RATE_LIMITED"
        assert "Retry-After" in limited.headers


class TestAdminModule:
    """Taxonomy + user management + pagination."""

    def test_grade_crud_and_duplicate_guard(
        self, client: TestClient, seeded_ids: dict[str, str]
    ) -> None:
        """Grades create/list/delete; duplicate names conflict."""
        _login(client, "admin@test.edu", "Admin@123")
        created = client.post("/api/v1/admin/grades", json={"name": "Grade 11"})
        assert created.status_code == 200
        duplicate = client.post("/api/v1/admin/grades", json={"name": "grade 11"})
        assert duplicate.status_code == 409
        deleted = client.delete(f"/api/v1/admin/grades/{created.json()['id']}")
        assert deleted.status_code == 204

    async def test_students_paginated(
        self,
        client: TestClient,
        seeded_ids: dict[str, str],
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Student list honors pageSize and returns a Page envelope."""
        from app.domain.users import User

        async with session_scope(db_sessionmaker) as session:
            for index in range(25):
                session.add(
                    User(
                        email=f"bulk{index}@test.edu",
                        name=f"Bulk {index:02d}",
                        role=UserRole.STUDENT,
                        school_id=seeded_ids["school"],
                        grade_id=seeded_ids["grade"],
                        division_id=seeded_ids["division"],
                    )
                )
        _login(client, "admin@test.edu", "Admin@123")
        page1 = client.get("/api/v1/admin/students?pageSize=10&page=1")
        assert page1.status_code == 200
        body = page1.json()
        assert body["pageSize"] == 10
        assert body["total"] == 26  # 25 bulk + 1 seeded
        assert body["totalPages"] == 3
        assert len(body["items"]) == 10

        page2 = client.get("/api/v1/admin/students?pageSize=10&page=2")
        assert page2.json()["items"][0]["name"] != body["items"][0]["name"]

        searched = client.get("/api/v1/admin/students?q=bulk24&pageSize=5")
        assert searched.json()["total"] == 1

    def test_students_requires_auth(self, client: TestClient) -> None:
        """Anonymous access is 401."""
        assert client.get("/api/v1/admin/students").status_code == 401

    def test_students_forbidden_for_students(
        self, client: TestClient, seeded_ids: dict[str, str]
    ) -> None:
        """Student role cannot list students."""
        _login(client, "student@test.edu", "Student@123")
        assert client.get("/api/v1/admin/students").status_code == 403

    def test_class_teacher_assignment(
        self, client: TestClient, seeded_ids: dict[str, str]
    ) -> None:
        """Admin can assign and clear a classroom teacher."""
        _login(client, "admin@test.edu", "Admin@123")
        updated = client.patch(
            f"/api/v1/admin/classes/{seeded_ids['class']}",
            json={"classTeacherId": seeded_ids["teacher"]},
        )
        assert updated.status_code == 200
        assert updated.json()["classTeacherId"] == seeded_ids["teacher"]
        cleared = client.patch(
            f"/api/v1/admin/classes/{seeded_ids['class']}",
            json={"classTeacherId": None},
        )
        assert cleared.status_code == 200
        assert cleared.json()["classTeacherId"] is None

    def test_stats_endpoint(
        self, client: TestClient, seeded_ids: dict[str, str]
    ) -> None:
        """Dashboard stats aggregate counts."""
        _login(client, "admin@test.edu", "Admin@123")
        response = client.get("/api/v1/admin/stats")
        assert response.status_code == 200
        body = response.json()
        assert body["totalStudents"] == 1
        assert body["totalTeachers"] == 1
        assert body["totalClasses"] == 1
        assert body["classesWithoutTeacher"] == 1


class TestAssignments:
    """Assignment CRUD, questions, submissions."""

    def _create_assignment(
        self, client: TestClient, seeded_ids: dict[str, str]
    ) -> dict[str, object]:
        """Create an online assignment via the API.

        Args:
            client: Test client (teacher logged in by caller).
            seeded_ids: Seed ids.

        Returns:
            Created assignment body.
        """
        response = client.post(
            "/api/v1/assignments",
            json={
                "type": "ONLINE",
                "title": "Photosynthesis Quiz",
                "subject": "Biology",
                "dueDate": "2026-12-01T00:00:00Z",
                "timeLimit": 30,
                "classIds": [seeded_ids["class"]],
            },
        )
        assert response.status_code == 200, response.text
        return response.json()

    def test_create_and_list_role_scoped(
        self, client: TestClient, seeded_ids: dict[str, str]
    ) -> None:
        """Teachers create; targeted students see it; others don't."""
        _login(client, "teacher@test.edu", "Teacher@123")
        assignment = self._create_assignment(client, seeded_ids)

        _login(client, "student@test.edu", "Student@123")
        listed = client.get("/api/v1/assignments")
        assert listed.status_code == 200
        assert listed.json()["total"] == 1
        assert listed.json()["items"][0]["id"] == assignment["id"]

    def test_questions_sanitized_for_students(
        self, client: TestClient, seeded_ids: dict[str, str]
    ) -> None:
        """Student question reads never carry isCorrect/explanations."""
        _login(client, "teacher@test.edu", "Teacher@123")
        assignment = self._create_assignment(client, seeded_ids)
        created = client.post(
            f"/api/v1/assignments/{assignment['id']}/questions",
            json={
                "questions": [{
                    "type": "MCQ_SINGLE",
                    "text": "Which pigment absorbs light?",
                    "points": 2,
                    "displayOrder": 1,
                    "explanation": "Chlorophyll.",
                    "options": [
                        {"text": "Chlorophyll", "isCorrect": True, "displayOrder": 1},
                        {"text": "Melanin", "isCorrect": False, "displayOrder": 2},
                    ],
                }]
            },
        )
        assert created.status_code == 200, created.text

        _login(client, "student@test.edu", "Student@123")
        listed = client.get(f"/api/v1/assignments/{assignment['id']}/questions")
        assert listed.status_code == 200
        question = listed.json()[0]
        assert question["explanation"] is None
        assert all(option["isCorrect"] is False for option in question["options"])

    def test_attempt_flow_auto_grades_mcq(
        self, client: TestClient, seeded_ids: dict[str, str]
    ) -> None:
        """Start → answer → submit grades the MCQ and returns aggregates."""
        _login(client, "teacher@test.edu", "Teacher@123")
        assignment = self._create_assignment(client, seeded_ids)
        created = client.post(
            f"/api/v1/assignments/{assignment['id']}/questions",
            json={
                "questions": [{
                    "type": "MCQ_SINGLE",
                    "text": "Which pigment absorbs light?",
                    "points": 2,
                    "displayOrder": 1,
                    "options": [
                        {"text": "Chlorophyll", "isCorrect": True, "displayOrder": 1},
                        {"text": "Melanin", "isCorrect": False, "displayOrder": 2},
                    ],
                }]
            },
        )
        question = created.json()[0]
        correct_option = next(
            o for o in question["options"] if o["text"] == "Chlorophyll"
        )

        _login(client, "student@test.edu", "Student@123")
        started = client.post(f"/api/v1/assessments/{assignment['id']}/start")
        assert started.status_code == 200, started.text
        assert started.json()["remainingSeconds"] > 0
        assert started.json()["settings"]["maxTabSwitches"] == 3

        saved = client.post(
            f"/api/v1/assessments/{assignment['id']}/answer",
            json={"questionId": question["id"], "answerData": correct_option["id"]},
        )
        assert saved.status_code == 200

        submitted = client.post(f"/api/v1/assessments/{assignment['id']}/submit")
        assert submitted.status_code == 200, submitted.text
        body = submitted.json()
        assert body["score"] == 2.0
        assert body["maxScore"] == 2.0
        assert body["percentage"] == 100.0
        assert body["attempt"]["status"] == "SUBMITTED"

    def test_tab_switch_terminates_attempt(
        self, client: TestClient, seeded_ids: dict[str, str]
    ) -> None:
        """Exceeding the switch allowance terminates the attempt."""
        _login(client, "teacher@test.edu", "Teacher@123")
        assignment = self._create_assignment(client, seeded_ids)
        client.patch(
            f"/api/v1/assignments/{assignment['id']}",
            json={"enableTabDetection": True, "maxTabSwitches": 1},
        )
        _login(client, "student@test.edu", "Student@123")
        client.post(f"/api/v1/assessments/{assignment['id']}/start")
        first = client.post(f"/api/v1/assessments/{assignment['id']}/tab-switch", json={})
        assert first.status_code == 200 and first.json()["terminated"] is False
        second = client.post(f"/api/v1/assessments/{assignment['id']}/tab-switch", json={})
        assert second.status_code == 200 and second.json()["terminated"] is True

    def test_submission_and_grade_override(
        self, client: TestClient, seeded_ids: dict[str, str]
    ) -> None:
        """Student submits; teacher lists and overrides the grade."""
        _login(client, "teacher@test.edu", "Teacher@123")
        offline = client.post(
            "/api/v1/assignments",
            json={
                "type": "OFFLINE",
                "title": "Lab Report",
                "subject": "Biology",
                "dueDate": "2026-12-01T00:00:00Z",
                "classIds": [seeded_ids["class"]],
            },
        )
        assignment_id = offline.json()["id"]

        presign = client.post(
            "/api/v1/files/presigned-url",
            json={"name": "report.pdf", "type": "application/pdf"},
        )
        assert presign.status_code == 200

        _login(client, "student@test.edu", "Student@123")
        submitted = client.post(
            f"/api/v1/assignments/{assignment_id}/submit",
            json={
                "attachments": [{
                    "name": "report.pdf",
                    "type": "application/pdf",
                    "url": presign.json()["key"],
                    "size": 128,
                }]
            },
        )
        assert submitted.status_code == 200, submitted.text

        _login(client, "teacher@test.edu", "Teacher@123")
        listed = client.get(f"/api/v1/assignments/{assignment_id}/submissions")
        assert listed.status_code == 200
        assert listed.json()["total"] == 1

        submission_id = listed.json()["items"][0]["id"]
        override = client.post(
            f"/api/v1/assignments/{assignment_id}/grade/{submission_id}",
            json={"gradeOverride": "A", "teacherNotes": "Excellent"},
        )
        assert override.status_code == 200
        assert override.json()["gradeOverride"] == "A"
        assert override.json()["isOverridden"] is True

    def test_rubric_comments(
        self, client: TestClient, seeded_ids: dict[str, str]
    ) -> None:
        """Both teacher and student participate in the thread."""
        _login(client, "teacher@test.edu", "Teacher@123")
        assignment = self._create_assignment(client, seeded_ids)
        posted = client.post(
            f"/api/v1/assignments/{assignment['id']}/comments",
            json={"comment": "Read chapter 4 first."},
        )
        assert posted.status_code == 200
        _login(client, "student@test.edu", "Student@123")
        reply = client.post(
            f"/api/v1/assignments/{assignment['id']}/comments",
            json={"comment": "Got it!"},
        )
        assert reply.status_code == 200
        thread = client.get(f"/api/v1/assignments/{assignment['id']}/comments")
        assert thread.json()["total"] if "total" in thread.json() else len(thread.json()) == 2


class TestFilesAndProfile:
    """Presign scope rules and profile management."""

    def test_download_scope_enforced(
        self, client: TestClient, seeded_ids: dict[str, str]
    ) -> None:
        """Students cannot read other schools' uploads."""
        _login(client, "student@test.edu", "Student@123")
        foreign = client.get(
            "/api/v1/files/presigned-url",
            params={"key": "uploads/other-school/file.pdf"},
        )
        assert foreign.status_code == 403

    def test_profile_update_and_password(
        self, client: TestClient, seeded_ids: dict[str, str]
    ) -> None:
        """PATCH profile works; password change verifies the old one."""
        _login(client, "student@test.edu", "Student@123")
        updated = client.patch(
            "/api/v1/profile",
            json={"name": "Sammy Student", "phoneNumber": "+919876543210"},
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "Sammy Student"

        wrong = client.post(
            "/api/v1/profile/password",
            json={"currentPassword": "nope", "newPassword": "NewPass@123"},
        )
        assert wrong.status_code == 401

        ok = client.post(
            "/api/v1/profile/password",
            json={"currentPassword": "Student@123", "newPassword": "NewPass@123"},
        )
        assert ok.status_code == 200

    def test_avatar_upload(
        self, client: TestClient, seeded_ids: dict[str, str]
    ) -> None:
        """Multipart avatar upload stores the key on the profile."""
        _login(client, "student@test.edu", "Student@123")
        response = client.post(
            "/api/v1/profile/avatar",
            files={"avatar": ("me.png", b"\x89PNG fake", "image/png")},
        )
        assert response.status_code == 200, response.text
        assert response.json()["avatarUrl"].startswith("avatars/")

    def test_analytics_batch(
        self, client: TestClient, seeded_ids: dict[str, str]
    ) -> None:
        """Batched events are accepted and attributed."""
        _login(client, "student@test.edu", "Student@123")
        response = client.post(
            "/api/v1/analytics/batch",
            json={"events": [{"action": "PAGE_VIEW", "resource": "dashboard"}]},
        )
        assert response.status_code == 200
        assert response.json()["accepted"] == 1
