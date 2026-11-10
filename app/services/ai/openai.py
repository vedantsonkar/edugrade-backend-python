"""AI adapter layer: OpenAI grader (direct and batch).

Implements the provider-agnostic grading contract from
:mod:`app.services.ai.schema` and the :class:`BatchGradingClient` port from
:mod:`app.services.ai.batching` on the OpenAI API (strict structured
outputs + the Batch API). ``openai`` is imported only inside this module
(adapter boundary, AGENTS.md §2-D).

Attachment note: images are inlined as ``image_url`` data URLs; non-image
attachments are relied on through their OCR text (the worker OCRs first and
only hands over blobs OCR could not read). Gemini inlines every blob; OpenAI
chat-completions batch does not accept arbitrary documents inline.
"""

import base64
import json
import logging
from collections.abc import Sequence
from typing import Any

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

_OPENAI_STATUS_MAP: dict[str, BatchJobState] = {
    "validating": BatchJobState.RUNNING,
    "in_progress": BatchJobState.RUNNING,
    "finalizing": BatchJobState.RUNNING,
    "completed": BatchJobState.SUCCEEDED,
    "failed": BatchJobState.FAILED,
    "expired": BatchJobState.EXPIRED,
    "cancelling": BatchJobState.CANCELLED,
    "cancelled": BatchJobState.CANCELLED,
}
"""OpenAI batch statuses → normalized :class:`BatchJobState`."""

