"""Shared pytest fixtures: in-memory API app with all fakes wired."""

from collections.abc import AsyncIterator, Callable

import app.domain.assessments
import app.domain.assignments
import app.domain.audit
import app.domain.grading
import pytest
import uuid_utils.compat as uuid_compat
from app.api.v1.deps import Container
from app.core.config import Settings
from app.core.db.base import Base
from app.core.db.engine import session_scope
from app.core.ids import Uuid7Provider
from app.core.security.passwords import BcryptPasswordHasher
from app.core.security.principal import UserPrincipal
from app.domain.enums import AuthMethod, UserRole
from app.domain.schools import School, SchoolConfig
from app.domain.taxonomy import Classroom, Division, Grade
from app.domain.users import User
from app.services.ai.ocr import OcrResult
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from tests.fakes import (
    FakeClock,
    FakeTokenService,
    InMemoryStorage,
    MemoryRateLimiterStub,
)


class _NoopOcr:
    """No-op OCR provider for tests."""

    async def extract_text(self, data: bytes, mime_type: str) -> OcrResult:
        """Return an empty OCR result.

        Args:
            data: Unused bytes.
            mime_type: Unused MIME type.

        Returns:
            Empty result.
        """
        _ = data, mime_type
        return OcrResult(text="", confidence=None)


@pytest.fixture
def clock() -> FakeClock:
    """Controllable clock for deterministic time behavior.

    Returns:
        Fake clock at a fixed epoch.
    """
    return FakeClock()


@pytest.fixture
def hasher() -> BcryptPasswordHasher:
    """Real bcrypt hasher (a few hashes per test is cheap).

    Returns:
        Hasher instance.
    """
    return BcryptPasswordHasher()


@pytest.fixture
def storage() -> InMemoryStorage:
    """Shared in-memory object storage.

    Returns:
        Storage fake.
    """
    return InMemoryStorage()


@pytest.fixture
async def db_sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """In-memory sqlite sessionmaker with all tables created.

    Yields:
        Session factory over an in-memory database.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def container_factory(
    clock: FakeClock,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    hasher: BcryptPasswordHasher,
    storage: InMemoryStorage,
) -> Callable[[], Container]:
    """Build API containers with fakes wired in place of infra.

    Returns:
        Factory producing a fully wired test container.
    """

    def _build() -> Container:
        """Assemble a container for tests.

        Returns:
            Test container.
        """
        settings = Settings(
            env="test",
            jwt_secret="test-secret",
            database_url="sqlite+aiosqlite://",
        )
        return Container(
            settings=settings,
            clock=clock,
            ids=Uuid7Provider(),
            hasher=hasher,
            token_service=FakeTokenService(clock),  # type: ignore[arg-type]
            rate_limiter=MemoryRateLimiterStub(clock),
            storage=storage,  # type: ignore[arg-type]
            http_client=None,  # type: ignore[arg-type]
            engine=None,  # type: ignore[arg-type]
            sessionmaker=db_sessionmaker,
            llm=None,
            batch_llm=None,
            ocr_registry={"off": _NoopOcr()},
        )

    return _build


@pytest.fixture
def app(container_factory: Callable[[], Container]) -> FastAPI:
    """FastAPI app built around the test container.

    Args:
        container_factory: Test container factory.

    Returns:
        Configured test application.
    """
    from app.main import build_app

    return build_app(container_factory())


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """Synchronous test client bound to the app.

    Args:
        app: Test application.

    Returns:
        Test client.
    """
    return TestClient(app)


@pytest.fixture
async def seeded_ids(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    hasher: BcryptPasswordHasher,
) -> dict[str, str]:
    """Seed school/users/classroom and return their ids.

    Args:
        db_sessionmaker: Session factory.
        hasher: Password hasher.

    Returns:
        Mapping with school/admin/teacher/student/class/grade/division ids.
    """
    async with session_scope(db_sessionmaker) as session:
        school = School(
            name="Test Academy",
            school_key="test-academy",
            admin_key="test-admin-key",
        )
        session.add(school)
        await session.flush()
        session.add(SchoolConfig(school_id=str(school.id), enable_logging=True))
        grade = Grade(school_id=str(school.id), name="Grade 9", display_order=9)
        division = Division(school_id=str(school.id), name="A")
        session.add_all([grade, division])
        await session.flush()
        classroom = Classroom(
            school_id=str(school.id),
            grade_id=str(grade.id),
            division_id=str(division.id),
            academic_year="2025-2026",
        )
        session.add(classroom)
        admin = User(
            email="admin@test.edu",
            name="Ada Admin",
            role=UserRole.ADMIN,
            school_id=str(school.id),
            password_hash=hasher.hash("Admin@123"),
            auth_method=AuthMethod.CREDENTIALS,
        )
        teacher = User(
            email="teacher@test.edu",
            name="Tara Teacher",
            role=UserRole.TEACHER,
            school_id=str(school.id),
            password_hash=hasher.hash("Teacher@123"),
            auth_method=AuthMethod.CREDENTIALS,
        )
        student = User(
            email="student@test.edu",
            name="Sam Student",
            role=UserRole.STUDENT,
            school_id=str(school.id),
            grade_id=str(grade.id),
            division_id=str(division.id),
            password_hash=hasher.hash("Student@123"),
            auth_method=AuthMethod.CREDENTIALS,
        )
        session.add_all([admin, teacher, student])
        await session.flush()
        return {
            "school": str(school.id),
            "grade": str(grade.id),
            "division": str(division.id),
            "class": str(classroom.id),
            "admin": str(admin.id),
            "teacher": str(teacher.id),
            "student": str(student.id),
        }


def principal_for(ids: dict[str, str], role: UserRole) -> UserPrincipal:
    """Build a principal for a seeded user id.

    Args:
        ids: Seeded id mapping.
        role: Role for the principal.

    Returns:
        Principal scoped to the seeded school.
    """
    key = {
        UserRole.ADMIN: "admin",
        UserRole.TEACHER: "teacher",
        UserRole.STUDENT: "student",
    }[role]
    return UserPrincipal(
        id=uuid_compat.UUID(ids[key]),
        email=f"{key}@test.edu",
        name=key.title(),
        role=role.value,
        school_id=ids["school"],
        school_name="Test Academy",
        school_key="test-academy",
        enable_logging=True,
    )
