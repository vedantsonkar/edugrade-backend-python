"""Grading strategy family - one subclass per question type (OCP).

Adding a new question type means adding a :class:`GradingStrategy` subclass
and registering it in :func:`build_registry`; the submit workflow never
changes. Objective strategies are pure; the free-text strategy delegates to
the injected :class:`LlmGrader` port.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.domain.assessments import StudentAnswer
from app.domain.assignments import Question
from app.domain.enums import QuestionType
from app.services.ai.llm import LlmGrader

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GradeOutcome:
    """Result of grading one answer.

    Attributes:
        is_correct: Correctness (``None`` for ungraded free text).
        points_awarded: Points granted (``None`` when ungraded).
    """

    is_correct: bool | None
    points_awarded: float | None


class GradingStrategy(ABC):
    """Base strategy for grading one answer against its question."""

    @abstractmethod
    async def grade(self, question: Question, answer: StudentAnswer) -> GradeOutcome:
        """Grade a saved answer.

        Args:
            question: Question being graded (options loaded).
            answer: The student's answer.

        Returns:
            Grading outcome.
        """
        ...


class ObjectiveStrategy(GradingStrategy):
    """Shared logic for option-based (objective) questions."""

    @staticmethod
    def _correct_option_ids(question: Question) -> set[str]:
        """Collect the ids of correct options.

        Args:
            question: Question with options loaded.

        Returns:
            Set of correct option ids.
        """
        return {str(o.id) for o in question.options if o.is_correct}

    @staticmethod
    def _as_id_set(raw: object) -> set[str]:
        """Normalize a raw answer payload into an option-id set.

        Args:
            raw: Answer payload (id string, list of ids, or bool).

        Returns:
            Set of ids (booleans map to their string form).
        """
        if isinstance(raw, str):
            return {raw}
        if isinstance(raw, (list, tuple)):
            return {str(item) for item in raw}
        return {str(raw)}


class McqSingleStrategy(ObjectiveStrategy):
    """Single-choice grading: full credit iff the selection is the correct one."""

    async def grade(self, question: Question, answer: StudentAnswer) -> GradeOutcome:
        """Grade a single-choice answer.

        Args:
            question: Question with options.
            answer: Student's answer.

        Returns:
            Full points when the selected option is correct, else zero.
        """
        selected = self._as_id_set(answer.answer_data)
        correct = self._correct_option_ids(question)
        is_correct = bool(selected) and selected == correct
        return GradeOutcome(
            is_correct=is_correct,
            points_awarded=float(question.points) if is_correct else 0.0,
        )


class McqMultipleStrategy(ObjectiveStrategy):
    """Multiple-choice grading: full credit iff the selection set matches."""

    async def grade(self, question: Question, answer: StudentAnswer) -> GradeOutcome:
        """Grade a multiple-choice answer.

        Args:
            question: Question with options.
            answer: Student's answer.

        Returns:
            Full points on exact set match, else zero.
        """
        selected = self._as_id_set(answer.answer_data)
        correct = self._correct_option_ids(question)
        is_correct = selected == correct
        return GradeOutcome(
            is_correct=is_correct,
            points_awarded=float(question.points) if is_correct else 0.0,
        )


class TrueFalseStrategy(ObjectiveStrategy):
    """True/false grading: answers arrive as the chosen option's id or a bool.

    Bool answers are matched against the correct option's text ("true" /
    "false"); string answers are matched as option ids (like single-choice).
    """

    async def grade(self, question: Question, answer: StudentAnswer) -> GradeOutcome:
        """Grade a true/false answer.

        Args:
            question: Question whose options carry ``True``/``False`` texts.
            answer: Student's answer.

        Returns:
            Full points when the choice matches, else zero.
        """
        raw = answer.answer_data
        if isinstance(raw, bool):
            correct_text = (correct_option_text(question) or "true").strip().lower()
            is_correct = raw == (correct_text == "true")
            return GradeOutcome(
                is_correct=is_correct,
                points_awarded=float(question.points) if is_correct else 0.0,
            )
        is_correct = self._as_id_set(raw) == self._correct_option_ids(question)
        return GradeOutcome(
            is_correct=is_correct,
            points_awarded=float(question.points) if is_correct else 0.0,
        )


def correct_option_text(question: Question) -> str | None:
    """Return the correct option's text for boolean comparison.

    Args:
        question: Question with options.

    Returns:
        Correct option text, or ``None``.
    """
    for option in question.options:
        if option.is_correct:
            return option.text
    return None


class FreeTextGeminiStrategy(GradingStrategy):
    """Free-text grading via the Gemini-backed :class:`LlmGrader` port.

    When no grader is configured (no API key) or the model call fails, the
    answer is left ungraded (``None`` outcome) rather than failing the
    submission - grading can be completed later via manual override or the
    batch pipeline.
    """

    def __init__(self, llm: LlmGrader | None) -> None:
        """Bind the LLM port.

        Args:
            llm: Grader implementation, or ``None`` when unconfigured.
        """
        self._llm = llm

    async def grade(self, question: Question, answer: StudentAnswer) -> GradeOutcome:
        """Grade a free-text answer with Gemini.

        Args:
            question: Question (text/points drive the prompt).
            answer: Student's answer text.

        Returns:
            Outcome with points; ungraded (``None``) on absence/failure.
        """
        if self._llm is None:
            logger.info("No LLM grader configured; leaving free text ungraded.")
            return GradeOutcome(is_correct=None, points_awarded=None)
        text = answer.answer_data if isinstance(answer.answer_data, str) else ""
        if not text.strip():
            return GradeOutcome(is_correct=False, points_awarded=0.0)
        try:
            result = await self._llm.grade_free_text(
                question_text=question.text,
                student_answer=text,
                max_points=question.points,
            )
        except Exception:
            logger.exception("Free-text grading failed for question %s", question.id)
            return GradeOutcome(is_correct=None, points_awarded=None)
        return GradeOutcome(
            is_correct=result.score >= question.points * 0.5,
            points_awarded=result.score,
        )


def build_registry(llm: LlmGrader | None) -> dict[QuestionType, GradingStrategy]:
    """Assemble the question-type → strategy registry.

    Args:
        llm: LLM grader for free-text questions (may be ``None``).

    Returns:
        Registry consumed by the assessment submit workflow.
    """
    return {
        QuestionType.MCQ_SINGLE: McqSingleStrategy(),
        QuestionType.MCQ_MULTIPLE: McqMultipleStrategy(),
        QuestionType.TRUE_FALSE: TrueFalseStrategy(),
        QuestionType.FREE_TEXT: FreeTextGeminiStrategy(llm),
    }
