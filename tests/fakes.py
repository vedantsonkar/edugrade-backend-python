"""In-memory fakes for every SOLID port (used by unit + integration tests).

These fakes are substitutable implementations of the core protocols
(AGENTS.md §2-L): services run against them unchanged.
"""

import time
from dataclasses import dataclass, field
from uuid import UUID

import jwt
from app.core.ids import Uuid7Provider
from app.core.ratelimit.base import RateLimitResult
from app.core.security.tokens import (
    ACCESS_TOKEN_TYPE,
    REFRESH_TOKEN_TYPE,
    RefreshTokenIssuance,
)
from app.core.storage.base import PresignedDownload, PresignedUpload, StoredObjectInfo


class FakeClock:
    """Controllable clock for deterministic time-dependent tests."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        """Start the fake at a fixed epoch.

        Args:
            start: Initial unix timestamp.
        """
        self._current = start

    def now(self) -> object:
        """Return the current fake time.

        Returns:
            Timezone-aware datetime of the fake clock.
        """
        from datetime import UTC, datetime

        return datetime.fromtimestamp(self._current, tz=UTC)

    def advance(self, seconds: float) -> None:
        """Move the fake clock forward.

        Args:
            seconds: Seconds to add.
        """
        self._current += seconds


class FakeTokenService:
    """Deterministic token service: base64 payloads, no signatures."""

    def __init__(self, clock: FakeClock, access_ttl: int = 900, refresh_ttl: int = 604800) -> None:
        """Bind the fake to a clock.

        Args:
            clock: Time source.
            access_ttl: Access lifetime seconds.
            refresh_ttl: Refresh lifetime seconds.
        """
        self._clock = clock
        self._access_ttl = access_ttl
        self._refresh_ttl = refresh_ttl

    def create_access_token(self, principal: object) -> str:
        """Encode the principal's claims as a fake token.

        Args:
            principal: Principal to serialize.

        Returns:
            Fake token string.
        """
        import base64
        import json

        claims = principal.to_claims()
        claims.update({"typ": ACCESS_TOKEN_TYPE, "exp": self._clock._current + self._access_ttl})
        return "fake." + base64.b64encode(json.dumps(claims).encode()).decode()

    def create_refresh_token(self, user_id: UUID) -> RefreshTokenIssuance:
        """Issue a fake refresh token with a fresh jti.

        Args:
            user_id: Owner.

        Returns:
            Issuance data.
        """
        import base64
        import json
        from datetime import UTC, datetime

        jti = Uuid7Provider().new_id()
        payload = {
            "sub": str(user_id),
            "typ": REFRESH_TOKEN_TYPE,
            "jti": str(jti),
            "exp": self._clock._current + self._refresh_ttl,
        }
        token = "fake-refresh." + base64.b64encode(json.dumps(payload).encode()).decode()
        return RefreshTokenIssuance(
            token=token,
            jti=jti,
            expires_at=datetime.fromtimestamp(self._clock._current + self._refresh_ttl, tz=UTC),
        )

    def decode(self, token: str, expected_type: str) -> dict[str, object]:
        """Decode a fake token, enforcing type and expiry.

        Args:
            token: Fake token string.
            expected_type: Required typ claim.

        Returns:
            Claims mapping.

        Raises:
            jwt.InvalidTokenError: On unknown/expired/type-mismatched tokens
                (mirroring the real service's contract for guards).
        """
        import base64
        import json

        try:
            payload_part = token.split(".", 1)[1]
            claims: dict[str, object] = json.loads(base64.b64decode(payload_part + "=="))
        except (IndexError, ValueError) as exc:
            raise jwt.InvalidTokenError("malformed fake token") from exc
        if claims.get("typ") != expected_type:
            raise jwt.InvalidTokenError("type mismatch")
        if float(claims["exp"]) <= self._clock._current:
            raise jwt.InvalidTokenError("expired")
        return claims


class MemoryRateLimiterStub:
    """Minimal fixed-window limiter with injectable clock (deterministic)."""

    def __init__(self, clock: FakeClock) -> None:
        """Bind to the fake clock.

        Args:
            clock: Time source.
        """
        self._clock = clock
        self._counters: dict[str, tuple[int, int]] = {}

    async def check(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        """Record a hit and evaluate the fixed window.

        Args:
            key: Bucket key.
            limit: Max requests per window.
            window_seconds: Window length.

        Returns:
            Evaluation result.
        """
        epoch = int(self._clock._current // window_seconds)
        current_epoch, hits = self._counters.get(key, (epoch, 0))
        if current_epoch != epoch:
            hits = 0
        hits += 1
        self._counters[key] = (epoch, hits)
        if hits > limit:
            return RateLimitResult(
                allowed=False,
                limit=limit,
                remaining=0,
                reset_after=window_seconds,
                retry_after=window_seconds,
            )
        return RateLimitResult(
            allowed=True,
            limit=limit,
            remaining=max(0, limit - hits),
            reset_after=window_seconds,
        )


@dataclass
class FakeFile:
    """A stored object in :class:`InMemoryStorage`.

    Attributes:
        data: Object bytes.
        content_type: Stored MIME type.
    """

    data: bytes
    content_type: str


@dataclass
class InMemoryStorage:
    """Dict-backed :class:`ObjectStorage` fake."""

    objects: dict[str, FakeFile] = field(default_factory=dict)

    async def presign_put(self, key: str, content_type: str | None) -> PresignedUpload:
        """Fake a PUT grant.

        Args:
            key: Destination key.
            content_type: Optional MIME type.

        Returns:
            Upload grant with a marker URL.
        """
        return PresignedUpload(key=key, url=f"http://storage.test/put/{key}", expires_in=600)

    async def presign_get(self, key: str) -> PresignedDownload:
        """Fake a GET grant.

        Args:
            key: Source key.

        Returns:
            Download grant with a marker URL.
        """
        return PresignedDownload(key=key, url=f"http://storage.test/get/{key}", expires_in=600)

    async def get_bytes(self, key: str) -> bytes:
        """Read a stored object.

        Args:
            key: Source key.

        Returns:
            Object bytes.

        Raises:
            KeyError: When the key is absent.
        """
        return self.objects[key].data

    async def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        """Store bytes.

        Args:
            key: Destination key.
            data: Payload.
            content_type: MIME type.
        """
        self.objects[key] = FakeFile(data=data, content_type=content_type)

    async def delete(self, key: str) -> None:
        """Delete an object if present.

        Args:
            key: Key to delete.
        """
        self.objects.pop(key, None)

    async def list_objects(self, prefix: str) -> list[StoredObjectInfo]:
        """Return fake metadata using the same prefix contract as S3."""
        return [
            StoredObjectInfo(key=key, size=len(self.objects[key].data), last_modified=None)
            for key in await self.list_keys(prefix)
        ]

    async def list_keys(self, prefix: str) -> list[str]:
        """List keys under a prefix.

        Args:
            prefix: Prefix to scan.

        Returns:
            Matching keys.
        """
        return sorted(k for k in self.objects if k.startswith(prefix))

    async def check(self) -> None:
        """Always reachable."""
        return None


class FakeLlmGrader:
    """Deterministic :class:`LlmGrader` fake."""

    def __init__(self, score: float = 3.0) -> None:
        """Configure the fixed score returned.

        Args:
            score: Score to return for free-text grading.
        """
        self.score = score
        self.calls = 0

    async def grade_free_text(
        self, *, question_text: str, student_answer: str, max_points: int
    ) -> object:
        """Return a fixed grading result.

        Args:
            question_text: Unused.
            student_answer: Unused.
            max_points: Upper bound of the score.

        Returns:
            Result-like object with score/summary/feedback.
        """
        from app.services.ai.llm import LlmGradeResult

        self.calls += 1
        return LlmGradeResult(
            score=min(self.score, float(max_points)),
            summary="Good attempt.",
            feedback="Well structured; add examples.",
            plagiarism_score=None,
            plagiarism_analysis=None,
        )


class FrozenClock(FakeClock):
    """Alias of :class:`FakeClock` for readability in fixtures."""

    def elapsed_since(self, start: float) -> float:
        """Seconds elapsed since a stored instant.

        Args:
            start: Earlier unix timestamp.

        Returns:
            Elapsed seconds.
        """
        return self._current - start


def make_time_helper() -> tuple[FakeClock, object]:
    """Build a clock plus a small sleep-like advancer for tests.

    Returns:
        Tuple of (clock, advance(seconds) callable).
    """
    clock = FakeClock()

    def advance(seconds: float) -> None:
        """Advance the fake clock.

        Args:
            seconds: Seconds to add.
        """
        clock.advance(seconds)
        time.sleep(0)

    return clock, advance
