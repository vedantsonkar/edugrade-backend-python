"""AI adapter layer: Gemini LLM grader (direct and batch)."""

import base64
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.core.config import Settings
from app.core.errors import ExternalServiceError

logger = logging.getLogger(__name__)

ALPHABET_SCALE = {"A": 4.0, "B": 3.0, "C": 2.0, "D": 1.0, "E": 0.5, "F": 0.0}
"""Letter-grade anchor scale embedded in prompts (ALPHABET grading type)."""


@dataclass(frozen=True)
class LlmGradeResult:
    """Structured result of grading one submission with Gemini.

    Attributes:
        score: Numeric score (already normalized to NUMBER scale; letters are
            converted by the prompt contract).
        summary: One-paragraph assessment summary.
        feedback: Constructive feedback incl. spelling/grammar notes.
        plagiarism_score: Plagiarism percentage (0-100), when assessed.
        plagiarism_analysis: Plagiarism explanation, when assessed.
    """

    score: float
    summary: str
    feedback: str
    plagiarism_score: float | None
    plagiarism_analysis: str | None


class GeminiLlmGrader:
    """Direct Gemini adapter for synchronous grading operations."""

    def __init__(self, settings: Settings) -> None:
        """Create the Gemini client.

        Args:
            settings: Settings providing the API key and model name.

        Raises:
            ExternalServiceError: When no API key is configured (callers
                should inject ``None`` instead of building the adapter).
        """
        self._model = settings.grading_model
        if not settings.gemini_api_key:
            raise ExternalServiceError("Gemini API key is not configured.")
        from google import genai  # local import: adapter boundary (AGENTS.md §2-D)

        self._client: Any = genai.Client(api_key=settings.gemini_api_key)

    async def grade_free_text(
        self, *, question_text: str, student_answer: str, max_points: int
    ) -> LlmGradeResult:
        """Grade one free-text answer.

        Args:
            question_text: The question body.
            student_answer: Student's answer.
            max_points: Maximum points for the question.

        Returns:
            Parsed grading result.

        Raises:
            ExternalServiceError: When the model call or parsing fails.
        """
        prompt = (
            "You are a fair, constructive teacher's assistant.\n"
            f"Grade the student's answer out of {max_points} points.\n\n"
            f"Question: {question_text}\n\n"
            f"Student answer: {student_answer}\n\n"
            'Return JSON: {"score": number, "summary": str, "feedback": str}.'
        )
        raw = await self._generate_json(prompt)
        try:
            score = min(max(float(raw["score"]), 0.0), float(max_points))
            return LlmGradeResult(
                score=score,
                summary=str(raw.get("summary", "")),
                feedback=str(raw.get("feedback", "")),
                plagiarism_score=None,
                plagiarism_analysis=None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ExternalServiceError("Malformed grading response.") from exc

    async def grade_submission(
        self,
        *,
        assignment: dict[str, object],
        attachments: list[dict[str, object]],
        ocr_texts: list[str],
    ) -> LlmGradeResult:
        """Grade a full offline submission (files + OCR context).

        Args:
            assignment: Assignment context dict (subject/title/prompt/config).
            attachments: Non-OCR'd attachments as ``{"data": base64,
                "mime_type": str}`` for inline understanding.
            ocr_texts: OCR-extracted texts for OCR-capable attachments.

        Returns:
            Parsed grading result incl. plagiarism fields.

        Raises:
            ExternalServiceError: When the model call or parsing fails.
        """
        prompt = build_grading_prompt(assignment, ocr_texts)
        parts: list[dict[str, Any]] = [{"text": prompt}]
        for attachment in attachments:
            data = str(attachment.get("data", ""))
            mime = str(attachment.get("mime_type", "application/octet-stream"))
            if "," in data:
                data = data.split(",", 1)[1]
            parts.append(
                {"inline_data": {"mime_type": mime, "data": base64.b64decode(data)}}
            )
        raw = await self._generate_json_structured(
            prompt, parts, response_schema=GRADING_RESPONSE_SCHEMA
        )
        return _parse_grading_result(raw)

    async def _generate_json(self, prompt: str) -> dict[str, Any]:
        """Run a plain JSON-mode generation.

        Args:
            prompt: Fully built prompt.

        Returns:
            Parsed JSON mapping.
        """
        from google.genai import types

        response = await self._client.aio.models.generate_content(
            model=self._model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
            ),
        )
        return _loads_json(response.text or "{}")

    async def _generate_json_structured(
        self,
        prompt: str,
        parts: list[dict[str, Any]],
        *,
        response_schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Run a schema-constrained multi-part generation.

        Args:
            prompt: Text part carrying the instructions.
            parts: Additional content parts (inline files).
            response_schema: Plain-JSON schema for structured output.

        Returns:
            Parsed JSON mapping.
        """
        from google.genai import types

        contents: list[dict[str, Any]] = [{"parts": parts}]
        response = await self._client.aio.models.generate_content(
            model=self._model,
            contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=response_schema,
            ),
        )
        return _loads_json(response.text or "{}")


@runtime_checkable
class LlmGrader(Protocol):
    """Port for AI grading operations.

    Implementations MUST raise :class:`ExternalServiceError` on provider
    failures and MUST NOT block the event loop.
    """

    async def grade_free_text(
        self, *, question_text: str, student_answer: str, max_points: int
    ) -> LlmGradeResult:
        """Grade one free-text answer.

        Args:
            question_text: The question body.
            student_answer: Student's answer.
            max_points: Maximum points for the question.

        Returns:
            Parsed grading result.
        """
        ...

    async def grade_submission(
        self,
        *,
        assignment: dict[str, object],
        attachments: list[dict[str, object]],
        ocr_texts: list[str],
    ) -> LlmGradeResult:
        """Grade a full offline submission.

        Args:
            assignment: Assignment context dict.
            attachments: Inline base64 parts for non-OCR'd files.
            ocr_texts: OCR-extracted texts.

        Returns:
            Parsed grading result incl. plagiarism fields.
        """
        ...


GRADING_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "score": {"type": "number"},
        "summary": {"type": "string"},
        "feedback": {"type": "string"},
        "plagiarismScore": {"type": "number", "nullable": True},
        "plagiarismAnalysis": {"type": "string", "nullable": True},
    },
    "required": ["score", "summary", "feedback"],
}
"""Structured-output schema for full submission grading."""


def build_grading_prompt(
    assignment: dict[str, object], ocr_texts: list[str]
) -> str:
    """Build the full-submission grading prompt.

    Mirrors the legacy prompt contract so grading behavior stays consistent.

    Args:
        assignment: Context with subject/title/description/gradingPrompt and
            the check flags/strictness/grading type.
        ocr_texts: OCR-extracted attachment texts.

    Returns:
        The complete prompt string.
    """
    grading_type = str(assignment.get("grading_type", "NUMBER"))
    scale_note = (
        f"Use the letter scale A={ALPHABET_SCALE['A']} … F={ALPHABET_SCALE['F']} "
        "and return the letter in 'score'."
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


def _parse_grading_result(raw: dict[str, Any]) -> LlmGradeResult:
    """Normalize the model's JSON into an :class:`LlmGradeResult`.

    Letter scores are converted to their numeric anchors.

    Args:
        raw: Parsed model output.

    Returns:
        Typed grading result.

    Raises:
        ExternalServiceError: When required fields are missing/invalid.
    """
    try:
        raw_score = raw["score"]
        score: float
        if isinstance(raw_score, str) and raw_score.strip().upper() in ALPHABET_SCALE:
            score = ALPHABET_SCALE[raw_score.strip().upper()]
        else:
            score = float(raw_score)
        plagiarism = raw.get("plagiarismScore")
        return LlmGradeResult(
            score=score,
            summary=str(raw.get("summary", "")),
            feedback=str(raw.get("feedback", "")),
            plagiarism_score=float(plagiarism) if plagiarism is not None else None,
            plagiarism_analysis=(
                str(raw["plagiarismAnalysis"])
                if raw.get("plagiarismAnalysis") is not None
                else None
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ExternalServiceError("Malformed grading response.") from exc


def _loads_json(text: str) -> dict[str, Any]:
    """Parse model output tolerating markdown fences.

    Args:
        text: Raw model text.

    Returns:
        Parsed mapping.

    Raises:
        ExternalServiceError: When parsing fails.
    """
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    try:
        parsed: Any = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ExternalServiceError("Malformed model output.") from exc
    if not isinstance(parsed, dict):
        raise ExternalServiceError("Malformed model output.")
    return parsed


class BatchGeminiClient:
    """Gemini Batch API operations for the background grading worker."""

    def __init__(self, settings: Settings) -> None:
        """Create the client.

        Args:
            settings: Settings providing the API key and model name.

        Raises:
            ExternalServiceError: When no API key is configured.
        """
        self._model = settings.grading_model
        if not settings.gemini_api_key:
            raise ExternalServiceError("Gemini API key is not configured.")
        from google import genai

        self._client: Any = genai.Client(api_key=settings.gemini_api_key)

    async def upload_jsonl(self, path: Path) -> str:
        """Upload a JSONL batch input file.

        Args:
            path: Local JSONL file.

        Returns:
            Gemini file name for batch creation.

        Raises:
            ExternalServiceError: On upload failure.
        """
        try:
            file = await self._client.aio.files.upload(file=path)
            return str(file.name)
        except Exception as exc:
            raise ExternalServiceError("Failed to upload batch input.") from exc

    async def create_batch(self, input_file_name: str) -> str:
        """Create a batch job over an uploaded JSONL file.

        Args:
            input_file_name: File name returned by :meth:`upload_jsonl`.

        Returns:
            Batch job name.

        Raises:
            ExternalServiceError: On creation failure.
        """
        try:
            batch = await self._client.aio.batches.create(
                model=self._model, src=input_file_name
            )
            return str(batch.name)
        except Exception as exc:
            raise ExternalServiceError("Failed to create batch job.") from exc

    async def get_batch_state(self, job_name: str) -> tuple[str, str | None]:
        """Poll a batch job's state.

        Args:
            job_name: Batch job name.

        Returns:
            Tuple of (job state, output file name when succeeded).

        Raises:
            ExternalServiceError: On polling failure.
        """
        try:
            batch = await self._client.aio.batches.get(name=job_name)
            output = getattr(batch, "output_file_name", None) or getattr(
                getattr(batch, "dest", None), "output_file_name", None
            )
            return str(batch.state.name if hasattr(batch.state, "name") else batch.state), (
                str(output) if output else None
            )
        except Exception as exc:
            raise ExternalServiceError("Failed to poll batch job.") from exc

    async def download_output(self, file_name: str) -> bytes:
        """Download a batch output file's bytes.

        Args:
            file_name: Output file name from :meth:`get_batch_state`.

        Returns:
            File content (JSONL).
        """
        try:
            file = await self._client.aio.files.download(file=file_name)
            import asyncio

            return await asyncio.to_thread(Path(str(file)).read_bytes)
        except Exception as exc:
            raise ExternalServiceError("Failed to download batch output.") from exc

    async def delete_file(self, file_name: str) -> None:
        """Best-effort deletion of an uploaded Gemini file.

        Args:
            file_name: File name to delete.
        """
        try:
            await self._client.aio.files.delete(name=file_name)
        except Exception:
            logger.debug("Failed to delete Gemini file %s", file_name, exc_info=True)
