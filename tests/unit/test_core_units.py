"""Unit tests: tokens, rate limiting, grading strategies, storage keys."""

from uuid import uuid4

import pytest
from app.core.errors import ForbiddenError, ValidationFailedError
from app.core.ratelimit.base import parse_limit
from app.core.storage.keys import (
    audit_archive_key,
    ensure_key_accessible,
    sanitize_filename,
    upload_key,
)
from app.domain.assessments import AssessmentAttempt, StudentAnswer
from app.domain.assignments import Question, QuestionOption
from app.domain.enums import AttemptStatus, QuestionType
from app.services.grading.strategies import (
    McqMultipleStrategy,
    McqSingleStrategy,
    TrueFalseStrategy,
    build_registry,
)
from tests.fakes import FakeClock, FakeLlmGrader, FakeTokenService


def _question(qtype: QuestionType, correct: list[int], points: int = 2) -> Question:
    """Build an objective question with options.

    Args:
        qtype: Question type.
        correct: Indices (1-based) of correct options.
        points: Points value.

    Returns:
        Question with options attached.
    """
    question = Question(assignment_id="a1", type=qtype, text="Q?", points=points,
                        display_order=1)
    question.id = uuid4()
    for index in range(1, 4):
        option = QuestionOption(
            question_id=str(question.id),
            text=f"Option {index}",
            is_correct=index in correct,
            display_order=index,
        )
        option.id = uuid4()
        question.options.append(option)
    return question


def _answer(question: Question, raw: object) -> StudentAnswer:
    """Build a saved answer for a question.

    Args:
        question: Owning question.
        raw: Raw payload.

    Returns:
        Answer entity.
    """
    return StudentAnswer(
        attempt_id="t1", question_id=str(question.id), answer_data=raw
    )


class TestGradingStrategies:
    """Strategy family behavior (OCP centerpiece)."""

    async def test_mcq_single_correct_full_points(self) -> None:
        """Correct selection earns full points."""
        question = _question(QuestionType.MCQ_SINGLE, correct=[2])
        answer = _answer(question, str(question.options[1].id))
        outcome = await McqSingleStrategy().grade(question, answer)
        assert outcome.is_correct is True
        assert outcome.points_awarded == 2.0

    async def test_mcq_single_wrong_zero(self) -> None:
        """Wrong selection earns zero."""
        question = _question(QuestionType.MCQ_SINGLE, correct=[2])
        answer = _answer(question, str(question.options[0].id))
        outcome = await McqSingleStrategy().grade(question, answer)
        assert outcome.is_correct is False
        assert outcome.points_awarded == 0.0

    async def test_mcq_multiple_requires_exact_set(self) -> None:
        """Partial multi-selection earns zero; exact set earns full."""
        question = _question(QuestionType.MCQ_MULTIPLE, correct=[1, 3])
        partial = await McqMultipleStrategy().grade(
            question, _answer(question, [str(question.options[0].id)])
        )
        exact = await McqMultipleStrategy().grade(
            question,
            _answer(question, [str(question.options[0].id), str(question.options[2].id)]),
        )
        assert partial.is_correct is False and partial.points_awarded == 0.0
        assert exact.is_correct is True and exact.points_awarded == 2.0

    async def test_true_false_bool_answer(self) -> None:
        """Boolean answers match against the correct option text."""
        question = _question(QuestionType.TRUE_FALSE, correct=[1])
        question.options[0].text = "True"
        question.options[1].text = "False"
        outcome = await TrueFalseStrategy().grade(question, _answer(question, True))
        assert outcome.is_correct is True
        assert outcome.points_awarded == 2.0

    async def test_registry_covers_all_types(self) -> None:
        """Every question type resolves to a strategy."""
        registry = build_registry(FakeLlmGrader())  # type: ignore[arg-type]
        assert set(registry) == set(QuestionType)

    async def test_free_text_ungraded_without_llm(self) -> None:
        """Without a grader, free text stays ungraded (never fails submit)."""
        question = _question(QuestionType.FREE_TEXT, correct=[])
        answer = _answer(question, "Plants need light for energy.")
        outcome = await build_registry(None)[QuestionType.FREE_TEXT].grade(
            question, answer
        )
        assert outcome.is_correct is None
        assert outcome.points_awarded is None


