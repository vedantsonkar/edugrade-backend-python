"""AI adapter layer: Gemini LLM grader (direct and batch).

Implements the provider-agnostic grading contract from
:mod:`app.services.ai.schema` and the :class:`BatchGradingClient` port from
:mod:`app.services.ai.batching`. ``google-genai`` is imported only inside
this module (adapter boundary, AGENTS.md §2-D).
"""

import base64
import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.core.config import Settings
from app.core.errors import ExternalServiceError
from app.services.ai.batching import (
    BatchGradeOutcome,
    BatchGradeRequest,
    BatchJobHandle,
    BatchJobSnapshot,
    BatchJobState,
)
from app.services.ai.schema import (
    GradingResult,
    build_free_text_prompt,
    build_grading_prompt,
    free_text_response_schema,
    grading_response_schema,
    parse_free_text_text,
    parse_grading_text,
)

logger = logging.getLogger(__name__)

_GEMINI_STATE_MAP: dict[str, BatchJobState] = {
    "JOB_STATE_QUEUED": BatchJobState.PENDING,
    "JOB_STATE_PENDING": BatchJobState.PENDING,
    "JOB_STATE_RUNNING": BatchJobState.RUNNING,
    "JOB_STATE_SUCCEEDED": BatchJobState.SUCCEEDED,
    "JOB_STATE_FAILED": BatchJobState.FAILED,
    "JOB_STATE_CANCELLED": BatchJobState.CANCELLED,
    "JOB_STATE_EXPIRED": BatchJobState.EXPIRED,
}
"""Gemini batch state names → normalized :class:`BatchJobState`."""


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
        self._temperature = settings.grading_temperature
        if not settings.gemini_api_key:
            raise ExternalServiceError("Gemini API key is not configured.")
        from google import genai  # local import: adapter boundary (AGENTS.md §2-D)

        self._client: Any = genai.Client(api_key=settings.gemini_api_key)

    async def grade_free_text(
        self, *, question_text: str, student_answer: str, max_points: int
    ) -> GradingResult:
        """Grade one free-text answer.

        Args:
            question_text: The question body.
            student_answer: Student's answer.
            max_points: Maximum points for the question.

        Returns:
            Parsed grading result (score clamped to ``max_points``).

        Raises:
            ExternalServiceError: When the model call or parsing fails.
        """
        prompt = build_free_text_prompt(question_text, student_answer, max_points)
        text = await self._generate_structured(
            prompt, [], response_schema=free_text_response_schema()
        )
        try:
            parsed = parse_free_text_text(text)
        except ExternalServiceError:
            raise
        score = min(max(parsed.score, 0.0), float(max_points))
        return GradingResult(
            score=score,
            summary=parsed.summary,
            feedback=parsed.feedback,
            plagiarism_score=None,
            plagiarism_analysis=None,
        )

    async def grade_submission(
        self,
        *,
        assignment: dict[str, object],
        attachments: list[dict[str, object]],
        ocr_texts: list[str],
    ) -> GradingResult:
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
            parts.append({"inline_data": {"mime_type": mime, "data": base64.b64decode(data)}})
        text = await self._generate_structured(
            prompt, parts, response_schema=grading_response_schema()
        )
        return parse_grading_text(text)

    async def _generate_structured(
        self,
        prompt: str,
        parts: list[dict[str, Any]],
        *,
        response_schema: dict[str, Any],
    ) -> str:
        """Run a schema-constrained generation and return the raw text.

        Args:
            prompt: Text part carrying the instructions.
            parts: Additional content parts (inline files); empty for
                text-only requests.
            response_schema: JSON schema derived from the Pydantic contract.

        Returns:
            Raw response text (parsed by the caller's contract model).

        Raises:
            ExternalServiceError: When the model call fails.
        """
        from google.genai import types

        contents: list[dict[str, Any]] = [{"parts": parts or [{"text": prompt}]}]
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=response_schema,
                    temperature=self._temperature,
                ),
            )
        except ExternalServiceError:
            raise
        except Exception as exc:
            raise ExternalServiceError("Gemini generation failed.") from exc
        return response.text or ""


@runtime_checkable
class LlmGrader(Protocol):
    """Port for AI grading operations.

    Implementations MUST raise :class:`ExternalServiceError` on provider
    failures and MUST NOT block the event loop.
    """

    async def grade_free_text(
        self, *, question_text: str, student_answer: str, max_points: int
    ) -> GradingResult:
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
    ) -> GradingResult:
        """Grade a full offline submission.

        Args:
            assignment: Assignment context dict.
            attachments: Inline base64 parts for non-OCR'd files.
            ocr_texts: OCR-extracted texts.

        Returns:
            Parsed grading result incl. plagiarism fields.
        """
        ...


def parse_batch_entry(entry: dict[str, Any]) -> BatchGradeOutcome:
    """Parse one Gemini batch output JSONL line into an outcome.

    Per-entry failures (error entries, missing candidates, malformed model
    text, schema violations) become failure outcomes instead of exceptions,
    so one bad response never fails the whole batch task.

    Args:
        entry: Parsed ``response`` JSONL line.

    Returns:
        Outcome with a validated :class:`GradingResult`, or an ``error``
        outcome explaining the per-entry failure.
    """
    key = str(entry.get("key", ""))
    error = entry.get("error")
    if error:
        return BatchGradeOutcome.failed(key, f"provider error: {error}")
    candidates = (entry.get("response") or {}).get("candidates") or []
    if not candidates:
        return BatchGradeOutcome.failed(key, "response has no candidates")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    texts = [str(p.get("text", "")) for p in parts if isinstance(p, dict)]
    text = "".join(texts).strip()
    if not text:
        return BatchGradeOutcome.failed(key, "response has no text")
    try:
        return BatchGradeOutcome(key=key, result=parse_grading_text(text))
    except ExternalServiceError as exc:
        return BatchGradeOutcome.failed(key, str(exc))


