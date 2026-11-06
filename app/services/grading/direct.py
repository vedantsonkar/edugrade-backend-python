"""Direct (synchronous) AI grading of a single submission.

Downloads attachments from storage, OCRs what it can, asks the LLM grader,
and writes results onto the submission. The legacy backend hardcoded OCR on
for this path; here the school's ``enable_vision_ocr`` config is honored.
"""

import logging

from app.core.errors import ExternalServiceError, NotFoundError
from app.core.storage.base import ObjectStorage
from app.domain.grading import Submission
from app.domain.schools import SchoolConfig
from app.repositories.grading import SubmissionRepository
from app.repositories.schools import SchoolConfigRepository
from app.services.ai.llm import LlmGrader, LlmGradeResult
from app.services.ai.ocr import OcrProvider, guess_mime

logger = logging.getLogger(__name__)

INLINE_MIME_PREFIXES = ("image/",)
"""MIME types sent inline to the model (others rely on OCR text)."""


class DirectGradingService:
    """Real-time single-submission grading pipeline."""

    def __init__(
        self,
        *,
        submissions: SubmissionRepository,
        configs: SchoolConfigRepository,
        storage: ObjectStorage,
        llm: LlmGrader | None,
        ocr: OcrProvider,
    ) -> None:
        """Inject collaborators.

        Args:
            submissions: Submission repository.
            configs: School-config repository (OCR gate).
            storage: Object storage port.
            llm: LLM grader (``None`` when unconfigured → service errors).
            ocr: OCR provider.
        """
        self._submissions = submissions
        self._configs = configs
        self._storage = storage
        self._llm = llm
        self._ocr = ocr

    async def grade_submission(
        self,
        *,
        submission_id: str,
        school_id: str,
    ) -> Submission:
        """Grade one submission end-to-end.

        Args:
            submission_id: Submission to grade.
            school_id: Tenant (scoping and OCR-gate resolution).

        Returns:
            The graded submission.

        Raises:
            NotFoundError: When the submission is not in the school.
            ExternalServiceError: When no LLM grader is configured or the
                model call fails.
        """
        from uuid import UUID as _UUID

        submission = await self._submissions.get_scoped(_UUID(submission_id), school_id)
        if submission is None:
            raise NotFoundError("Submission not found.")
        if self._llm is None:
            raise ExternalServiceError("AI grading is not configured.")

        assignment = submission.assignment
        config: SchoolConfig | None = await self._configs.get_for_school(school_id)
        ocr_enabled = bool(config.enable_vision_ocr) if config else False

        ocr_texts: list[str] = []
        inline_parts: list[dict[str, object]] = []
        for attachment in submission.attachments:
            data = await self._storage.get_bytes(attachment.url)
            mime = attachment.type or guess_mime(attachment.name)
            if ocr_enabled:
                ocr_result = await self._ocr.extract_text(data, mime)
                if ocr_result.text:
                    ocr_texts.append(ocr_result.text)
                    continue
            if mime.startswith(INLINE_MIME_PREFIXES):
                import base64

                inline_parts.append(
                    {"data": base64.b64encode(data).decode(), "mime_type": mime}
                )

        result: LlmGradeResult = await self._llm.grade_submission(
            assignment={
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
            attachments=inline_parts,
            ocr_texts=ocr_texts,
        )

        self._apply(submission, result)
        await self._submissions.session.flush()
        return submission

    def _apply(self, submission: Submission, result: LlmGradeResult) -> None:
        """Write grading results onto the submission.

        Letter-scale assignments convert the model's 0-4 anchor into a
        letter grade; numeric assignments store the score directly.

        Args:
            submission: Submission to update (mutated).
            result: Model output.
        """
        submission.ai_summary = result.summary
        submission.ai_feedback = result.feedback
        submission.plagiarism_score = result.plagiarism_score
        submission.plagiarism_analysis = result.plagiarism_analysis
        if submission.assignment.grading_type.value == "ALPHABET":
            submission.ai_grade_letter = self._letter_for(result.score)
            submission.grade = None
            return
        submission.grade = result.score

    @staticmethod
    def _letter_for(score: float) -> str:
        """Map a 0-4 anchor score back onto its letter grade.

        Args:
            score: Model-provided anchor.

        Returns:
            Letter grade (``F`` for unrecognized anchors).
        """
        anchors = {4.0: "A", 3.0: "B", 2.0: "C", 1.0: "D", 0.5: "E", 0.0: "F"}
        nearest = min(anchors, key=lambda a: abs(a - score))
        return anchors[nearest]