_STRICT_UNSUPPORTED_KEYS = (
    "title",
    "default",
    "minimum",
    "maximum",
    "maxLength",
    "minLength",
    "examples",
    "$defs",
    "$schema",
)
"""JSON-Schema keys OpenAI strict mode rejects or ignores; stripped."""


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert a Pydantic-derived schema into OpenAI strict-mode form.

    Requirements: every property listed in ``required`` (optionality is
    expressed via nullable types), ``additionalProperties: false`` on every
    object, and no unsupported keywords. Nullability arrives from Pydantic
    as ``anyOf: [T, null]`` and is flattened to ``type: [T, "null"]``.

    Args:
        schema: JSON schema (e.g. :func:`grading_response_schema` output).

    Returns:
        Strict-mode schema.
    """
    result = _strictify(schema)
    assert isinstance(result, dict)
    return result


def _is_null_branch(branch: dict[str, object]) -> bool:
    """Whether a schema branch is the bare ``null`` type of an ``anyOf``.

    Args:
        branch: Strictified branch node.

    Returns:
        ``True`` when the branch contributes only nullability.
    """
    return branch.get("type") == "null" and len(branch) == 1


def _strictify(node: object) -> object:
    """Recursively apply strict-mode rules to one schema node.

    Args:
        node: Schema node (dict, list, or scalar).

    Returns:
        Transformed node.
    """
    if isinstance(node, list):
        return [_strictify(item) for item in node]
    if not isinstance(node, dict):
        return node

    raw: dict[object, object] = dict(node)
    result: dict[str, object] = {
        str(key): value for key, value in raw.items() if key not in _STRICT_UNSUPPORTED_KEYS
    }
    branches = result.pop("anyOf", None)
    if isinstance(branches, list):
        strict_branches: list[dict[str, object]] = []
        for item in branches:
            converted = _strictify(item)
            if isinstance(converted, dict):
                strict_branches.append(converted)
        has_null = any(_is_null_branch(b) for b in strict_branches)
        non_null = [b for b in strict_branches if not _is_null_branch(b)]
        if has_null and len(non_null) == 1 and "type" in non_null[0]:
            base_type = non_null[0]["type"]
            if isinstance(base_type, str):
                non_null[0]["type"] = [base_type, "null"]
            elif isinstance(base_type, list):
                non_null[0]["type"] = [*base_type, "null"]
            result.update(non_null[0])
        elif non_null:
            result["anyOf"] = non_null

    if result.get("type") == "object" or "properties" in result:
        properties = result.get("properties", {})
        if isinstance(properties, dict):
            strict_props: dict[str, object] = {
                str(name): _strictify(prop) for name, prop in properties.items()
            }
            result["properties"] = strict_props
            result["required"] = list(strict_props)
            result["additionalProperties"] = False
    if result.get("type") == "array" and "items" in result:
        result["items"] = _strictify(result["items"])
    return result


def parse_output_line(line: dict[str, Any]) -> BatchGradeOutcome:
    """Parse one OpenAI batch output JSONL line into an outcome.

    Per-entry failures (error rows, non-200 statuses, empty content,
    schema violations) become failure outcomes instead of exceptions, so
    one bad response never fails the whole batch task.

    Args:
        line: Parsed output JSONL line.

    Returns:
        Outcome with a validated :class:`GradingResult`, or an ``error``
        outcome explaining the per-entry failure.
    """
    key = str(line.get("custom_id", ""))
    error = line.get("error")
    if error:
        return BatchGradeOutcome.failed(key, f"provider error: {error}")
    response = line.get("response") or {}
    status = response.get("status_code")
    if status != 200:
        return BatchGradeOutcome.failed(key, f"status_code: {status}")
    choices = (response.get("body") or {}).get("choices") or []
    if not choices:
        return BatchGradeOutcome.failed(key, "response has no choices")
    message = choices[0].get("message") or {}
    content = str(message.get("content") or "").strip()
    if not content:
        return BatchGradeOutcome.failed(key, "response has no content")
    try:
        return BatchGradeOutcome(key=key, result=parse_grading_text(content))
    except ExternalServiceError as exc:
        return BatchGradeOutcome.failed(key, str(exc))


class OpenAILlmGrader:
    """Direct OpenAI adapter for synchronous grading operations."""

    def __init__(self, settings: Settings) -> None:
        """Create the OpenAI client.

        Args:
            settings: Settings providing the API key, model name, and
                sampling knobs.

        Raises:
            ExternalServiceError: When no API key is configured (callers
                should inject ``None`` instead of building the adapter).
        """
        self._model = settings.openai_model
        self._temperature = settings.grading_temperature
        self._seed = settings.grading_seed
        if not settings.openai_api_key:
            raise ExternalServiceError("OpenAI API key is not configured.")
        from openai import AsyncOpenAI  # local import: adapter boundary

        self._client: Any = AsyncOpenAI(api_key=settings.openai_api_key)

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
        text = await self._complete_text(prompt, response_schema=free_text_response_schema())
        parsed = parse_free_text_text(text)
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
                "mime_type": str}``; images are inlined, other types are
                skipped (covered by OCR text).
            ocr_texts: OCR-extracted texts for OCR-capable attachments.

        Returns:
            Parsed grading result incl. plagiarism fields.

        Raises:
            ExternalServiceError: When the model call or parsing fails.
        """
        prompt = build_grading_prompt(assignment, ocr_texts)
        parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for attachment in attachments:
            data = str(attachment.get("data", ""))
            mime = str(attachment.get("mime_type", "application/octet-stream"))
            if "," in data:
                data = data.split(",", 1)[1]
            if not mime.startswith("image/"):
                logger.warning(
                    "Skipping non-image attachment (mime=%s); relying on OCR text.", mime
                )
                continue
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{data}"},
                }
            )
        text = await self._complete_parts(parts, response_schema=grading_response_schema())
        return parse_grading_text(text)

    async def _complete_text(self, prompt: str, *, response_schema: dict[str, Any]) -> str:
        """Run one text-only completion and return the raw content.

        Args:
            prompt: User message text.
            response_schema: JSON schema derived from the Pydantic contract.

        Returns:
            Raw response content.

        Raises:
            ExternalServiceError: When the model call fails.
        """
        return await self._complete_parts(
            [{"type": "text", "text": prompt}], response_schema=response_schema
        )

    async def _complete_parts(
        self, parts: list[dict[str, Any]], *, response_schema: dict[str, Any]
    ) -> str:
        """Run one strict structured-output completion.

        Args:
            parts: Multimodal content parts.
            response_schema: JSON schema derived from the Pydantic contract.

        Returns:
            Raw response content.

        Raises:
            ExternalServiceError: When the model call fails or returns no
                content.
        """
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": parts}],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "grading_result",
                        "strict": True,
                        "schema": strict_schema(response_schema),
                    },
                },
                temperature=self._temperature,
                **({"seed": self._seed} if self._seed is not None else {}),
            )
        except ExternalServiceError:
            raise
        except Exception as exc:
            raise ExternalServiceError("OpenAI generation failed.") from exc
        raw_content = response.choices[0].message.content if response.choices else None
        content = str(raw_content) if raw_content is not None else ""
        if not content:
            raise ExternalServiceError("OpenAI returned no content.")
        return content