class BatchGeminiClient:
    """Gemini Batch API adapter implementing :class:`BatchGradingClient`."""

    def __init__(self, settings: Settings) -> None:
        """Create the client.

        Args:
            settings: Settings providing the API key, model name, and
                sampling knobs.

        Raises:
            ExternalServiceError: When no API key is configured.
        """
        self._model = settings.grading_model
        self._temperature = settings.grading_temperature
        if not settings.gemini_api_key:
            raise ExternalServiceError("Gemini API key is not configured.")
        from google import genai

        self._client: Any = genai.Client(api_key=settings.gemini_api_key)

    async def create_job(self, requests: Sequence[BatchGradeRequest]) -> BatchJobHandle:
        """Upload a JSONL payload and create the Gemini batch job.

        Each line carries the provider's ``generationConfig`` with the
        shared Pydantic schema and the configured sampling temperature, so
        batch responses come back schema-constrained and deterministic.

        Args:
            requests: One entry per submission to grade.

        Returns:
            Handle with the batch job name and uploaded input file name.

        Raises:
            ExternalServiceError: On upload/creation failure.
        """
        from app.services.ai.ocr import temporary_jsonl_path

        lines: list[str] = []
        for request in requests:
            parts: list[dict[str, Any]] = [{"text": request.prompt}]
            for blob in request.files:
                parts.append(
                    {
                        "inline_data": {
                            "mime_type": blob.mime_type,
                            "data": base64.b64encode(blob.data).decode(),
                        }
                    }
                )
            payload = {
                "key": request.key,
                "request": {
                    "contents": [{"parts": parts}],
                    "generationConfig": {
                        "responseMimeType": "application/json",
                        "responseSchema": grading_response_schema(),
                        "temperature": self._temperature,
                    },
                },
            }
            lines.append(json.dumps(payload))

        path = temporary_jsonl_path("batch-input")
        path.write_text("\n".join(lines), encoding="utf-8")
        try:
            file = await self._client.aio.files.upload(file=path)
            batch = await self._client.aio.batches.create(model=self._model, src=str(file.name))
        except Exception as exc:
            raise ExternalServiceError("Failed to create Gemini batch job.") from exc
        return BatchJobHandle(job_name=str(batch.name), input_ref=str(file.name))

    async def poll(self, job_name: str) -> BatchJobSnapshot:
        """Poll a batch job and normalize its state.

        Args:
            job_name: Name returned by :meth:`create_job`.

        Returns:
            Snapshot with the normalized state and output file reference
            when succeeded.

        Raises:
            ExternalServiceError: On polling failure.
        """
        try:
            batch = await self._client.aio.batches.get(name=job_name)
        except Exception as exc:
            raise ExternalServiceError("Failed to poll batch job.") from exc
        state = _normalize_state(batch)
        output = getattr(batch, "output_file_name", None) or getattr(
            getattr(batch, "dest", None), "output_file_name", None
        )
        return BatchJobSnapshot(
            state=state,
            output_ref=str(output) if output else None,
        )

    async def collect(self, job_name: str, snapshot: BatchJobSnapshot) -> list[BatchGradeOutcome]:
        """Download the output JSONL and parse it into per-key outcomes.

        Args:
            job_name: Name returned by :meth:`create_job`.
            snapshot: The succeeded snapshot carrying ``output_ref``.

        Returns:
            One outcome per output line (failures carried as ``error``).

        Raises:
            ExternalServiceError: When the output cannot be downloaded.
        """
        if not snapshot.output_ref:
            raise ExternalServiceError("Succeeded batch has no output file.")
        try:
            file = await self._client.aio.files.download(file=snapshot.output_ref)
            import asyncio

            content = await asyncio.to_thread(Path(str(file)).read_bytes)
        except ExternalServiceError:
            raise
        except Exception as exc:
            raise ExternalServiceError("Failed to download batch output.") from exc
        outcomes: list[BatchGradeOutcome] = []
        for line in content.decode("utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                outcomes.append(BatchGradeOutcome.failed("", "unparseable output line"))
                continue
            outcomes.append(parse_batch_entry(entry))
        return outcomes

    async def dispose(
        self, job_name: str, input_ref: str | None, snapshot: BatchJobSnapshot
    ) -> None:
        """Best-effort deletion of the input/output Gemini files.

        Args:
            job_name: Name returned by :meth:`create_job` (unused by this
                provider; kept for port symmetry).
            input_ref: Uploaded input file name, when known.
            snapshot: Terminal snapshot carrying the output file name.
        """
        _ = job_name
        if input_ref:
            await self._delete_file(input_ref)
        if snapshot.output_ref:
            await self._delete_file(snapshot.output_ref)

    async def _delete_file(self, file_name: str) -> None:
        """Delete one Gemini file, logging (not raising) on failure.

        Args:
            file_name: File name to delete.
        """
        try:
            await self._client.aio.files.delete(name=file_name)
        except Exception:
            logger.debug("Failed to delete Gemini file %s", file_name, exc_info=True)


def _normalize_state(batch: object) -> BatchJobState:
    """Map a Gemini batch object's state onto :class:`BatchJobState`.

    Args:
        batch: Batch object from the SDK (``batch.state.name`` when named).

    Returns:
        The normalized state; unknown states map to ``PENDING`` (never
        accidentally terminal).
    """
    state = getattr(batch, "state", None)
    name = getattr(state, "name", None) or str(state or "")
    return _GEMINI_STATE_MAP.get(name, BatchJobState.PENDING)
