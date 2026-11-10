"""Background batch-grading worker.

Implements the legacy prototype's state machine with its defects fixed:
request mappings are real (submission id → request key), sessions are opened
per pass (workers own no long-lived DB session), and results are applied
through the shared applier with per-submission isolation. Flow:

1. ``enqueue_due_date_assignments`` - due offline assignments with auto-grade
   become QUEUED tasks (deduped by ``auto_graded_at``).
2. ``start_queued_grading_tasks`` - QUEUED → RUNNING: build batch requests,
   hand them to the configured :class:`BatchGradingClient` provider.
3. ``poll_running_grading_tasks`` - RUNNING tasks poll the provider; on
   success per-request outcomes are applied to submissions and tasks
   finalize (COMPLETED/PARTIAL/FAILED).

The worker depends only on the provider-agnostic port: per-request failures
(malformed model output, provider errors) are isolated as failed
submissions and never abort the task.
"""

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.core.db.engine import session_scope
from app.core.ids import IdProvider
from app.core.pagination import PageParams
from app.core.storage.base import ObjectStorage
from app.domain.enums import BatchGradingMode, BatchGradingTaskStatus, BatchGradingTrigger
from app.domain.grading import BatchGradingTask, Submission
from app.repositories.assignments import AssignmentRepository
from app.repositories.grading import BatchGradingTaskRepository, SubmissionRepository
from app.services.ai.batching import (
    TERMINAL_FAILURE_STATES,
    AttachmentBlob,
    BatchGradeRequest,
    BatchGradingClient,
    BatchJobState,
)
from app.services.ai.ocr import OcrProvider, guess_mime
from app.services.ai.schema import build_grading_prompt
from app.services.grading.apply import apply_grading_result

logger = logging.getLogger(__name__)


