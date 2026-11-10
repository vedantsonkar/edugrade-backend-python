"""Background batch-grading worker.

Implements the legacy prototype's state machine with its defects fixed:
request mappings are real (submission id → request key), sessions are opened
per pass (workers own no long-lived DB session), and results are applied
through the shared applier with per-submission isolation. Flow:

1. ``enqueue_due_date_assignments`` - due offline assignments with auto-grade
   become QUEUED tasks (deduped by ``auto_graded_at``).
2. ``start_queued_grading_tasks`` - QUEUED (retry gate open) → RUNNING:
   build batch requests, hand them to the configured
   :class:`BatchGradingClient` provider.
3. ``poll_running_grading_tasks`` - RUNNING tasks poll the provider; on
   success per-request outcomes are applied to submissions and tasks
   finalize (COMPLETED/PARTIAL/FAILED) or requeue for retry.

Failed runs enter a DB-backed retry queue: the failing attempt is recorded
as ``FAILED`` in ``grading_attempts``, and the task returns to ``QUEUED``
within the same transaction until the retry budget
(``max_attempts`` total executions) is exhausted. The first ``fast_retries``
retries run on the next worker pass; later retries wait out an exponential
backoff recorded in ``next_retry_at``. Retried tasks re-grade only
submissions that still lack a grade, so results applied before a failure
are preserved. Each execution (initial run and every retry) is one
:class:`GradingAttempt` row (PENDING → SUCCESS/FAILED).

The worker depends only on the provider-agnostic port: per-request failures
(malformed model output, provider errors) are isolated as failed
submissions and never abort the task.
"""