class OpenAIBatchGradingClient:
    """OpenAI Batch API adapter implementing :class:`BatchGradingClient`."""

    def __init__(self, settings: Settings) -> None:
        """Create the client.

        Args:
            settings: Settings providing the API key, model name, and
                sampling knobs.

        Raises:
            ExternalServiceError: When no API key is configured.
        """
        self._model = settings.openai_model
        self._temperature = settings.grading_temperature
        self._seed = settings.grading_seed
        if not settings.openai_api_key:
            raise ExternalServiceError("OpenAI API key is not configured.")
        from openai import AsyncOpenAI

        self._client: Any = AsyncOpenAI(api_key=settings.openai_api_key)

    async def create_job(self, requests: Sequence[BatchGradeRequest]) -> BatchJobHandle:
        """Upload a JSONL payload and create the OpenAI batch job.

        Each line targets ``/v1/chat/completions`` with strict structured
        output and the configured sampling knobs, so batch responses come
        back schema-guaranteed and deterministic.

        Args:
            requests: One entry per submission to grade.

        Returns:
            Handle with the batch job id and uploaded input file id.

        Raises:
            ExternalServiceError: On upload/creation failure.
        """
        from app.services.ai.ocr import temporary_jsonl_path

        schema = {
            "name": "grading_result",
            "strict": True,
            "schema": strict_schema(grading_response_schema()),
        }
        lines: list[str] = []
        for request in requests:
            parts: list[dict[str, Any]] = [{"type": "text", "text": request.prompt}]
            for blob in request.files:
                if not blob.mime_type.startswith("image/"):
                    logger.warning(
                        "Batch request %s: skipping non-image attachment (mime=%s).",
                        request.key,
                        blob.mime_type,
                    )
                    continue
                encoded = base64.b64encode(blob.data).decode()
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{blob.mime_type};base64,{encoded}"},
                    }
                )
            body: dict[str, Any] = {
                "model": self._model,
                "messages": [{"role": "user", "content": parts}],
                "response_format": {"type": "json_schema", "json_schema": schema},
                "temperature": self._temperature,
            }
            if self._seed is not None:
                body["seed"] = self._seed
            payload = {
                "custom_id": request.key,
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": body,
            }
            lines.append(json.dumps(payload))

        path = temporary_jsonl_path("openai-batch-input")
        path.write_text("\n".join(lines), encoding="utf-8")
        try:
            file = await self._client.files.create(
                file=(path.name, path.read_bytes()), purpose="batch"
            )
            batch = await self._client.batches.create(
                input_file_id=file.id,
                endpoint="/v1/chat/completions",
                completion_window="24h",
            )
        except Exception as exc:
            raise ExternalServiceError("Failed to create OpenAI batch job.") from exc
        return BatchJobHandle(job_name=str(batch.id), input_ref=str(file.id))

    async def poll(self, job_name: str) -> BatchJobSnapshot:
        """Poll a batch job and normalize its state.

        Args:
            job_name: Id returned by :meth:`create_job`.

        Returns:
            Snapshot with the normalized state and output file id when
            succeeded.

        Raises:
            ExternalServiceError: On polling failure.
        """
        try:
            batch = await self._client.batches.retrieve(job_name)
        except Exception as exc:
            raise ExternalServiceError("Failed to poll batch job.") from exc
        state = _OPENAI_STATUS_MAP.get(str(batch.status), BatchJobState.RUNNING)
        error = str(batch.error) if getattr(batch, "error", None) else None
        return BatchJobSnapshot(
            state=state,
            output_ref=str(batch.output_file_id) if batch.output_file_id else None,
            error=error,
        )

    async def collect(self, job_name: str, snapshot: BatchJobSnapshot) -> list[BatchGradeOutcome]:
        """Download the output JSONL and parse it into per-key outcomes.

        Args:
            job_name: Id returned by :meth:`create_job` (unused; port
                symmetry).
            snapshot: The succeeded snapshot carrying ``output_ref``.

        Returns:
            One outcome per output line (failures carried as ``error``).

        Raises:
            ExternalServiceError: When the output cannot be downloaded.
        """
        _ = job_name
        if not snapshot.output_ref:
            raise ExternalServiceError("Succeeded batch has no output file.")
        try:
            content_obj = await self._client.files.content(snapshot.output_ref)
            content: str = content_obj.text
        except ExternalServiceError:
            raise
        except Exception as exc:
            raise ExternalServiceError("Failed to download batch output.") from exc
        outcomes: list[BatchGradeOutcome] = []
        for line in content.splitlines():
            if not line.strip():
                continue
            try:
                entry: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                outcomes.append(BatchGradeOutcome.failed("", "unparseable output line"))
                continue
            outcomes.append(parse_output_line(entry))
        return outcomes

    async def dispose(
        self, job_name: str, input_ref: str | None, snapshot: BatchJobSnapshot
    ) -> None:
        """Best-effort deletion of the input/output OpenAI files.

        Args:
            job_name: Id returned by :meth:`create_job` (unused; port
                symmetry).
            input_ref: Uploaded input file id, when known.
            snapshot: Terminal snapshot carrying the output file id.
        """
        _ = job_name
        if input_ref:
            await self._delete_file(input_ref)
        if snapshot.output_ref:
            await self._delete_file(snapshot.output_ref)

    async def _delete_file(self, file_id: str) -> None:
        """Delete one OpenAI file, logging (not raising) on failure.

        Args:
            file_id: File id to delete.
        """
        try:
            await self._client.files.delete(file_id)
        except Exception:
            logger.debug("Failed to delete OpenAI file %s", file_id, exc_info=True)
