"""Contract regression tests for the standalone frontend integration."""

from datetime import timedelta

from fastapi.testclient import TestClient
from tests.fakes import FakeClock, InMemoryStorage


def login(client: TestClient, role: str) -> None:
    """Authenticate a seeded role through the same cookie API as the frontend."""
    payload: dict[str, object] = {
        "email": f"{role}@test.edu",
        "password": f"{role.title()}@123",
        "loginType": "student" if role == "student" else "admin",
    }
    if role != "student":
        payload["adminKey"] = "test-admin-key"
    response = client.post("/api/v1/auth/login", json=payload)
    assert response.status_code == 200, response.text


def assignment(
    client: TestClient, ids: dict[str, str], clock: FakeClock, kind: str = "OFFLINE"
) -> str:
    """Create a targeted assignment inside the deterministic open window."""
    response = client.post(
        "/api/v1/assignments",
        json={
            "title": "Integration exercise",
            "subject": "Science",
            "type": kind,
            "startDate": (clock.now() - timedelta(hours=1)).isoformat(),
            "dueDate": (clock.now() + timedelta(days=1)).isoformat(),
            "classIds": [ids["class"]],
            **({"timeLimit": 60} if kind == "ONLINE" else {}),
        },
    )
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


def test_school_settings_and_counts(client: TestClient, seeded_ids: dict[str, str]) -> None:
    """Admin settings persist; student access and foreign selectors are rejected."""
    login(client, "admin")
    response = client.patch(
        "/api/v1/admin/school/settings",
        json={"primaryColor": "#123456", "currentAcademicYear": "2026-2027"},
    )
    assert response.status_code == 200, response.text
    assert client.get("/api/v1/admin/school/settings").json()["primaryColor"] == "#123456"
    assert client.get("/api/v1/admin/user-counts?role=STUDENT").json() == {
        "active": 1,
        "inactive": 0,
    }
    assert (
        client.patch("/api/v1/admin/school/settings", json={"schoolId": "foreign"}).status_code
        == 422
    )
    login(client, "student")
    assert client.get("/api/v1/admin/school/settings").status_code == 403
    assert client.get("/api/v1/admin/user-counts").status_code == 403


