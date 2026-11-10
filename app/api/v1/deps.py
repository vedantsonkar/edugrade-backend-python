"""Dependency-injection composition root.

:class:`Container` owns process-wide infrastructure (settings, engine, redis,
storage, security services) and binds ports to adapters — the ONLY module
that instantiates concrete implementations (AGENTS.md §2-D). Request-scoped
collaborators (sessions, repositories, services) are built per request by the
provider functions below.
"""

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta

import httpx
import redis.asyncio as aioredis
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.clock import Clock, SystemClock
from app.core.config import Settings, get_settings
from app.core.db.base import Base
from app.core.db.engine import build_engine, build_sessionmaker, session_scope
from app.core.ids import IdProvider, Uuid7Provider
from app.core.ratelimit.base import RateLimiter
from app.core.ratelimit.memory import MemoryRateLimiter
from app.core.ratelimit.redis_limiter import RedisRateLimiter
from app.core.security.passwords import BcryptPasswordHasher, PasswordHasher
from app.core.security.tokens import Hs256TokenService, TokenService
from app.core.storage.base import ObjectStorage
from app.core.storage.s3 import SeaweedS3Storage
from app.repositories.assessments import (
    AssessmentAttemptRepository,
    StudentAnswerRepository,
)
from app.repositories.assignments import (
    AssignmentRepository,
    AssignmentTargetRepository,
    QuestionRepository,
    RubricCommentRepository,
)
from app.repositories.audit import AuditLogRepository
from app.repositories.grading import (
    AttachmentRepository,
    BatchGradingTaskRepository,
    SubmissionRepository,
)
from app.repositories.schools import SchoolConfigRepository, SchoolRepository
from app.repositories.taxonomy import (
    ClassroomRepository,
    DivisionRepository,
    GradeRepository,
)
from app.repositories.users import RefreshTokenRepository, UserRepository
from app.services.admin import AdminService
from app.services.ai.batching import (
    BatchGradingClient,
    GradingProviderBundle,
    resolve_grading_provider,
)
from app.services.ai.llm import LlmGrader
from app.services.ai.ocr import OcrProvider, build_ocr_registry
from app.services.ai.registry import build_grading_registry
from app.services.assessments import AssessmentService
from app.services.assignments import AssignmentService
from app.services.audit import AuditService
from app.services.auth import AuthService
from app.services.files import FileService
from app.services.grading.direct import DirectGradingService
from app.services.grading.strategies import build_registry
from app.services.oauth import GoogleOAuthService
from app.services.profile import ProfileService
from app.services.schools import SchoolService

logger = logging.getLogger(__name__)


