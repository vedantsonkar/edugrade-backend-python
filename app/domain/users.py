"""User identity entities: :class:`User` and rotating :class:`RefreshToken`.

The user table is the identity anchor for credentials login and Google SSO
alike. Refresh tokens are persisted so they can be rotated on use and
revoked on logout (the legacy prototype's stateless refresh tokens could do
neither).
"""

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db.base import UUID_KEY, Base, TimestampMixin, UUID7PKMixin
from app.domain.enums import AuthMethod, UserRole

if TYPE_CHECKING:
    from app.domain.assessments import AssessmentAttempt
    from app.domain.assignments import Assignment, RubricComment
    from app.domain.grading import BatchGradingTask, Submission
    from app.domain.schools import School
    from app.domain.taxonomy import Classroom, Division, Grade


class User(UUID7PKMixin, TimestampMixin, Base):
    """A person belonging to exactly one school.

    Attributes:
        email: Login identifier, globally unique (Google identity aligns).
        name: Display name.
        role: Role within the school.
        avatar_url: Storage key of the avatar image, if any.
        school_id: Owning school.
        grade_id / division_id: Student placement (staff: ``None``).
        roll_number / student_identifier: Student bookkeeping fields.
        date_of_birth: Optional DOB; also the default password fallback.
        password_hash: bcrypt hash; ``None`` for pure-SSO users.
        auth_method: Which authentication paths the user may use.
        is_active: Soft-delete / deactivation switch.
        last_login_at: Last successful login instant.
        phone_country_code / phone_number / preferred_locale: Contact prefs.
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(default=UserRole.STUDENT, index=True)
    avatar_url: Mapped[str | None] = mapped_column(String(512))
    school_id: Mapped[str] = mapped_column(
        UUID_KEY,
        ForeignKey("schools.id", ondelete="CASCADE"), index=True
    )
    grade_id: Mapped[str | None] = mapped_column(
        UUID_KEY,
        ForeignKey("grades.id", ondelete="SET NULL"), index=True
    )
    division_id: Mapped[str | None] = mapped_column(
        UUID_KEY,
        ForeignKey("divisions.id", ondelete="SET NULL"), index=True
    )
    roll_number: Mapped[str | None] = mapped_column(String(64))
    student_identifier: Mapped[str | None] = mapped_column(String(64))
    date_of_birth: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    auth_method: Mapped[AuthMethod] = mapped_column(
        default=AuthMethod.SSO_GOOGLE, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    phone_country_code: Mapped[str | None] = mapped_column(String(8), default="+91")
    phone_number: Mapped[str | None] = mapped_column(String(15), index=True)
    preferred_locale: Mapped[str | None] = mapped_column(String(10))

    school: Mapped["School"] = relationship(back_populates="users", lazy="selectin")
    grade: Mapped["Grade | None"] = relationship(lazy="selectin")
    division: Mapped["Division | None"] = relationship(lazy="selectin")
    classes_taught: Mapped[list["Classroom"]] = relationship(
        back_populates="class_teacher"
    )
    assignments_created: Mapped[list["Assignment"]] = relationship(
        back_populates="teacher"
    )
    submissions: Mapped[list["Submission"]] = relationship(back_populates="student")
    attempts: Mapped[list["AssessmentAttempt"]] = relationship(
        back_populates="student"
    )
    rubric_comments: Mapped[list["RubricComment"]] = relationship(
        back_populates="author"
    )
    batch_tasks_created: Mapped[list["BatchGradingTask"]] = relationship(
        back_populates="created_by"
    )
    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def can_use_credentials(self) -> bool:
        """Whether credentials login is permitted for this user."""
        return self.auth_method in (AuthMethod.CREDENTIALS, AuthMethod.BOTH)

    @property
    def can_use_google(self) -> bool:
        """Whether Google SSO login is permitted for this user."""
        return self.auth_method in (AuthMethod.SSO_GOOGLE, AuthMethod.BOTH)


class RefreshToken(UUID7PKMixin, Base):
    """A persisted, single-use refresh token enabling rotation and revocation.

    Lifecycle: created on login/refresh → consumed on refresh (row marked
    ``revoked_at`` + ``replaced_by_id`` pointing at the successor) or revoked
    on logout. Expired rows are inert.

    Attributes:
        jti: Unique token id matching the JWT ``jti`` claim.
        user_id: Owner.
        expires_at: Absolute expiry (UTC).
        revoked_at: When the token was consumed or revoked (``None`` = live).
        replaced_by_id: ``jti`` (as UUID) of the successor token after rotation.
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        UniqueConstraint("jti", name="uq_refresh_tokens_jti"),
    )

    jti: Mapped[UUID] = mapped_column(UUID_KEY, index=True)
    user_id: Mapped[str] = mapped_column(
        UUID_KEY,
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by_id: Mapped[UUID | None] = mapped_column(UUID_KEY)

    user: Mapped["User"] = relationship(back_populates="refresh_tokens")

    @property
    def is_live(self) -> bool:
        """Whether the token is neither revoked nor expired.

        Expiry is additionally checked against the clock by the auth service.
        """
        return self.revoked_at is None
