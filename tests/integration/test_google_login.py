"""Google OAuth policy tests using a fake provider and the real callback route."""

from urllib.parse import parse_qs, urlparse
from uuid import UUID

import httpx
from app.core.db.engine import session_scope
from app.domain.enums import AuthMethod
from app.domain.schools import School, SchoolConfig
from app.domain.users import User
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_google_requires_registered_same_school_verified_student(
    app: FastAPI,
    client: TestClient,
    seeded_ids: dict[str, str],
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Unknown, unverified and wrong-school identities never receive a session."""
    container = app.state.container
    container.settings = container.settings.model_copy(
        update={
            "jwt_secret": "test-secret-that-is-long-enough-for-hmac-signing",
                "google_client_id": "test-client",
            "google_client_secret": "test-secret",
        }
    )
    async with session_scope(db_sessionmaker) as session:
        student = await session.get(User, UUID(seeded_ids["student"]))
        assert student is not None
        student.auth_method = AuthMethod.BOTH
        school = School(name="Other School", school_key="other-school", admin_key="other")
        session.add(school)
        await session.flush()
        session.add(SchoolConfig(school_id=str(school.id)))

    identity: dict[str, object] = {
        "email": "unknown@test.edu",
        "email_verified": True,
        "name": "Unknown",
    }

    def provider(request: httpx.Request) -> httpx.Response:
        """Return token/userinfo responses without contacting Google."""
        return httpx.Response(
            200,
            json={"access_token": "fake-provider-token"} if request.method == "POST" else identity,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as provider_client:
        container.http_client = provider_client
        for email, verified, school, success in [
            ("unknown@test.edu", True, "test-academy", False),
            ("student@test.edu", False, "test-academy", False),
            ("student@test.edu", True, "other-school", False),
            ("teacher@test.edu", True, "test-academy", False),
            ("student@test.edu", True, "test-academy", True),
        ]:
            identity.update(email=email, email_verified=verified)
            start = client.get(f"/api/v1/auth/google?school={school}", follow_redirects=False)
            state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
            callback = client.get(
                "/api/v1/auth/google/callback",
                params={"code": "fake", "state": state},
                follow_redirects=False,
            )
            assert callback.status_code == 307
            assert ("/auth/success" in callback.headers["location"]) is success
            assert bool(callback.headers.get_list("set-cookie")) is success
    async with session_scope(db_sessionmaker) as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 3