class BatchGradingProcessor:
    """Orchestrates the batch grading state machine on CRON triggers."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        storage: ObjectStorage,
        ocr: OcrProvider,
        batch_client: BatchGradingClient | None,
        clock: Clock,
        ids: IdProvider,
    ) -> None:
        """Inject collaborators.

        Args:
            sessionmaker: Session factory (repos are built per pass).
            storage: Object storage (attachment downloads).
            ocr: OCR provider.
            batch_client: Batch grading port (``None`` disables processing).
            clock: Time source.
            ids: Id provider (request keys).
        """
        self._sessionmaker = sessionmaker
        self._storage = storage
        self._ocr = ocr
        self._batch = batch_client
        self._clock = clock
        self._ids = ids

    async def run_pass(self) -> dict[str, int]:
        """Execute one worker pass: enqueue → start → poll.

        Returns:
            Counters ``{"queued", "started", "finalized"}``.
        """
        queued = await self.enqueue_due_date_assignments()
        started = await self.start_queued_grading_tasks()
        finalized = await self.poll_running_grading_tasks()
        return {"queued": queued, "started": started, "finalized": finalized}

    async def enqueue_due_date_assignments(self) -> int:
        """Create QUEUED tasks for due auto-grade assignments.

        Returns:
            Number of tasks created.
        """
        now = self._clock.now()
        async with session_scope(self._sessionmaker) as session:
            assignments = AssignmentRepository(session)
            tasks = BatchGradingTaskRepository(session)
            due = await assignments.find_due_for_auto_grading(now)
            queued = 0
            for assignment in due:
                await tasks.lock_assignment(assignment.id)
                if await tasks.active_for_assignment(str(assignment.id)) is not None:
                    continue
                assignment.auto_graded_at = now
                await tasks.add(
                    BatchGradingTask(
                        assignment_id=str(assignment.id),
                        school_id=assignment.school_id,
                        trigger=BatchGradingTrigger.DUE_DATE,
                        mode=BatchGradingMode.UNGRADED_ONLY,
                    )
                )
                queued += 1
            return queued

    async def start_queued_grading_tasks(self) -> int:
        """Launch QUEUED tasks: compile batch requests and create jobs.

        Returns:
            Number of tasks started.
        """
        if self._batch is None:
            logger.warning("Batch grading requested without a provider; skipping.")
            return 0
        started = 0
        async with session_scope(self._sessionmaker) as session:
            tasks = BatchGradingTaskRepository(session)
            for task in await tasks.list_by_status(BatchGradingTaskStatus.QUEUED):
                try:
                    await self._start_task(session, task)
                    started += 1
                except Exception:
                    logger.exception("Failed to start batch task %s", task.id)
                    task.status = BatchGradingTaskStatus.FAILED
                    task.error_message = "Failed to start batch grading."
                    task.completed_at = self._clock.now()
        return started

    async def poll_running_grading_tasks(self) -> int:
        """Poll RUNNING tasks and finalize finished ones.

        Returns:
            Number of tasks finalized.
        """
        if self._batch is None:
            return 0
        finalized = 0
        async with session_scope(self._sessionmaker) as session:
            tasks = BatchGradingTaskRepository(session)
            submissions = SubmissionRepository(session)
            for task in await tasks.list_by_status(BatchGradingTaskStatus.RUNNING):
                try:
                    if await self._poll_task(session, submissions, task):
                        finalized += 1
                except Exception as exc:
                    logger.exception("Failed polling batch task %s", task.id)
                    task.status = BatchGradingTaskStatus.FAILED
                    task.error_message = str(exc)
                    task.completed_at = self._clock.now()
        return finalized

    # ── internals ────────────────────────────────────────────────────────

    async def _start_task(self, session: AsyncSession, task: BatchGradingTask) -> None:
        """Compile a task's batch requests and create the provider job.

        Args:
            session: Open session owning this pass's transaction.
            task: QUEUED task (mutated to RUNNING with provider bookkeeping).
        """
        assert self._batch is not None
        submission_repo = SubmissionRepository(session)
        assignment = task.assignment
        submissions, _total = await submission_repo.list_for_assignment(
            str(assignment.id),
            PageParams(page=1, page_size=1000),
            only_ungraded=task.mode is BatchGradingMode.UNGRADED_ONLY,
        )
        mappings: dict[str, str] = {}
        requests: list[BatchGradeRequest] = []
        for index, submission in enumerate(submissions):
            request_key = f"req-{index}-{self._ids.new_id().hex[:8]}"
            mappings[request_key] = str(submission.id)
            ocr_texts, blobs = await self._collect_attachment_context(submission)
            prompt = build_grading_prompt(
                {
                    "subject": assignment.subject,
                    "title": assignment.title,
                    "description": assignment.description,
                    "grading_prompt": assignment.grading_prompt,
                    "strictness_level": assignment.strictness_level.value,
                    "check_spelling": assignment.check_spelling,
                    "check_grammar": assignment.check_grammar,
                    "check_plagiarism": assignment.check_plagiarism,
                    "grading_type": assignment.grading_type.value,
                },
                ocr_texts,
            )
            requests.append(BatchGradeRequest(key=request_key, prompt=prompt, files=tuple(blobs)))

        handle = await self._batch.create_job(requests)

        task.status = BatchGradingTaskStatus.RUNNING
        task.started_at = self._clock.now()
        task.total_submissions = len(requests)
        task.request_mappings = dict(mappings)
        task.gemini_job_name = handle.job_name
        task.gemini_input_file_name = handle.input_ref

    async def _collect_attachment_context(
        self, submission: Submission
    ) -> tuple[list[str], list[AttachmentBlob]]:
        """Download attachments and split them into OCR text vs raw blobs.

        Args:
            submission: Submission whose attachments to fetch.

        Returns:
            Tuple of (OCR texts, attachment blobs for provider inlining).
        """
        ocr_texts: list[str] = []
        blobs: list[AttachmentBlob] = []
        for attachment in submission.attachments:
            try:
                data = await self._storage.get_bytes(attachment.url)
            except Exception:
                logger.warning("Skipping unreadable attachment %s", attachment.url, exc_info=True)
                continue
            mime = attachment.type or guess_mime(attachment.name)
            ocr = await self._ocr.extract_text(data, mime)
            if ocr.text:
                ocr_texts.append(ocr.text)
                continue
            blobs.append(AttachmentBlob(data=data, mime_type=mime))
        return ocr_texts, blobs

    async def _poll_task(
        self,
        session: AsyncSession,
        submissions: SubmissionRepository,
        task: BatchGradingTask,
    ) -> bool:
        """Poll one RUNNING task; apply outcomes when the job succeeded.

        Per-request failures are isolated: each unparseable or errored
        response fails only its own submission (recorded in
        ``failure_details``); requests with no response are failed too.

        Args:
            session: Open session owning this pass's transaction.
            submissions: Submission repository for grade writes.
            task: Task to poll.

        Returns:
            ``True`` when the task reached a terminal state this pass.
        """
        assert self._batch is not None
        assert task.gemini_job_name is not None
        snapshot = await self._batch.poll(task.gemini_job_name)
        if snapshot.state is not BatchJobState.SUCCEEDED:
            if snapshot.state in TERMINAL_FAILURE_STATES:
                task.status = BatchGradingTaskStatus.FAILED
                task.error_message = f"Provider job state: {snapshot.state.value}" + (
                    f" ({snapshot.error})" if snapshot.error else ""
                )
                task.completed_at = self._clock.now()
                return True
            return False

        mappings = {k: str(v) for k, v in (task.request_mappings or {}).items()}
        outcomes = await self._batch.collect(task.gemini_job_name, snapshot)
        graded = 0
        failures: list[object] = []
        answered: set[str] = set()
        for outcome in outcomes:
            submission_id = mappings.get(outcome.key)
            if submission_id is None:
                logger.warning("Ignoring outcome for unknown key %s", outcome.key)
                continue
            if outcome.key in answered:
                logger.warning("Duplicate outcome for key %s ignored", outcome.key)
                continue
            answered.add(outcome.key)
            submission = await submissions.get(UUID(submission_id))
            if submission is None:
                failures.append({"key": outcome.key, "error": "submission not found"})
                continue
            if outcome.result is None:
                failures.append({"key": outcome.key, "error": outcome.error})
                continue
            apply_grading_result(submission, outcome.result)
            graded += 1

        failed = len(mappings) - graded
        failures.extend(
            {"key": key, "error": "no response"} for key in sorted(set(mappings) - answered)
        )

        task.graded_submissions = graded
        task.failed_submissions = failed
        task.processed_submissions = graded + failed
        task.failure_details = failures or None
        task.status = (
            BatchGradingTaskStatus.COMPLETED if failed == 0 else BatchGradingTaskStatus.PARTIAL
        )
        task.completed_at = self._clock.now()
        task.gemini_output_file_name = snapshot.output_ref

        await self._batch.dispose(task.gemini_job_name, task.gemini_input_file_name, snapshot)
        return True
