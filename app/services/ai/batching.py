"""Batch grading port and transport types (provider-agnostic).

:class:`BatchGradingClient` is the port the background worker depends on
(AGENTS.md §2-D); provider adapters (:mod:`app.services.ai.llm`,
:mod:`app.services.ai.openai`) implement it. This module intentionally
imports no adapter: new providers slot in behind the port without touching
consumers (AGENTS.md §2-O).

Error semantics (the production-reliability contract):

- Transport failures (upload/poll/download problems) raise
  :class:`ExternalServiceError` and fail the whole batch task.
- Per-request failures (malformed model output, per-entry API errors) are
  returned as :class:`BatchGradeOutcome` rows with ``error`` set - one bad
  response never fails the task.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from app.services.ai.schema import GradingResult

if TYPE_CHECKING:
    from app.services.ai.llm import LlmGrader


class BatchJobState(StrEnum):
    """Provider-agnostic batch job lifecycle states."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


TERMINAL_FAILURE_STATES: frozenset[BatchJobState] = frozenset(
    {BatchJobState.FAILED, BatchJobState.CANCELLED, BatchJobState.EXPIRED}
)
"""States from which a batch task can only be marked failed."""


@dataclass(frozen=True)
class AttachmentBlob:
    """An attachment's raw bytes plus MIME type, handed to an adapter.

    Attributes:
        data: File content.
        mime_type: MIME type (adapter decides inline representation).
    """

    data: bytes
    mime_type: str


@dataclass(frozen=True)
class BatchGradeRequest:
    """One submission's grading request in the batch job.

    Attributes:
        key: Stable request key the worker maps back to a submission id.
        prompt: Fully built grading prompt.
        files: Attachments the adapter may inline (may be empty).
    """

    key: str
    prompt: str
    files: tuple[AttachmentBlob, ...] = ()


@dataclass(frozen=True)
class BatchJobHandle:
    """Identity of a created batch job.

    Attributes:
        job_name: Provider job identifier to poll later.
        input_ref: Opaque provider reference to the uploaded input payload
            (needed for cleanup; ``None`` when the provider manages inputs
            itself).
    """

    job_name: str
    input_ref: str | None = None


@dataclass(frozen=True)
class BatchJobSnapshot:
    """A poll result for one batch job.

    Attributes:
        state: Normalized job state.
        output_ref: Opaque provider reference to the output payload once
            succeeded (``None`` before that).
        error: Provider-side explanation when the job failed.
    """

    state: BatchJobState
    output_ref: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class BatchGradeOutcome:
    """Per-request result of a finished batch job.

    Exactly one of ``result`` / ``error`` is set: ``result`` on success,
    ``error`` on per-request failure.

    Attributes:
        key: The originating :attr:`BatchGradeRequest.key`.
        result: Validated grading result on success.
        error: Failure explanation on per-request failure.
    """

    key: str
    result: GradingResult | None = None
    error: str | None = None

    @classmethod
    def failed(cls, key: str, error: str) -> BatchGradeOutcome:
        """Build a failure outcome.

        Args:
            key: The originating request key.
            error: Failure explanation.

        Returns:
            Outcome with ``error`` set.
        """
        return cls(key=key, error=error)


@runtime_checkable
class BatchGradingClient(Protocol):
    """Port for asynchronous batch grading operations.

    Implementations MUST raise :class:`ExternalServiceError` on transport
    failures, MUST return per-request failures as outcome rows (never raise
    for them), and MUST NOT block the event loop.
    """

    async def create_job(self, requests: Sequence[BatchGradeRequest]) -> BatchJobHandle:
        """Upload requests and create the provider batch job.

        Args:
            requests: One entry per submission to grade.

        Returns:
            Handle carrying the job name and input reference.

        Raises:
            ExternalServiceError: On upload/creation failure.
        """
        ...

    async def poll(self, job_name: str) -> BatchJobSnapshot:
        """Poll a batch job's state.

        Args:
            job_name: Name returned by :meth:`create_job`.

        Returns:
            Normalized snapshot (state + output reference when succeeded).

        Raises:
            ExternalServiceError: On polling failure.
        """
        ...

    async def collect(self, job_name: str, snapshot: BatchJobSnapshot) -> list[BatchGradeOutcome]:
        """Download and parse a succeeded job's results.

        Args:
            job_name: Name returned by :meth:`create_job`.
            snapshot: The succeeded snapshot carrying ``output_ref``.

        Returns:
            One outcome per response line; per-request failures carry
            ``error``. Response lines for unknown keys are dropped.

        Raises:
            ExternalServiceError: When the output payload itself cannot be
                downloaded or is not parseable at the transport level.
        """
        ...

    async def dispose(
        self, job_name: str, input_ref: str | None, snapshot: BatchJobSnapshot
    ) -> None:
        """Best-effort cleanup of provider-side artifacts.

        Args:
            job_name: Name returned by :meth:`create_job`.
            input_ref: The input payload reference recorded at start time
                (``None`` when unknown, e.g. after a restart).
            snapshot: The terminal snapshot carrying ``output_ref``.
        """
        ...


@dataclass(frozen=True)
class GradingProviderBundle:
    """The adapter pair a named grading provider contributes.

    Attributes:
        llm: Direct (synchronous) grader; ``None`` when the provider does
            not serve the direct path.
        batch: Batch client; ``None`` when the provider does not serve
            batch grading.
    """

    llm: LlmGrader | None = None
    batch: BatchGradingClient | None = None


def resolve_grading_provider(
    registry: dict[str, GradingProviderBundle], provider_name: str
) -> GradingProviderBundle | None:
    """Resolve the configured provider from the registry.

    Mirrors OCR resolution: settings-keyed lookup with a first-registered
    fallback, ``None`` when the registry is empty.

    Args:
        registry: Provider name → bundle mapping built at composition time.
        provider_name: Configured provider key (``settings.grading_provider``).

    Returns:
        The resolved bundle, or ``None`` when no provider is configured.
    """
    if not registry:
        return None
    bundle = registry.get(provider_name)
    if bundle is not None:
        return bundle
    return next(iter(registry.values()))
