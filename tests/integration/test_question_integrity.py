"""Question-read and teacher DTO regression tests discovered in live integration."""

from fastapi.testclient import TestClient
from tests.fakes import FakeClock
from tests.integration.test_frontend_contracts import assignment, login


def test_student_question_reads_preserve_stored_answers(
    client: TestClient,
    seeded_ids: dict[str, str],
    clock: FakeClock,
) -> None:
    """A student's safe projection never changes the teacher's correct answer."""
    login(client, "teacher")
    assignment_id = assignment(client, seeded_ids, clock, "ONLINE")
    response = client.post(
        f"/api/v1/assignments/{assignment_id}/questions",
        json={
            "questions": [
                {
                    "type": "MCQ_SINGLE",
                    "text": "Choose A",
                    "explanation": "A is right",
                    "options": [
                        {"text": "A", "isCorrect": True},
                        {"text": "B", "isCorrect": False},
                    ],
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    login(client, "student")
    safe = client.get(f"/api/v1/assignments/{assignment_id}/questions").json()
    assert not any(option["isCorrect"] for option in safe[0]["options"])
    assert safe[0]["explanation"] is None
    login(client, "teacher")
    saved = client.get(f"/api/v1/assignments/{assignment_id}/questions").json()
    assert saved[0]["explanation"] == "A is right"
    assert saved[0]["options"][0]["isCorrect"] is True
    changed = client.patch(
        f"/api/v1/assignments/{assignment_id}/questions/{saved[0]['id']}",
        json={"type": "FREE_TEXT", "text": "Explain your answer"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["type"] == "FREE_TEXT"
    assert changed.json()["options"] == []


def test_teacher_creation_and_classroom_summary(
    client: TestClient, seeded_ids: dict[str, str]
) -> None:
    """Teacher list and create responses serialize classroom IDs and initial passwords."""
    login(client, "admin")
    assigned = client.patch(
        f"/api/v1/admin/classes/{seeded_ids['class']}",
        json={"classTeacherId": seeded_ids["teacher"]},
    )
    assert assigned.status_code == 200, assigned.text
    teachers = client.get("/api/v1/admin/teachers")
    assert teachers.status_code == 200, teachers.text
    assert teachers.json()["items"][0]["classesTaught"] == [seeded_ids["class"]]
    created = client.post(
        "/api/v1/admin/teachers",
        json={
            "email": "newteacher@test.edu",
            "name": "New Teacher",
            "dateOfBirth": "1990-01-01T00:00:00Z",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["generatedPassword"] == "NewTeacher19900101"
    listed = client.get("/api/v1/admin/teachers").json()
    assert all("generatedPassword" not in teacher for teacher in listed["items"])


def test_browser_auth_uses_cookie_only_tokens(
    client: TestClient, seeded_ids: dict[str, str]
) -> None:
    """The gateway's browser marker suppresses explicit token values in JSON."""
    response = client.post(
        "/api/v1/auth/login",
        headers={"x-edugrade-browser": "1"},
        json={"email": "student@test.edu", "password": "Student@123"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["tokens"] is None
    assert client.cookies.get("access_token")
    refreshed = client.post("/api/v1/auth/refresh", headers={"x-edugrade-browser": "1"})
    assert refreshed.status_code == 200
    assert refreshed.json()["tokens"] is None


def test_unreleased_results_are_hidden_across_student_endpoints(
    client: TestClient,
    seeded_ids: dict[str, str],
    clock: FakeClock,
) -> None:
    """The results-release setting applies to submit, status, summaries and review."""
    login(client, "teacher")
    assignment_id = assignment(client, seeded_ids, clock, "ONLINE")
    client.patch(f"/api/v1/assignments/{assignment_id}", json={"showResultsAfter": False})
    question = client.post(
        f"/api/v1/assignments/{assignment_id}/questions",
        json={
            "questions": [
                {
                    "type": "MCQ_SINGLE",
                    "text": "Choose A",
                    "options": [
                        {"text": "A", "isCorrect": True},
                        {"text": "B", "isCorrect": False},
                    ],
                }
            ]
        },
    ).json()[0]
    login(client, "student")
    attempt = client.post(f"/api/v1/assessments/{assignment_id}/start").json()["attempt"]
    client.post(
        f"/api/v1/assessments/{assignment_id}/answer",
        json={"questionId": question["id"], "answerData": [question["options"][0]["id"]]},
    )
    submitted = client.post(f"/api/v1/assessments/{assignment_id}/submit")
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["score"] is None
    assert submitted.json()["attempt"]["answers"] == []
    assert client.get(f"/api/v1/assessments/{assignment_id}/status").json()["score"] is None
    detail = client.get(f"/api/v1/assignments/{assignment_id}").json()
    assert detail["state"] == "submitted"
    assert detail["myAttempt"]["score"] is None
    assert (
        client.get(f"/api/v1/assignments/{assignment_id}/attempts").json()["items"][0]["score"]
        is None
    )
    assert client.get(f"/api/v1/attempts/{attempt['id']}/review").status_code == 403