import logging
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.core.db.engine import session_scope
from app.core.events import TaskEventPublisher, grading_task_channel
from app.core.ids import IdProvider
from app.core.pagination import PageParams
from app.core.storage.base import ObjectStorage
from app.domain.enums import (
    BatchGradingMode,
    BatchGradingTaskStatus,
    BatchGradingTrigger,
    GradingAttemptStatus,
)
from app.domain.grading import BatchGradingTask, GradingAttempt, Submission
from app.repositories.assignments import AssignmentRepository
from app.repositories.grading import (
    BatchGradingTaskRepository,
    GradingAttemptRepository,
    SubmissionRepository,
)
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
        max_attempts: int = 5,
        fast_retries: int = 2,
        backoff_base_seconds: int = 60,
        backoff_cap_seconds: int = 900,
        events: TaskEventPublisher | None = None,
    ) -> None:
        """Inject collaborators.

        Args:
            sessionmaker: Session factory (repos are built per pass).
            storage: Object storage (attachment downloads).
            ocr: OCR provider.
            batch_client: Batch grading port (``None`` disables processing).
            clock: Time source.
            ids: Id provider (request keys).
            max_attempts: Total executions (initial run + retries) before a
                task is terminal.
            fast_retries: Retries requeued immediately before backoff.
            backoff_base_seconds: Exponential backoff base.
            backoff_cap_seconds: Upper bound on a single retry delay.
            events: Optional publisher for state transitions (``None``
                disables event distribution).
        """
        self._sessionmaker = sessionmaker
        self._storage = storage
        self._ocr = ocr
        self._batch = batch_client
        self._clock = clock
        self._ids = ids
        self._max_attempts = max(1, max_attempts)
        self._fast_retries = max(0, fast_retries)
        self._backoff_base = max(1, backoff_base_seconds)
        self._backoff_cap = max(1, backoff_cap_seconds)
        self._events = events

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
                task = await tasks.add(
                    BatchGradingTask(
                        assignment_id=str(assignment.id),
                        school_id=assignment.school_id,
                        trigger=BatchGradingTrigger.DUE_DATE,
                        mode=BatchGradingMode.UNGRADED_ONLY,
                    )
                )
                await GradingAttemptRepository(session).add(
                    GradingAttempt(task_id=str(task.id), attempt_number=1)
                )
                queued += 1
            return queued

    async def start_queued_grading_tasks(self) -> int:
        """Launch QUEUED tasks whose retry gate is open: compile and create jobs.

        Returns:
            Number of tasks handed to the provider this pass.
        """
        if self._batch is None:
            logger.warning("Batch grading requested without a provider; skipping.")
            return 0
        started = 0
        async with session_scope(self._sessionmaker) as session:
            tasks = BatchGradingTaskRepository(session)
            for task in await tasks.list_due_queued(self._clock.now()):
                try:
                    if await self._start_task(session, task):
                        started += 1
                except Exception:
                    logger.exception("Failed to start batch task %s", task.id)
                    await self._schedule_retry_or_fail(
                        session, task, "Failed to start batch grading."
                    )
        return started

    async def poll_running_grading_tasks(self) -> int:
        """Poll RUNNING tasks; finalize finished ones, requeue failed ones.

        Returns:
            Number of tasks that left RUNNING this pass (finalized or
            requeued for retry).
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
                    await self._schedule_retry_or_fail(session, task, str(exc))
        return finalized

    # ── internals ────────────────────────────────────────────────────────

    async def _publish_state(self, task: BatchGradingTask) -> None:
        """Notify subscribers that a task's state changed.

        Publishing is best-effort: failures are logged and never abort the
        grading pass (subscribers' safety ticks cover missed events).

        Args:
            task: Task whose state just changed.
        """
        if self._events is None:
            return
        try:
            await self._events.publish(
                grading_task_channel(str(task.assignment_id)),
                f'{{"type":"grading-task","status":"{task.status.value}"}}',
            )
        except Exception:
            logger.warning(
                "Failed to publish grading-task event for %s", task.id, exc_info=True
            )

    def _next_retry_at(self, retry_number: int) -> datetime:
        """Compute when a scheduled retry may run.

        The first ``fast_retries`` run immediately; later retries wait an
        exponential backoff (``base * 2^(retry - fast_retries - 1)``) capped
        at ``backoff_cap``.

        Args:
            retry_number: 1-based retry index being scheduled.

        Returns:
            The earliest instant the worker may pick the task up again.
        """
        now = self._clock.now()
        if retry_number <= self._fast_retries:
            return now
        delay = min(
            self._backoff_base * (2 ** (retry_number - self._fast_retries - 1)),
            self._backoff_cap,
        )
        return now + timedelta(seconds=delay)

    async def _schedule_retry_or_fail(
        self,
        session: AsyncSession,
        task: BatchGradingTask,
        error_message: str,
        *,
        submission_failure: bool = False,
    ) -> None:
        """Fail the current attempt, then requeue for retry or finalize.

        The task stays inside the QUEUED/RUNNING window on retry (status
        flips within this same transaction), so progress polling never
        observes a transient FAILED. When the budget is exhausted the task
        is terminal: FAILED for hard failures, PARTIAL when some submissions
        were graded.

        Args:
            session: Open session owning this pass's transaction.
            task: The task whose run just failed.
            error_message: Human-readable failure explanation.
            submission_failure: ``True`` when the run reached the provider
                but some submissions failed (terminal state is PARTIAL).
        """
        attempts = GradingAttemptRepository(session)
        current = await attempts.pending_for_task(str(task.id))
        if current is None:
            # Tasks predating the attempts table have no open row.
            current = await attempts.add(
                GradingAttempt(task_id=str(task.id), attempt_number=task.retry_count + 1)
            )
        current.status = GradingAttemptStatus.FAILED
        current.error_message = error_message
        current.completed_at = self._clock.now()

        task.retry_count += 1
        if task.retry_count < self._max_attempts:
            latest = await attempts.latest_for_task(str(task.id))
            next_number = (latest.attempt_number + 1) if latest else task.retry_count + 1
            await attempts.add(
                GradingAttempt(task_id=str(task.id), attempt_number=next_number)
            )
            task.status = BatchGradingTaskStatus.QUEUED
            task.next_retry_at = self._next_retry_at(task.retry_count)
            task.error_message = None
            task.completed_at = None
            task.gemini_job_name = None
            task.gemini_input_file_name = None
            task.gemini_output_file_name = None
            await self._publish_state(task)
            logger.info(
                "Batch task %s failed (attempt %s); retry scheduled at %s",
                task.id,
                current.attempt_number,
                task.next_retry_at,
            )
            return

        task.status = (
            BatchGradingTaskStatus.PARTIAL if submission_failure else BatchGradingTaskStatus.FAILED
        )
        task.error_message = error_message
        task.completed_at = self._clock.now()
        task.next_retry_at = None
        await self._publish_state(task)
        logger.warning(
            "Batch task %s terminal (%s) after %s attempts: %s",
            task.id,
            task.status.value,
            task.retry_count + 1,
            error_message,
        )

    async def _start_task(self, session: AsyncSession, task: BatchGradingTask) -> bool:
        """Compile a task's batch requests and create the provider job.

        Tasks requeued after a failure re-grade only submissions still
        missing a grade, regardless of their original mode. When nothing is
        in scope (e.g. grades were applied manually during backoff) the task
        finalizes COMPLETED without contacting the provider.

        Args:
            session: Open session owning this pass's transaction.
            task: QUEUED task (mutated to RUNNING with provider bookkeeping).

        Returns:
            ``True`` when a provider job was created, ``False`` when the
            task finalized with an empty scope.
        """
        assert self._batch is not None
        submission_repo = SubmissionRepository(session)
        assignment = task.assignment
        only_ungraded = task.mode is BatchGradingMode.UNGRADED_ONLY or task.retry_count > 0
        submissions, _total = await submission_repo.list_for_assignment(
            str(assignment.id),
            PageParams(page=1, page_size=1000),
            only_ungraded=only_ungraded,
        )
        attempt = await self._open_attempt(session, task)
        attempt.started_at = self._clock.now()

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

        if not requests:
            attempt.total_submissions = 0
            attempt.status = GradingAttemptStatus.SUCCESS
            attempt.completed_at = self._clock.now()
            task.status = BatchGradingTaskStatus.COMPLETED
            task.total_submissions = 0
            task.processed_submissions = 0
            task.graded_submissions = 0
            task.failed_submissions = 0
            task.error_message = None
            task.completed_at = self._clock.now()
            task.next_retry_at = None
            await self._publish_state(task)
            return False

        handle = await self._batch.create_job(requests)

        attempt.total_submissions = len(requests)
        task.status = BatchGradingTaskStatus.RUNNING
        task.started_at = self._clock.now()
        task.next_retry_at = None
        task.total_submissions = len(requests)
        task.request_mappings = dict(mappings)
        task.gemini_job_name = handle.job_name
        task.gemini_input_file_name = handle.input_ref
        await self._publish_state(task)
        return True

    async def _open_attempt(
        self, session: AsyncSession, task: BatchGradingTask
    ) -> GradingAttempt:
        """Fetch or create the PENDING attempt for the upcoming run.

        Args:
            session: Open session owning this pass's transaction.
            task: Task about to start running.

        Returns:
            The open attempt row.
        """
        attempts = GradingAttemptRepository(session)
        attempt = await attempts.pending_for_task(str(task.id))
        if attempt is None:
            latest = await attempts.latest_for_task(str(task.id))
            attempt = await attempts.add(
                GradingAttempt(
                    task_id=str(task.id),
                    attempt_number=(latest.attempt_number + 1) if latest else task.retry_count + 1,
                )
            )
        return attempt

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
        Runs with failed submissions enter the retry queue (re-grading only
        the ungraded leftovers) until the budget is exhausted.

        Args:
            session: Open session owning this pass's transaction.
            submissions: Submission repository for grade writes.
            task: Task to poll.

        Returns:
            ``True`` when the task left RUNNING this pass (finalized or
            requeued for retry).
        """
        assert self._batch is not None
        assert task.gemini_job_name is not None
        snapshot = await self._batch.poll(task.gemini_job_name)
        if snapshot.state is not BatchJobState.SUCCEEDED:
            if snapshot.state in TERMINAL_FAILURE_STATES:
                await self._schedule_retry_or_fail(
                    session,
                    task,
                    f"Provider job state: {snapshot.state.value}"
                    + (f" ({snapshot.error})" if snapshot.error else ""),
                )
                return True
            return False

        mappings = {k: str(v) for k, v in (task.request_mappings or {}).items()}
        outcomes = await self._batch.collect(task.gemini_job_name, snapshot)
        attempt = await self._open_attempt(session, task)
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
        task.completed_at = self._clock.now()
        task.gemini_output_file_name = snapshot.output_ref
        attempt.total_submissions = len(mappings)
        attempt.graded_submissions = graded
        attempt.failed_submissions = failed
        attempt.completed_at = self._clock.now()

        await self._batch.dispose(task.gemini_job_name, task.gemini_input_file_name, snapshot)

        if failed == 0:
            attempt.status = GradingAttemptStatus.SUCCESS
            task.status = BatchGradingTaskStatus.COMPLETED
            task.next_retry_at = None
            await self._publish_state(task)
            return True

        await self._schedule_retry_or_fail(
            session,
            task,
            f"{failed} of {len(mappings)} submissions failed grading.",
            submission_failure=True,
        )
        return True
