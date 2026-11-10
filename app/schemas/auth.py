"""Authentication wire schemas: login, tokens, session, Google OAuth."""

from pydantic import Field

from app.schemas.common import BaseRequest, BaseSchema, IdStr


class LoginRequest(BaseRequest):
    """Credentials login payload.

    Attributes:
        email: Account email.
        password: Account password.
        school_id: Optional tenant disambiguation; when given, the user must
            belong to that school.
        login_type: Optional client tab context (``student``/``admin``);
            ``admin`` rejects student accounts (staff-only login screen).
        admin_key: Optional school admin key; required whenever the account
            resolves to a staff role (teacher/admin).
    """

    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=1024)
    school_id: IdStr | None = None
    login_type: str | None = None
    admin_key: str | None = Field(default=None, min_length=1, max_length=255)


class TokenPair(BaseSchema):
    """Issued access/refresh tokens (for non-cookie clients).

    Attributes:
        access_token: Short-lived bearer token.
        refresh_token: Long-lived rotating token.
        expires_in: Access token lifetime in seconds.
    """

    access_token: str
    refresh_token: str
    expires_in: int


class AuthUserResponse(BaseSchema):
    """Public identity of the authenticated user.

    Attributes:
        id: User id.
        email: Email.
        name: Display name.
        role: Role string value.
        avatar_url: Avatar storage key, if any.
    """

    id: IdStr
    email: str
    name: str
    role: str
    avatar_url: str | None = None


class AuthSchoolResponse(BaseSchema):
    """School branding embedded in the session.

    Attributes:
        id: School id.
        name: Display name.
        primary_color: Brand hex color.
        logo_url: Logo storage key, if any.
        school_key: Public school key.
    """

    id: IdStr
    name: str
    primary_color: str
    logo_url: str | None = None
    school_key: str


class LoginResponse(BaseSchema):
    """Result of a successful login.

    Attributes:
        user: Authenticated identity.
        school: School branding.
        enable_logging: Whether client analytics are enabled for the school.
        tokens: Explicit token pair (cookies are set as well).
    """

    user: AuthUserResponse
    school: AuthSchoolResponse
    enable_logging: bool
    tokens: TokenPair | None = None


class RefreshRequest(BaseRequest):
    """Refresh grant; the token may also arrive via the ``refresh_token`` cookie.

    Attributes:
        refresh_token: Optional explicit refresh token.
    """

    refresh_token: str | None = None


class MeResponse(BaseSchema):
    """Current session snapshot returned by ``GET /auth/me``.

    Attributes:
        id: User id.
        email: Email.
        name: Display name.
        role: Role string value.
        school_id: School id.
        school_name: School display name.
        school_primary_color: Brand color.
        school_logo_url: Logo storage key.
        school_key: Public school key.
        enable_logging: Client analytics flag for the school.
    """

    id: IdStr
    email: str
    name: str
    role: str
    avatar_url: str | None = None
    school_id: IdStr
    school_name: str | None = None
    school_primary_color: str | None = None
    school_logo_url: str | None = None
    school_key: str | None = None
    enable_logging: bool = False
