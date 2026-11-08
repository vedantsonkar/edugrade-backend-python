"""Background batch-grading worker.

Implements the legacy prototype's state machine with its defects fixed:
OCR honors the school's ``enable_vision_ocr`` config, request mappings are
real (submission id → Gemini request key), and sessions are opened per pass
(workers own no long-lived DB session). Flow:

1. ``enqueue_due_date_assignments`` - due offline assignments with auto-grade
   become QUEUED tasks (deduped by ``auto_graded_at``).
2. ``start_queued_grading_tasks`` - QUEUED → RUNNING: build JSONL request
   payloads, upload to Gemini, create the batch job.
3. ``poll_running_grading_tasks`` - RUNNING tasks poll Gemini; on success the
   output JSONL is applied to submissions and tasks finalize
   (COMPLETED/PARTIAL/FAILED).
"""

import json
import logging
from typing import Any

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
from app.services.ai.llm import GRADING_RESPONSE_SCHEMA, BatchGeminiClient, build_grading_prompt
from app.services.ai.ocr import OcrProvider, guess_mime, temporary_jsonl_path

logger = logging.getLogger(__name__)


class BatchGradingProcessor:
    """Orchestrates the batch grading state machine on CRON triggers."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        storage: ObjectStorage,
        ocr: OcrProvider,
        gemini: BatchGeminiClient | None,
        clock: Clock,
        ids: IdProvider,
    ) -> None:
        """Inject collaborators.

        Args:
            sessionmaker: Session factory (repos are built per pass).
            storage: Object storage (attachment downloads).
            ocr: OCR provider (gated per school config).
            gemini: Batch API adapter (``None`` disables processing).
            clock: Time source.
            ids: Id provider (request keys).
        """
        self._sessionmaker = sessionmaker
        self._storage = storage
        self._ocr = ocr
        self._gemini = gemini
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
        """Launch QUEUED tasks: compile payloads and create Gemini jobs.

        Returns:
            Number of tasks started.
        """
        if self._gemini is None:
            logger.warning("Batch grading requested without Gemini config; skipping.")
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
        if self._gemini is None:
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
        """Compile a task's request payloads and create its Gemini batch.

        Args:
            session: Open session owning this pass's transaction.
            task: QUEUED task (mutated to RUNNING with Gemini bookkeeping).
        """
        assert self._gemini is not None
        assignments = AssignmentRepository(session)
        submission_repo = SubmissionRepository(session)
        assignment = task.assignment
        _ = assignments
        submissions, _total = await submission_repo.list_for_assignment(
            str(assignment.id),
            PageParams(page=1, page_size=1000),
            only_ungraded=task.mode is BatchGradingMode.UNGRADED_ONLY,
        )
        mappings: dict[str, str] = {}
        lines: list[str] = []
        for index, submission in enumerate(submissions):
            request_key = f"req-{index}-{self._ids.new_id().hex[:8]}"
            mappings[request_key] = str(submission.id)
            ocr_texts, inline = await self._collect_attachment_context(submission)
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
            contents: list[dict[str, Any]] = [{"text": prompt}]
            for part in inline:
                contents.append(part)
            request_payload = {
                "key": request_key,
                "request": {
                    "contents": [{"parts": contents}],
                    "generationConfig": {
                        "responseMimeType": "application/json",
                        "responseSchema": GRADING_RESPONSE_SCHEMA,
                    },
                },
            }
            lines.append(json.dumps(request_payload))

        path = temporary_jsonl_path(str(task.id))
        path.write_text("\n".join(lines), encoding="utf-8")
        input_file = await self._gemini.upload_jsonl(path)
        job_name = await self._gemini.create_batch(input_file)

        task.status = BatchGradingTaskStatus.RUNNING
        task.started_at = self._clock.now()
        task.total_submissions = len(submissions)
        task.request_mappings = dict(mappings)
        task.gemini_job_name = job_name
        task.gemini_input_file_name = input_file

    async def _collect_attachment_context(
        self, submission: Submission
    ) -> tuple[list[str], list[dict[str, object]]]:
        """Download attachments and split them into OCR text vs inline parts.

        Args:
            submission: Submission whose attachments to fetch.

        Returns:
            Tuple of (ocr texts, inline base64 parts).
        """
        import base64

        ocr_texts: list[str] = []
        inline: list[dict[str, object]] = []
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
            inline.append(
                {
                    "inline_data": {
                        "mime_type": mime,
                        "data": base64.b64encode(data).decode(),
                    }
                }
            )
        return ocr_texts, inline

    async def _poll_task(
        self,
        session: AsyncSession,
        submissions: SubmissionRepository,
        task: BatchGradingTask,
    ) -> bool:
        """Poll one RUNNING task; apply output when the job succeeded.

        Args:
            session: Open session owning this pass's transaction.
            submissions: Submission repository for grade writes.
            task: Task to poll.

        Returns:
            ``True`` when the task reached a terminal state this pass.
        """
        assert self._gemini is not None
        assert task.gemini_job_name is not None
        state, output_file = await self._gemini.get_batch_state(task.gemini_job_name)
        if state.upper() not in ("SUCCEEDED", "JOB_STATE_SUCCEEDED"):
            terminal_failure = any(
                marker in state.upper() for marker in ("FAILED", "CANCELLED", "EXPIRED")
            )
            if terminal_failure:
                task.status = BatchGradingTaskStatus.FAILED
                task.error_message = f"Gemini job state: {state}"
                task.completed_at = self._clock.now()
                return True
            return False

        mappings = {k: str(v) for k, v in (task.request_mappings or {}).items()}
        output = await self._gemini.download_output(output_file or "")
        graded = failed = 0
        for line in output.decode("utf-8").splitlines():
            if not line.strip():
                continue
            entry: dict[str, Any] = json.loads(line)
            submission_id = mappings.get(str(entry.get("key", "")))
            if submission_id is None:
                continue
            response = entry.get("response", {})
            text = _extract_response_text(response)
            if text is None:
                failed += 1
                continue
            parsed = json.loads(_strip_fences(text))
            from uuid import UUID

            submission = await submissions.get(UUID(submission_id))
            if submission is None:
                failed += 1
                continue
            submission.grade = float(parsed.get("score", 0))
            submission.ai_summary = str(parsed.get("summary", ""))
            submission.ai_feedback = str(parsed.get("feedback", ""))
            if parsed.get("plagiarismScore") is not None:
                submission.plagiarism_score = float(parsed["plagiarismScore"])
            if parsed.get("plagiarismAnalysis") is not None:
                submission.plagiarism_analysis = str(parsed["plagiarismAnalysis"])
            graded += 1

        task.graded_submissions = graded
        task.failed_submissions = failed
        task.processed_submissions = graded + failed
        task.status = (
            BatchGradingTaskStatus.COMPLETED if failed == 0 else BatchGradingTaskStatus.PARTIAL
        )
        task.completed_at = self._clock.now()

        await self._gemini.delete_file(task.gemini_input_file_name or "")
        if output_file:
            await self._gemini.delete_file(output_file)
        return True


def _extract_response_text(response: dict[str, Any]) -> str | None:
    """Pull the assistant text out of a Gemini batch response entry.

    Args:
        response: ``response`` object from the JSONL line.

    Returns:
        The text, or ``None`` when absent.
    """
    candidates = response.get("candidates") or []
    if not candidates:
        return None
    parts = (candidates[0].get("content") or {}).get("parts") or []
    texts = [str(p.get("text", "")) for p in parts if isinstance(p, dict)]
    joined = "".join(texts).strip()
    return joined or None


def _strip_fences(text: str) -> str:
    """Strip markdown fences from model output.

    Args:
        text: Raw text.

    Returns:
        Cleaned JSON text.
    """
    import re

    return re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