@dataclass
class Container:
    """Process-wide dependency container and composition root.

    Attributes:
        settings: Parsed application settings.
        clock: System clock.
        ids: UUIDv7 id provider.
        hasher: bcrypt password hasher.
        token_service: HS256 token service.
        rate_limiter: Redis (or memory-fallback) limiter.
        storage: SeaweedFS storage adapter.
        http_client: Shared async HTTP client (OAuth, OCR).
        engine: Async SQLAlchemy engine.
        sessionmaker: Session factory.
        redis: Redis client (``None`` when unavailable → memory limiter).
        llm: Direct grader on the :class:`LlmGrader` port (``None`` when
            unconfigured).
        batch_llm: Batch client on the :class:`BatchGradingClient` port
            (``None`` when unconfigured).
        grading_registry: Named grading provider bundles.
        ocr_registry: Named OCR providers.
    """

    settings: Settings
    clock: Clock
    ids: IdProvider
    hasher: PasswordHasher
    token_service: TokenService
    rate_limiter: RateLimiter
    storage: ObjectStorage
    http_client: httpx.AsyncClient
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: aioredis.Redis | None = None
    llm: LlmGrader | None = field(default=None)
    batch_llm: BatchGradingClient | None = field(default=None)
    grading_registry: dict[str, GradingProviderBundle] = field(default_factory=dict)
    ocr_registry: dict[str, OcrProvider] = field(default_factory=dict)

    @classmethod
    def build(cls, settings: Settings | None = None) -> Container:
        """Assemble the container from settings.

        Redis is optional at boot: when unreachable we fall back to the
        in-memory limiter and log a warning (single-worker correctness).

        Args:
            settings: Optional pre-built settings (defaults to env parsing).

        Returns:
            The assembled container.
        """
        settings = settings or get_settings()

        token_service = Hs256TokenService(
            secret=settings.jwt_secret,
            access_ttl=timedelta(minutes=settings.access_token_expire_minutes),
            refresh_ttl=timedelta(days=settings.refresh_token_expire_days),
            clock=SystemClock(),
        )

        redis_client: aioredis.Redis | None = None
        rate_limiter: RateLimiter
        try:
            redis_client = aioredis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=2.0,
                socket_timeout=2.0,
            )
            rate_limiter = RedisRateLimiter(redis_client)
        except Exception as exc:  # pragma: no cover - environment-dependent
            logger.warning("Redis unavailable (%s); using in-memory rate limiter.", exc)
            rate_limiter = MemoryRateLimiter()

        storage = SeaweedS3Storage(settings)

        grading_registry = build_grading_registry(settings)
        bundle = resolve_grading_provider(grading_registry, settings.grading_provider)
        llm = bundle.llm if bundle else None
        batch_llm = bundle.batch if bundle else None

        http_client = httpx.AsyncClient(timeout=30.0)
        ocr_registry = build_ocr_registry(settings, http_client)

        engine = build_engine(settings)
        return cls(
            settings=settings,
            clock=SystemClock(),
            ids=Uuid7Provider(),
            hasher=BcryptPasswordHasher(),
            token_service=token_service,
            rate_limiter=rate_limiter,
            storage=storage,
            http_client=http_client,
            engine=engine,
            sessionmaker=build_sessionmaker(engine),
            redis=redis_client,
            llm=llm,
            batch_llm=batch_llm,
            grading_registry=grading_registry,
            ocr_registry=ocr_registry,
        )

    async def dispose(self) -> None:
        """Release engine, redis, and HTTP resources on shutdown."""
        await self.engine.dispose()
        await self.http_client.aclose()
        if self.redis is not None:
            await self.redis.aclose()

    async def create_tables(self) -> None:
        """Create all tables (development/test bootstrap convenience)."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)


# ── Request-scoped providers ─────────────────────────────────────────────────


def get_container(request: Request) -> Container:
    """Fetch the app-level container.

    Args:
        request: Incoming request.

    Returns:
        The container stored on app state.
    """
    container: Container = request.app.state.container
    return container


ContainerDep = Depends(get_container)
"""Annotated-ready dependency for the app container."""


async def get_session(
    container: Container = ContainerDep,
) -> AsyncIterator[AsyncSession]:
    """Yield a request-scoped transactional session.

    Args:
        container: App container providing the session factory.

    Yields:
        An async session committing on success.
    """
    async with session_scope(container.sessionmaker) as session:
        yield session


SessionDep = Depends(get_session)
"""Annotated-ready dependency for request-scoped sessions."""


def _ocr_provider(container: Container) -> OcrProvider:
    """Resolve the configured OCR provider.

    Args:
        container: App container.

    Returns:
        The provider named by settings (fallback: first registered).
    """
    return container.ocr_registry.get(container.settings.ocr_provider) or next(
        iter(container.ocr_registry.values())
    )


# ── Repositories ─────────────────────────────────────────────────────────────


def get_user_repo(session: AsyncSession = SessionDep) -> UserRepository:
    """Build the user repository for this request.

    Args:
        session: Request session.

    Returns:
        Repository instance.
    """
    return UserRepository(session)


def get_refresh_token_repo(session: AsyncSession = SessionDep) -> RefreshTokenRepository:
    """Build the refresh-token repository for this request.

    Args:
        session: Request session.

    Returns:
        Repository instance.
    """
    return RefreshTokenRepository(session)


def get_school_repo(session: AsyncSession = SessionDep) -> SchoolRepository:
    """Build the school repository for this request.

    Args:
        session: Request session.

    Returns:
        Repository instance.
    """
    return SchoolRepository(session)


def get_school_config_repo(session: AsyncSession = SessionDep) -> SchoolConfigRepository:
    """Build the school-config repository for this request.

    Args:
        session: Request session.

    Returns:
        Repository instance.
    """
    return SchoolConfigRepository(session)


def get_grade_repo(session: AsyncSession = SessionDep) -> GradeRepository:
    """Build the grade repository for this request.

    Args:
        session: Request session.

    Returns:
        Repository instance.
    """
    return GradeRepository(session)


def get_division_repo(session: AsyncSession = SessionDep) -> DivisionRepository:
    """Build the division repository for this request.

    Args:
        session: Request session.

    Returns:
        Repository instance.
    """
    return DivisionRepository(session)


def get_classroom_repo(session: AsyncSession = SessionDep) -> ClassroomRepository:
    """Build the classroom repository for this request.

    Args:
        session: Request session.

    Returns:
        Repository instance.
    """
    return ClassroomRepository(session)


def get_assignment_repo(session: AsyncSession = SessionDep) -> AssignmentRepository:
    """Build the assignment repository for this request.

    Args:
        session: Request session.

    Returns:
        Repository instance.
    """
    return AssignmentRepository(session)


def get_assignment_target_repo(
    session: AsyncSession = SessionDep,
) -> AssignmentTargetRepository:
    """Build the assignment-target repository for this request.

    Args:
        session: Request session.

    Returns:
        Repository instance.
    """
    return AssignmentTargetRepository(session)


def get_question_repo(session: AsyncSession = SessionDep) -> QuestionRepository:
    """Build the question repository for this request.

    Args:
        session: Request session.

    Returns:
        Repository instance.
    """
    return QuestionRepository(session)


def get_rubric_comment_repo(
    session: AsyncSession = SessionDep,
) -> RubricCommentRepository:
    """Build the rubric-comment repository for this request.

    Args:
        session: Request session.

    Returns:
        Repository instance.
    """
    return RubricCommentRepository(session)


def get_attempt_repo(
    session: AsyncSession = SessionDep,
) -> AssessmentAttemptRepository:
    """Build the attempt repository for this request.

    Args:
        session: Request session.

    Returns:
        Repository instance.
    """
    return AssessmentAttemptRepository(session)


def get_answer_repo(session: AsyncSession = SessionDep) -> StudentAnswerRepository:
    """Build the student-answer repository for this request.

    Args:
        session: Request session.

    Returns:
        Repository instance.
    """
    return StudentAnswerRepository(session)


def get_submission_repo(session: AsyncSession = SessionDep) -> SubmissionRepository:
    """Build the submission repository for this request.

    Args:
        session: Request session.

    Returns:
        Repository instance.
    """
    return SubmissionRepository(session)


def get_attachment_repo(session: AsyncSession = SessionDep) -> AttachmentRepository:
    """Build the attachment repository for this request.

    Args:
        session: Request session.

    Returns:
        Repository instance.
    """
    return AttachmentRepository(session)


def get_batch_task_repo(
    session: AsyncSession = SessionDep,
) -> BatchGradingTaskRepository:
    """Build the batch-task repository for this request.

    Args:
        session: Request session.

    Returns:
        Repository instance.
    """
    return BatchGradingTaskRepository(session)


def get_audit_repo(session: AsyncSession = SessionDep) -> AuditLogRepository:
    """Build the audit-log repository for this request.

    Args:
        session: Request session.

    Returns:
        Repository instance.
    """
    return AuditLogRepository(session)


# ── Services ─────────────────────────────────────────────────────────────────


def get_auth_service(
    session: AsyncSession = SessionDep,
    container: Container = ContainerDep,
) -> AuthService:
    """Build the auth service for this request.

    Args:
        session: Request session.
        container: App container (security ports).

    Returns:
        Service instance.
    """
    return AuthService(
        users=UserRepository(session),
        refresh_tokens=RefreshTokenRepository(session),
        schools=SchoolRepository(session),
        hasher=container.hasher,
        token_service=container.token_service,
        clock=container.clock,
    )


def get_assignment_service(
    session: AsyncSession = SessionDep,
    container: Container = ContainerDep,
) -> AssignmentService:
    """Build the assignment service for this request.

    Args:
        session: Request session.
        container: App container.

    Returns:
        Service instance.
    """
    return AssignmentService(
        assignments=AssignmentRepository(session),
        targets=AssignmentTargetRepository(session),
        questions=QuestionRepository(session),
        comments=RubricCommentRepository(session),
        submissions=SubmissionRepository(session),
        attachments=AttachmentRepository(session),
        tasks=BatchGradingTaskRepository(session),
        classrooms=ClassroomRepository(session),
        clock=container.clock,
    )


def get_assessment_service(
    session: AsyncSession = SessionDep,
    container: Container = ContainerDep,
) -> AssessmentService:
    """Build the assessment service for this request.

    Args:
        session: Request session.
        container: App container.

    Returns:
        Service instance.
    """
    return AssessmentService(
        attempts=AssessmentAttemptRepository(session),
        answers=StudentAnswerRepository(session),
        questions=QuestionRepository(session),
        assignments=AssignmentRepository(session),
        assignment_service=AssignmentService(
            assignments=AssignmentRepository(session),
            targets=AssignmentTargetRepository(session),
            questions=QuestionRepository(session),
            comments=RubricCommentRepository(session),
            submissions=SubmissionRepository(session),
            attachments=AttachmentRepository(session),
            tasks=BatchGradingTaskRepository(session),
            classrooms=ClassroomRepository(session),
            clock=container.clock,
        ),
        strategies=build_registry(container.llm),
        storage=container.storage,
        clock=container.clock,
        ids=container.ids,
    )


def get_direct_grading_service(
    session: AsyncSession = SessionDep,
    container: Container = ContainerDep,
) -> DirectGradingService:
    """Build the direct-grading service for this request.

    Args:
        session: Request session.
        container: App container.

    Returns:
        Service instance.
    """
    return DirectGradingService(
        submissions=SubmissionRepository(session),
        configs=SchoolConfigRepository(session),
        storage=container.storage,
        llm=container.llm,
        ocr=_ocr_provider(container),
    )


def get_file_service(
    session: AsyncSession = SessionDep, container: Container = ContainerDep
) -> FileService:
    """Build the file service.

    Args:
        container: App container.
        session: Request-scoped persistence.

    Returns:
        Service instance.
    """
    return FileService(
        container.storage, UserRepository(session), get_assignment_service(session, container)
    )


def get_school_service(
    session: AsyncSession = SessionDep, container: Container = ContainerDep
) -> SchoolService:
    """Build the school service for this request.

    Args:
        session: Request session.
        container: Storage configuration and adapters.

    Returns:
        Service instance.
    """
    return SchoolService(
        storage=container.storage,
        schools=SchoolRepository(session),
        configs=SchoolConfigRepository(session),
    )


def get_profile_service(
    session: AsyncSession = SessionDep,
    container: Container = ContainerDep,
) -> ProfileService:
    """Build the profile service for this request.

    Args:
        session: Request session.
        container: App container.

    Returns:
        Service instance.
    """
    return ProfileService(
        users=UserRepository(session),
        hasher=container.hasher,
        storage=container.storage,
    )


def get_audit_service(
    session: AsyncSession = SessionDep,
    container: Container = ContainerDep,
) -> AuditService:
    """Build the audit service for this request.

    Args:
        session: Request session.
        container: App container.

    Returns:
        Service instance.
    """
    return AuditService(
        logs=AuditLogRepository(session),
        storage=container.storage,
    )


def get_admin_service(
    session: AsyncSession = SessionDep,
    container: Container = ContainerDep,
) -> AdminService:
    """Build the admin service for this request.

    Args:
        session: Request session.
        container: App container.

    Returns:
        Service instance.
    """
    return AdminService(
        session,
        schools=SchoolRepository(session),
        configs=SchoolConfigRepository(session),
        grades=GradeRepository(session),
        divisions=DivisionRepository(session),
        classrooms=ClassroomRepository(session),
        users=UserRepository(session),
        assignments=AssignmentRepository(session),
        hasher=container.hasher,
        ids=container.ids,
    )


def get_google_oauth_service(
    session: AsyncSession = SessionDep,
    container: Container = ContainerDep,
) -> GoogleOAuthService:
    """Build the Google OAuth service for this request.

    Args:
        session: Request session.
        container: App container.

    Returns:
        Service instance.
    """
    return GoogleOAuthService(
        settings=container.settings,
        schools=SchoolRepository(session),
        users=UserRepository(session),
        refresh_tokens=RefreshTokenRepository(session),
        token_service=container.token_service,
        clock=container.clock,
        id_provider=container.ids,
        http_client=container.http_client,
    )
