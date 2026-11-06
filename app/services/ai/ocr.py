"""OCR provider abstraction (Strategy + Registry).

:class:`OcrProvider` implementations extract text from attachments.
``glm`` uses the Z.ai layout-parsing endpoint over async httpx; ``off`` is a
no-op used when OCR is disabled. Adding ``vision`` later = one new class +
registry entry (AGENTS.md §2-O).
"""

import asyncio
import base64
import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)

GLM_TIMEOUT_SECONDS = 45
GLM_RETRIES = 3


@dataclass(frozen=True)
class OcrResult:
    """Outcome of an OCR pass over one file.

    Attributes:
        text: Extracted text (empty when OCR unavailable).
        confidence: Provider confidence 0-1 (``None`` when unsupported).
    """

    text: str
    confidence: float | None


@runtime_checkable
class OcrProvider(Protocol):
    """Port for text extraction from a stored file."""

    async def extract_text(self, data: bytes, mime_type: str) -> OcrResult:
        """Extract text from file bytes.

        Args:
            data: File content.
            mime_type: File MIME type.

        Returns:
            Extraction result.
        """
        ...


class GlmOcrProvider:
    """OCR via the Z.ai (GLM) layout-parsing API with bounded retries."""

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient) -> None:
        """Configure the provider.

        Args:
            settings: Settings carrying the API key and endpoint.
            http_client: Async HTTP client.
        """
        self._api_key = settings.glm_ocr_api_key
        self._endpoint = settings.glm_ocr_endpoint
        self._http = http_client

    async def extract_text(self, data: bytes, mime_type: str) -> OcrResult:
        """Extract text via GLM layout parsing.

        Retries on 429/5xx up to :data:`GLM_RETRIES`; other statuses and
        network errors return an empty result (grading proceeds without OCR).

        Args:
            data: File content.
            mime_type: File MIME type.

        Returns:
            Extraction result (possibly empty text).
        """
        if not self._api_key:
            logger.warning("GLM OCR requested without an API key; skipping.")
            return OcrResult(text="", confidence=None)
        payload = {
            "model": "glm-ocr",
            "file": f"data:{mime_type};base64,{base64.b64encode(data).decode()}",
        }
        for attempt in range(1, GLM_RETRIES + 1):
            try:
                response = await self._http.post(
                    self._endpoint,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=GLM_TIMEOUT_SECONDS,
                )
                if response.status_code in (429, 500, 502, 503, 504):
                    await asyncio.sleep(min(2**attempt, 8))
                    continue
                response.raise_for_status()
                body: dict[str, object] = response.json()
                text = str(body.get("text") or body.get("content") or "")
                confidence_raw = body.get("confidence")
                confidence = (
                    float(str(confidence_raw))
                    if confidence_raw is not None
                    else None
                )
                return OcrResult(text=text, confidence=confidence)
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("GLM OCR attempt %d failed: %s", attempt, exc)
                await asyncio.sleep(min(2**attempt, 8))
        return OcrResult(text="", confidence=None)


class DisabledOcrProvider:
    """No-op provider used when OCR is switched off."""

    async def extract_text(self, data: bytes, mime_type: str) -> OcrResult:
        """Return an empty extraction.

        Args:
            data: Unused file content.
            mime_type: Unused MIME type.

        Returns:
            Empty result.
        """
        _ = data, mime_type
        return OcrResult(text="", confidence=None)


def guess_mime(filename: str, fallback: str = "application/octet-stream") -> str:
    """Guess a MIME type from a filename.

    Args:
        filename: Client filename.
        fallback: Value when the type cannot be guessed.

    Returns:
        Best-effort MIME type.
    """
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or fallback


def build_ocr_registry(
    settings: Settings, http_client: httpx.AsyncClient
) -> dict[str, OcrProvider]:
    """Assemble the provider registry from settings.

    Args:
        settings: Application settings.
        http_client: Shared async HTTP client.

    Returns:
        Mapping of provider name → implementation (``glm``, ``off``).
    """
    return {
        "glm": GlmOcrProvider(settings, http_client),
        "off": DisabledOcrProvider(),
    }


def temporary_jsonl_path(task_id: str) -> Path:
    """Build the scratch path for a batch task's JSONL input.

    Args:
        task_id: Batch task id.

    Returns:
        Path under ``scratch/``.
    """
    scratch = Path("scratch")
    scratch.mkdir(exist_ok=True)
    return scratch / f"batch_requests_{task_id}.jsonl"
