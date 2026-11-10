"""Provider-agnostic grading contract.

The Pydantic models in this module are the single source of truth for AI
grading output: every provider adapter derives its wire schema from them
(``model_json_schema``) and validates model responses with the same model,
so swapping providers can never loosen the contract. Prompt builders and
the letter-anchor scale live here too - they are shared verbatim by the
direct and batch paths (AGENTS.md §2-O: behavior extensions happen behind
the provider registry, not by editing consumers).
"""

import json
import re
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError

from app.core.errors import ExternalServiceError

ALPHABET_SCALE: dict[str, float] = {"A": 4.0, "B": 3.0, "C": 2.0, "D": 1.0, "E": 0.5, "F": 0.0}
"""Letter-grade anchor scale embedded in prompts (ALPHABET grading type)."""

ANCHOR_LETTERS: dict[float, str] = {v: k for k, v in ALPHABET_SCALE.items()}
"""Inverse of :data:`ALPHABET_SCALE`: numeric anchor → letter grade."""

_FENCE_PATTERN = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class GradingResult(BaseModel):
    """Structured output contract for full-submission grading.

    Every provider adapter emits :func:`grading_response_schema` and parses
    model text back into this model, so the wire format is identical across
    providers. Letters are never requested from the model: ALPHABET
    assignments receive a numeric 0-4 anchor in ``score`` and the letter is
    derived server-side (:func:`letter_for`).

    Attributes:
        score: Numeric grade. Raw score for NUMBER assignments; the 0-4
            anchor for ALPHABET assignments.
        summary: One-paragraph assessment summary.
        feedback: Constructive feedback incl. spelling/grammar notes.
        plagiarism_score: Plagiarism percentage (0-100), when assessed.
        plagiarism_analysis: Plagiarism explanation, when assessed.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    score: float = Field(ge=0)
    summary: str
    feedback: str
    plagiarism_score: float | None = Field(
        default=None,
        ge=0,
        le=100,
        validation_alias=AliasChoices("plagiarismScore", "plagiarism_score"),
    )
    plagiarism_analysis: str | None = Field(
        default=None,
        validation_alias=AliasChoices("plagiarismAnalysis", "plagiarism_analysis"),
    )


class FreeTextGradeResult(BaseModel):
    """Structured output contract for one free-text answer.

    Attributes:
        score: Points awarded (clamped to ``max_points`` by the caller).
        summary: Short assessment summary.
        feedback: Constructive feedback.
    """

    model_config = ConfigDict(extra="ignore")

    score: float = Field(ge=0)
    summary: str
    feedback: str


def grading_response_schema() -> dict[str, Any]:
    """Emit the wire schema for full-submission grading.

    Returns:
        JSON schema derived from :class:`GradingResult` (nullability in the
        type-array/anyOf form providers accept).
    """
    return GradingResult.model_json_schema()


def free_text_response_schema() -> dict[str, Any]:
    """Emit the wire schema for free-text answer grading.

    Returns:
        JSON schema derived from :class:`FreeTextGradeResult`.
    """
    return FreeTextGradeResult.model_json_schema()


def parse_model_text[T: BaseModel](text: str, model_cls: type[T]) -> T:
    """Parse and validate raw model text against a contract model.

    Tolerates markdown fences; anything that is not schema-conforming JSON
    raises instead of being masked with defaults.

    Args:
        text: Raw model output.
        model_cls: The contract model to validate against.

    Returns:
        The validated result.

    Raises:
        ExternalServiceError: When the text is not valid JSON or violates
            the schema.
    """
    cleaned = _FENCE_PATTERN.sub("", text.strip())
    try:
        raw: Any = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ExternalServiceError("Malformed grading response.") from exc
    try:
        return model_cls.model_validate(raw)
    except ValidationError as exc:
        raise ExternalServiceError("Malformed grading response.") from exc


def parse_grading_text(text: str) -> GradingResult:
    """Parse raw model text into a :class:`GradingResult`.

    Args:
        text: Raw model output.

    Returns:
        The validated grading result.

    Raises:
        ExternalServiceError: When the text is not valid JSON or violates
            the schema.
    """
    return parse_model_text(text, GradingResult)


def parse_free_text_text(text: str) -> FreeTextGradeResult:
    """Parse raw model text into a :class:`FreeTextGradeResult`.

    Args:
        text: Raw model output.

    Returns:
        The validated free-text grading result.

    Raises:
        ExternalServiceError: When the text is not valid JSON or violates
            the schema.
    """
    return parse_model_text(text, FreeTextGradeResult)


def letter_for(score: float) -> str:
    """Map a 0-4 anchor score onto its letter grade.

    Args:
        score: Model-provided anchor (non-anchored values snap to the
            nearest anchor).

    Returns:
        Letter grade (``F`` for unrecognized anchors).
    """
    nearest = min(ANCHOR_LETTERS, key=lambda a: abs(a - score))
    return ANCHOR_LETTERS[nearest]


def build_grading_prompt(assignment: dict[str, object], ocr_texts: list[str]) -> str:
    """Build the full-submission grading prompt.

    Mirrors the legacy prompt contract so grading behavior stays consistent;
    the ALPHABET branch asks for the numeric anchor (the letter is derived
    server-side), keeping the prompt consistent with the numeric schema.

    Args:
        assignment: Context with subject/title/description/grading_prompt and
            the check flags/strictness/grading type.
        ocr_texts: OCR-extracted attachment texts.

    Returns:
        The complete prompt string.
    """
    grading_type = str(assignment.get("grading_type", "NUMBER"))
    scale_note = (
        f"Grade on the letter scale A={ALPHABET_SCALE['A']} … F={ALPHABET_SCALE['F']} "
        "and return the numeric anchor value in 'score'; the letter grade is "
        "derived from it."
        if grading_type == "ALPHABET"
        else "Return a numeric score."
    )
    checks = [
        f"penalize_spelling: {bool(assignment.get('check_spelling'))}",
        f"penalize_grammar: {bool(assignment.get('check_grammar'))}",
        f"check_plagiarism: {bool(assignment.get('check_plagiarism'))}",
    ]
    ocr_block = "\n".join(ocr_texts)
    return (
        "You are grading a student submission.\n"
        f"Subject: {assignment.get('subject')}\n"
        f"Title: {assignment.get('title')}\n"
        f"Description: {assignment.get('description')}\n"
        f"Teacher's grading instructions: {assignment.get('grading_prompt')}\n"
        f"Strictness: {assignment.get('strictness_level')}\n"
        f"Configuration: {'; '.join(checks)}\n"
        f"{scale_note}\n\n"
        "Assess correctness, completeness, and quality. Provide a summary, "
        "constructive feedback, and (when plagiarism checking is on) a "
        "plagiarism score 0-100 with analysis.\n"
        + (f"\n=== OCR EXTRACTED CONTENT ===\n{ocr_block}\n" if ocr_block else "")
    )


def build_free_text_prompt(question_text: str, student_answer: str, max_points: int) -> str:
    """Build the single free-text answer grading prompt.

    Args:
        question_text: The question body.
        student_answer: Student's answer.
        max_points: Maximum points for the question.

    Returns:
        The complete prompt string.
    """
    return (
        "You are a fair, constructive teacher's assistant.\n"
        f"Grade the student's answer out of {max_points} points.\n\n"
        f"Question: {question_text}\n\n"
        f"Student answer: {student_answer}\n\n"
        'Return JSON: {"score": number, "summary": str, "feedback": str}.'
    )