def test_csv_preview_is_read_only_and_import_is_atomic(
    client: TestClient, seeded_ids: dict[str, str]
) -> None:
    """Preview does not update existing users and invalid imports require an explicit skip."""
    login(client, "admin")
    preview = client.post(
        "/api/v1/admin/users/bulk-csv",
        json={
            "csv": "email,name\nstudent@test.edu,Changed Name",
            "dryRun": True,
            "operation": "update",
            "userType": "student",
        },
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["updated"] == 1
    assert client.get("/api/v1/admin/students").json()["items"][0]["name"] == "Sam Student"
    payload = {
        "csv": "email,name,dob\nfresh@test.edu,New Student,2010-01-01\nbad,Invalid,wrong",
        "operation": "create",
        "userType": "student",
    }
    invalid = client.post("/api/v1/admin/users/bulk-csv", json=payload)
    assert invalid.json()["valid"] is False
    assert invalid.json()["created"] == 0
    assert client.get("/api/v1/admin/students").json()["total"] == 1
    imported = client.post("/api/v1/admin/users/bulk-csv", json={**payload, "skipInvalid": True})
    assert imported.json()["created"] == 1
    assert client.get("/api/v1/admin/students").json()["total"] == 2


def test_csv_status_and_generated_password(client: TestClient, seeded_ids: dict[str, str]) -> None:
    """CSV status updates and template password generation preserve existing behavior."""
    login(client, "admin")
    created = client.post(
        "/api/v1/admin/users/bulk-csv",
        json={
            "csv": "email,name,dob,grade,division\nnew@test.edu,New Student,2010-01-01,Grade 9,A",
            "operation": "create",
            "userType": "student",
        },
    )
    assert created.json()["created"] == 1, created.text
    response = client.post(
        "/api/v1/auth/login", json={"email": "new@test.edu", "password": "NewStudent20100101"}
    )
    assert response.status_code == 200, response.text
    login(client, "admin")
    response = client.post(
        "/api/v1/admin/users/bulk-csv",
        json={
            "csv": "email,isActive\nnew@test.edu,false",
            "operation": "status",
            "userType": "student",
        },
    )
    assert response.json()["updated"] == 1
    assert client.get("/api/v1/admin/user-counts?role=STUDENT").json()["inactive"] == 1


def test_assignment_state_and_student_filter_scope(
    client: TestClient, seeded_ids: dict[str, str], clock: FakeClock
) -> None:
    """Server states agree with filters and foreign classroom filters cannot broaden visibility."""
    login(client, "teacher")
    assignment_id = assignment(client, seeded_ids, clock)
    login(client, "student")
    listed = client.get("/api/v1/assignments?state=due_soon")
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["state"] == "due_soon"
    assert client.get(f"/api/v1/assignments?classIds={seeded_ids['school']}").json()["total"] == 0
    submitted = client.post(
        f"/api/v1/assignments/{assignment_id}/submit",
        json={
            "attachments": [
                {
                    "name": "answer.pdf",
                    "type": "application/pdf",
                    "url": f"uploads/{seeded_ids['school']}/answer.pdf",
                }
            ]
        },
    )
    assert submitted.status_code == 200, submitted.text
    own = client.get(f"/api/v1/assignments/{assignment_id}/submissions")
    assert own.status_code == 200, own.text
    assert own.json()["items"][0]["studentId"] == seeded_ids["student"]
    assert client.get("/api/v1/assignments?state=submitted").json()["total"] == 1
    assert client.get(f"/api/v1/assignments/{assignment_id}").json()["state"] == "submitted"


def test_batch_grading_deduplication_and_latest_task(
    client: TestClient, seeded_ids: dict[str, str], clock: FakeClock
) -> None:
    """Duplicate work and online grade-all requests are rejected."""
    login(client, "teacher")
    assignment_id = assignment(client, seeded_ids, clock)
    first = client.post(
        f"/api/v1/assignments/{assignment_id}/grade-all", json={"mode": "UNGRADED_ONLY"}
    )
    assert first.status_code == 200, first.text
    assert client.post(f"/api/v1/assignments/{assignment_id}/grade-all").status_code == 409
    assert (
        client.get(f"/api/v1/assignments/{assignment_id}/grading-task").json()["id"]
        == first.json()["id"]
    )
    online_id = assignment(client, seeded_ids, clock, "ONLINE")
    assert client.post(f"/api/v1/assignments/{online_id}/grade-all").status_code == 409


def test_attempt_review_and_question_overrides(
    client: TestClient, seeded_ids: dict[str, str], clock: FakeClock
) -> None:
    """Teachers can review finished attempts and update question points safely."""
    login(client, "teacher")
    assignment_id = assignment(client, seeded_ids, clock, "ONLINE")
    questions = client.post(
        f"/api/v1/assignments/{assignment_id}/questions",
        json={
            "questions": [
                {
                    "type": "MCQ_SINGLE",
                    "text": "Pick A",
                    "points": 4,
                    "options": [{"text": "A", "isCorrect": True}, {"text": "B"}],
                }
            ]
        },
    ).json()
    question = questions[0]
    reordered = client.patch(
        f"/api/v1/assignments/{assignment_id}/questions/reorder",
        json={"questionIds": [question["id"]]},
    )
    assert reordered.status_code == 204, reordered.text
    login(client, "student")
    started = client.post(f"/api/v1/assessments/{assignment_id}/start")
    attempt_id = started.json()["attempt"]["id"]
    client.post(
        f"/api/v1/assessments/{assignment_id}/answer",
        json={"questionId": question["id"], "answerData": question["options"][1]["id"]},
    )
    assert client.post(f"/api/v1/assessments/{assignment_id}/submit").status_code == 200
    login(client, "teacher")
    attempts = client.get(f"/api/v1/assignments/{assignment_id}/attempts")
    assert attempts.status_code == 200, attempts.text
    assert attempts.json()["items"][0]["student"]["name"] == "Sam Student"
    assert attempts.json()["items"][0]["answers"] == []
    review = client.get(f"/api/v1/attempts/{attempt_id}/review")
    assert review.status_code == 200, review.text
    updated = client.patch(
        f"/api/v1/attempts/{attempt_id}/grade", json={"questionOverrides": {question["id"]: 3}}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["score"] == 3
    assert (
        client.patch(
            f"/api/v1/attempts/{attempt_id}/grade", json={"questionOverrides": {question["id"]: 5}}
        ).status_code
        == 422
    )
    assert (
        client.patch(f"/api/v1/attempts/{attempt_id}/grade", json={"gradeOverride": 5}).status_code
        == 422
    )


async def test_archive_list_is_tenant_scoped(
    client: TestClient, seeded_ids: dict[str, str], storage: InMemoryStorage
) -> None:
    """Archive metadata only includes the authenticated school's objects."""
    own = f"audit-logs/{seeded_ids['school']}/2026-01-01.json"
    await storage.put_bytes(own, b"[]", "application/json")
    await storage.put_bytes("audit-logs/foreign/2026-01-01.json", b"[]", "application/json")
    login(client, "admin")
    response = client.get("/api/v1/admin/audit/archives")
    assert response.status_code == 200, response.text
    assert response.json() == [{"key": own, "size": 2, "lastModified": None}]


def test_credentials_portal_restrictions(client: TestClient, seeded_ids: dict[str, str]) -> None:
    """Portal rules are enforced by Python rather than trusted client UI."""
    for role, portal in [("teacher", "student"), ("student", "admin")]:
        response = client.post(
            "/api/v1/auth/login",
            json={
                "email": f"{role}@test.edu",
                "password": f"{role.title()}@123",
                "loginType": portal,
            },
        )
        assert response.status_code == 403