class TestAttemptBehavior:
    """Entity-owned invariants."""

    def test_tab_switch_terminates_past_max(self) -> None:
        """The third switch signals termination (service then closes it)."""
        from datetime import UTC, datetime

        attempt = AssessmentAttempt(
            assignment_id="a", student_id="s",
            status=AttemptStatus.IN_PROGRESS, tab_switch_count=0,
        )
        assert attempt.register_tab_switch(max_switches=2, enable_detection=True) is False
        assert attempt.register_tab_switch(max_switches=2, enable_detection=True) is False
        assert attempt.register_tab_switch(max_switches=2, enable_detection=True) is True
        attempt.terminate(at=datetime.now(UTC))
        assert attempt.status is AttemptStatus.TERMINATED

    def test_tab_detection_disabled_never_terminates(self) -> None:
        """With detection off, counters stay put and no termination occurs."""
        attempt = AssessmentAttempt(
            assignment_id="a", student_id="s",
            status=AttemptStatus.IN_PROGRESS, tab_switch_count=0,
        )
        for _ in range(5):
            assert attempt.register_tab_switch(max_switches=1, enable_detection=False) is False
        assert attempt.tab_switch_count == 0

    def test_effective_score_prefers_override(self) -> None:
        """Override wins over the computed score."""
        attempt = AssessmentAttempt(
            assignment_id="a", student_id="s", score=5.0,
            status=AttemptStatus.IN_PROGRESS, tab_switch_count=0,
        )
        assert attempt.effective_score == 5.0
        attempt.grade_override = 8.0
        attempt.is_overridden = True
        assert attempt.effective_score == 8.0


class TestRateLimitSpec:
    """Limit-spec parsing."""

    def test_parses_minute(self) -> None:
        """'30/minute' parses to (30, 60)."""
        assert parse_limit("30/minute") == (30, 60)

    def test_rejects_bad_unit(self) -> None:
        """Unknown units fail validation."""
        with pytest.raises(ValidationFailedError):
            parse_limit("5/fortnight")

    def test_rejects_bad_format(self) -> None:
        """Malformed specs fail validation."""
        with pytest.raises(ValidationFailedError):
            parse_limit("30")


class TestStorageKeys:
    """Key construction and scope enforcement."""

    def test_upload_key_is_scoped_and_safe(self) -> None:
        """Keys nest under the school and sanitize filenames."""
        key = upload_key("school-1", "../../etc/passwd")
        assert key.startswith("uploads/school-1/")
        assert ".." not in key

    def test_sanitize_filename_rejects_empty(self) -> None:
        """Unusable filenames are rejected."""
        with pytest.raises(ValidationFailedError):
            sanitize_filename("///")

    def test_student_cannot_read_other_school_uploads(self) -> None:
        """Cross-school upload reads are forbidden."""
        principal = type("P", (), {"id": uuid4(), "role": "STUDENT", "school_id": "school-1"})()
        with pytest.raises(ForbiddenError):
            ensure_key_accessible("uploads/school-2/file.pdf", principal)

    def test_student_can_read_own_school_uploads(self) -> None:
        """Same-school uploads are allowed."""
        principal = type("P", (), {"id": uuid4(), "role": "STUDENT", "school_id": "school-1"})()
        ensure_key_accessible("uploads/school-1/file.pdf", principal)

    def test_audit_archive_key_shape(self) -> None:
        """Archive keys follow the daily pattern."""
        from datetime import date

        key = audit_archive_key("school-1", date(2026, 11, 7))
        assert key == "audit-logs/school-1/2026-11-07.json"


class TestFakeTokenServiceContract:
    """The fake token service must obey the same contract as the real one."""

    def test_decode_enforces_type(self) -> None:
        """Refresh tokens do not decode as access tokens."""
        from app.core.security.principal import UserPrincipal

        clock = FakeClock()
        tokens = FakeTokenService(clock)
        principal = UserPrincipal(
            id=uuid4(), email="e@x.y", name="N", role="STUDENT", school_id="s1"
        )
        access = tokens.create_access_token(principal)
        with pytest.raises(ValueError):
            tokens.decode(access, expected_type="refresh")
        claims = tokens.decode(access, expected_type="access")
        assert claims["role"] == "STUDENT"

    def test_expired_tokens_rejected(self) -> None:
        """Expiry is enforced against the fake clock."""
        from app.core.security.principal import UserPrincipal

        clock = FakeClock()
        tokens = FakeTokenService(clock, access_ttl=10)
        principal = UserPrincipal(id=uuid4(), email="e@x.y", name="N", role="ADMIN")
        access = tokens.create_access_token(principal)
        clock.advance(11)
        with pytest.raises(ValueError):
            tokens.decode(access, expected_type="access")
