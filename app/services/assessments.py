"""Online-assessment use cases: attempts, proctoring, submission."""

import random
from dataclasses import dataclass
from uuid import UUID

from app.core.clock import Clock
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationFailedError
from app.core.ids import IdProvider
from app.core.pagination import Page, PageParams
from app.core.security.principal import UserPrincipal
from app.core.storage.base import ObjectStorage
from app.core.storage.keys import snapshot_key
from app.domain.assessments import AssessmentAttempt, StudentAnswer
from app.domain.assignments import Assignment, Question, QuestionOption
from app.domain.enums import AssignmentType, AttemptStatus, QuestionType, UserRole
from app.repositories.assessments import (
    AssessmentAttemptRepository,
    StudentAnswerRepository,
)
from app.repositories.assignments import AssignmentRepository, QuestionRepository
from app.schemas.assessments import AttemptResponse
from app.services.assignments import AssignmentService
from app.services.grading.strategies import GradeOutcome, GradingStrategy


@dataclass(frozen=True)
class AttemptStart:
    """Start/resume outcome.

    Attributes:
        attempt: Attempt entity (answers loaded).
        questions: Answer-safe questions.
        remaining_seconds: Time left before auto-timeout.
        remaining_settings: Proctoring settings tuple
            ``(camera, screen_share, tab_detection, max_switches)``.
    """

    attempt: AssessmentAttempt
    questions: list[Question]
    remaining_seconds: int
    remaining_settings: tuple[bool, bool, bool, int]


@dataclass(frozen=True)
class AttemptSubmit:
    """Submission outcome.

    Attributes:
        attempt: Finalized attempt.
        score: Achieved points.
        max_score: Total possible points.
        percentage: Rounded percentage (``None`` when ungraded free text
            leaves the attempt partially graded).
    """

    attempt: AssessmentAttempt
    score: float
    max_score: float
    percentage: float | None


class AssessmentService:
    """Coordinates the online-assessment attempt workflow."""

    def __init__(
        self,
        *,
        attempts: AssessmentAttemptRepository,
        answers: StudentAnswerRepository,
        questions: QuestionRepository,
        assignments: AssignmentRepository,
        assignment_service: AssignmentService,
        strategies: dict[QuestionType, GradingStrategy],
        storage: ObjectStorage,
        clock: Clock,
        ids: IdProvider,
    ) -> None:
        """Inject collaborators.

        Args:
            attempts: Attempt repository.
            answers: Answer repository.
            questions: Question repository.
            assignments: Assignment repository.
            assignment_service: Assignment service (visibility rules).
            strategies: Question-type → grading-strategy registry.
            storage: Object storage (snapshot presign).
            clock: Time source.
            ids: Id provider (snapshot keys).
        """
        self._attempts = attempts
        self._answers = answers
        self._questions = questions
        self._assignments = assignments
        self._assignment_service = assignment_service
        self._strategies = strategies
        self._storage = storage
        self._clock = clock
        self._ids = ids

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self, principal: UserPrincipal, assignment_id: UUID) -> AttemptStart:
        """Start or resume the student's attempt.

        Resumes an in-progress attempt; a terminated/timed-out attempt blocks
        restarting; a submitted attempt raises conflict.

        Args:
            principal: Authenticated student (role enforced at route).
            assignment_id: Online assignment id.

        Returns:
            Attempt start data with sanitized questions.

        Raises:
            NotFoundError: When the assignment is absent/not targeted.
            ConflictError: When the attempt cannot be (re)started.
            ForbiddenError: When the caller is not a student.
        """
        if principal.role != UserRole.STUDENT.value:
            raise ForbiddenError("Only students take assessments.")
        assignment = await self._assignment_service.get_assignment(principal, assignment_id)
        if assignment.type is not AssignmentType.ONLINE:
            raise ConflictError("This assignment is not an online assessment.")

        now = self._clock.now()
        attempt = await self._attempts.get_for_student(assignment_id, str(principal.id))
        if attempt is None:
            from app.services.assignments import _aware

            if now < _aware(assignment.start_date) or now > _aware(assignment.due_date):
                raise ConflictError("The assessment window is closed.")
            created = await self._attempts.add(
                AssessmentAttempt(
                    assignment_id=str(assignment.id),
                    student_id=str(principal.id),
                    started_at=now,
                )
            )
            # Re-fetch so the answers collection is eager-loaded for
            # serialization (a flushed-new instance still lazy-loads).
            attempt = (
                await self._attempts.get_for_student(assignment_id, str(principal.id)) or created
            )
        elif not attempt.is_active:
            raise ConflictError(f"Attempt already {attempt.status.value.lower()}.")

        questions = await self._questions.list_for_assignment(str(assignment.id))
        shown = self._prepare_questions(assignment, questions)
        remaining = self._remaining_seconds(assignment, attempt, now)
        self._auto_timeout(assignment, attempt, now, remaining)

        return AttemptStart(
            attempt=attempt,
            questions=shown,
            remaining_seconds=max(0, remaining),
            remaining_settings=(
                assignment.require_camera,
                assignment.require_screen_share,
                assignment.enable_tab_detection,
                assignment.max_tab_switches,
            ),
        )

    async def status(
        self, principal: UserPrincipal, assignment_id: UUID, max_score_total: int | None
    ) -> AssessmentAttempt | None:
        """Return the caller's attempt (or ``None`` when not started).

        Args:
            principal: Authenticated student.
            assignment_id: Assignment id.
            max_score_total: Caller-facing total points, passed through.

        Returns:
            The attempt or ``None``.

        Raises:
            ForbiddenError: When the caller is not a student.
            NotFoundError: When the assignment is invisible.
        """
        if principal.role != UserRole.STUDENT.value:
            raise ForbiddenError("Only students have attempt status.")
        # Visibility check (raises NotFoundError for foreign assignments).
        await self._assignment_service.get_assignment(principal, assignment_id)
        _ = max_score_total  # caller-facing projection, unused internally
        return await self._attempts.get_for_student(assignment_id, str(principal.id))

    async def save_answer(
        self,
        principal: UserPrincipal,
        assignment_id: UUID,
        question_id: str,
        answer_data: object,
    ) -> StudentAnswer:
        """Save or replace the student's answer to one question.

        Args:
            principal: Authenticated student.
            assignment_id: Assignment id.
            question_id: Question id.
            answer_data: Raw payload (id/list/bool/text).

        Returns:
            The persisted answer (grading flags reset until submit).

        Raises:
            ConflictError: When no active attempt exists or time is up.
            NotFoundError: When the question is not in the assignment.
        """
        attempt, question = await self._active_context(principal, assignment_id, question_id)
        _ = question
        return await self._answers.upsert(
            attempt_id=str(attempt.id),
            question_id=question_id,
            answer_data=answer_data,
        )

    async def register_tab_switch(
        self, principal: UserPrincipal, assignment_id: UUID
    ) -> tuple[AssessmentAttempt, bool]:
        """Log a tab-switch event; terminate the attempt past the allowance.

        Args:
            principal: Authenticated student.
            assignment_id: Assignment id.

        Returns:
            Tuple of (attempt, terminated?).

        Raises:
            ConflictError: When no active attempt exists.
        """
        assignment = await self._assignment_service.get_assignment(principal, assignment_id)
        attempt = await self._attempts.get_active(assignment_id, str(principal.id))
        if attempt is None:
            raise ConflictError("No active attempt found.")
        terminated = attempt.register_tab_switch(
            max_switches=assignment.max_tab_switches,
            enable_detection=assignment.enable_tab_detection,
        )
        if terminated:
            attempt.terminate(at=self._clock.now())
        await self._attempts.session.flush()
        return attempt, terminated

    async def snapshot_url(self, principal: UserPrincipal, assignment_id: UUID) -> tuple[str, str]:
        """Create an upload grant for a proctoring snapshot and log the key.

        Args:
            principal: Authenticated student.
            assignment_id: Assignment id.

        Returns:
            Tuple of (upload URL, file key).

        Raises:
            ConflictError: When no active attempt exists.
        """
        attempt = await self._attempts.get_active(assignment_id, str(principal.id))
        if attempt is None:
            raise ConflictError("No active attempt found.")
        key = snapshot_key(str(assignment_id), str(principal.id))
        grant = await self._storage.presign_put(key, "image/jpeg")
        log = attempt.proctoring_log or {"snapshots": [], "events": []}
        snapshots = log.setdefault("snapshots", [])
        if isinstance(snapshots, list):
            snapshots.append({"key": key, "timestamp": self._clock.now().isoformat()})
        attempt.proctoring_log = log
        await self._attempts.session.flush()
        return grant.url, key

    async def submit(self, principal: UserPrincipal, assignment_id: UUID) -> AttemptSubmit:
        """Finalize the attempt and persist grading aggregates.

        Auto-grades objective answers and grades free text via strategies.

        Args:
            principal: Authenticated student.
            assignment_id: Assignment id.

        Returns:
            Submission aggregates.

        Raises:
            ConflictError: When no active attempt exists.
        """
        assignment = await self._assignment_service.get_assignment(principal, assignment_id)
        attempt = await self._attempts.get_active(assignment_id, str(principal.id))
        if attempt is None:
            raise ConflictError("No active attempt found to submit.")

        now = self._clock.now()
        remaining = self._remaining_seconds(assignment, attempt, now)
        if remaining <= 0:
            attempt.terminate(at=now, timed_out=True)

        questions = await self._questions.list_for_assignment(str(assignment.id))
        score = 0.0
        max_score = 0.0
        ungraded = False
        for question in questions:
            max_score += question.points
            answer = await self._answers.get_for_question(str(attempt.id), str(question.id))
            if answer is None:
                continue
            strategy = self._strategies.get(question.type)
            if strategy is None:
                ungraded = True
                continue
            outcome: GradeOutcome = await strategy.grade(question, answer)
            answer.is_correct = outcome.is_correct
            answer.points_awarded = outcome.points_awarded
            if outcome.points_awarded is not None:
                score += outcome.points_awarded
            else:
                ungraded = True

        attempt.score = round(score, 2)
        attempt.max_score = float(max_score)
        if attempt.status == AttemptStatus.IN_PROGRESS:
            attempt.status = AttemptStatus.SUBMITTED
            attempt.submitted_at = now
        await self._attempts.session.flush()

        percentage = None if ungraded or max_score == 0 else round(score / max_score * 100, 1)
        return AttemptSubmit(
            attempt=attempt, score=score, max_score=max_score, percentage=percentage
        )

    async def grade_attempt(
        self,
        principal: UserPrincipal,
        attempt_id: UUID,
        grade_override: float | None,
        question_overrides: dict[str, float] | None = None,
    ) -> AssessmentAttempt:
        """Teacher/admin override of an attempt's score.

        Args:
            principal: Authenticated staff caller.
            attempt_id: Attempt id.
            grade_override: New score in points, or None to clear.
            question_overrides: Updated points indexed by question id.

        Returns:
            The updated attempt.

        Raises:
            NotFoundError: When the attempt is not in the caller's school.
        """
        if principal.school_id is None:
            raise ForbiddenError("Caller has no school context.")
        attempt = await self._attempts.get_by_id(attempt_id, principal.school_id)
        if attempt is None:
            raise NotFoundError("Attempt not found.")
        await self._assignment_service.get_assignment(principal, UUID(str(attempt.assignment_id)))
        if principal.role not in (UserRole.ADMIN.value, UserRole.TEACHER.value):
            raise ForbiddenError("Staff access required.")
        if attempt.is_active:
            raise ConflictError("Finish the assessment before grading.")
        questions = await self._questions.list_for_assignment(str(attempt.assignment_id))
        by_id = {str(q.id): q for q in questions}
        for question_id, points in (question_overrides or {}).items():
            question = by_id.get(question_id)
            if question is None or not 0 <= points <= question.points:
                raise ValidationFailedError(
                    "Question override is outside the permitted point range."
                )
        if grade_override is not None and not 0 <= grade_override <= sum(
            q.points for q in questions
        ):
            raise ValidationFailedError("Overall override exceeds the assessment points.")
        for question_id, points in (question_overrides or {}).items():
            answer = await self._answers.get_for_question(str(attempt.id), question_id)
            if answer is None:
                answer = await self._answers.upsert(
                    attempt_id=str(attempt.id), question_id=question_id, answer_data=None
                )
            answer.points_awarded = points
            answer.is_correct = points == by_id[question_id].points
        await self._answers.session.flush()
        answers = await self._answers.list_for_attempt(str(attempt.id))
        attempt.score = sum(a.points_awarded or 0 for a in answers)
        attempt.grade_override = grade_override
        attempt.is_overridden = grade_override is not None
        await self._attempts.session.flush()
        return attempt

    async def review(
        self, principal: UserPrincipal, attempt_id: UUID
    ) -> tuple[AssessmentAttempt, list[Question]]:
        """Build the post-submission review for the attempt's owner.

        Args:
            principal: Authenticated student.
            attempt_id: Attempt id.

        Returns:
            Tuple of (attempt, questions with answers revealed).

        Raises:
            ForbiddenError: When another student's attempt is requested.
            NotFoundError: When the attempt is absent.
            ConflictError: When the attempt is still in progress.
        """
        if principal.school_id is None:
            raise ForbiddenError("Caller has no school context.")
        attempt = await self._attempts.get_by_id(attempt_id, principal.school_id)
        if attempt is None:
            raise NotFoundError("Attempt not found.")
        await self._assignment_service.get_assignment(principal, UUID(str(attempt.assignment_id)))
        if principal.role == UserRole.STUDENT.value:
            if str(attempt.student_id) != str(principal.id):
                raise ForbiddenError("You may only review your own attempt.")
            if not attempt.assignment.show_results_after:
                raise ForbiddenError("Results are not available yet.")
        if attempt.is_active:
            raise ConflictError("Finish the assessment before reviewing.")
        questions = await self._questions.list_for_assignment(str(attempt.assignment_id))
        return attempt, questions

    def present_attempt(
        self, attempt: AssessmentAttempt, principal: UserPrincipal
    ) -> AttemptResponse:
        """Respect result-release policy when projecting student attempt data."""
        visible = principal.role != UserRole.STUDENT.value or attempt.assignment.show_results_after
        return AttemptResponse.model_validate(attempt).model_copy(
            update={
                "score": attempt.effective_score if visible else None,
                "grade_override": attempt.grade_override if visible else None,
                "answers": [],
                "proctoring_log": None,
            }
        )

    async def list_attempts(
        self, principal: UserPrincipal, assignment_id: UUID, params: PageParams
    ) -> Page[AssessmentAttempt]:
        """List visible assignment attempts, restricted to the student's own when applicable."""
        await self._assignment_service.get_assignment(principal, assignment_id)
        if principal.role == UserRole.STUDENT.value:
            own = await self._attempts.get_for_student(assignment_id, str(principal.id))
            return Page.of([own] if own and params.page == 1 else [], 1 if own else 0, params)
        rows, total = await self._attempts.list_for_assignment(str(assignment_id), params)
        return Page.of(rows, total, params)

    async def review_snapshots(self, attempt: AssessmentAttempt) -> list[tuple[str, str]]:
        """Resolve logged snapshot keys after the caller passed review authorization."""
        results: list[tuple[str, str]] = []
        entries = (attempt.proctoring_log or {}).get("snapshots", [])
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and isinstance(entry.get("key"), str):
                    grant = await self._storage.presign_get(entry["key"])
                    results.append((grant.url, str(entry.get("timestamp", ""))))
        return results

    async def get_assignment_for(self, principal: UserPrincipal, assignment_id: UUID) -> Assignment:
        """Public passthrough for role-scoped assignment visibility.

        Args:
            principal: Authenticated caller.
            assignment_id: Assignment id.

        Returns:
            The assignment (visibility rules applied).

        Raises:
            NotFoundError: When absent or invisible.
        """
        return await self._assignment_service.get_assignment(principal, assignment_id)

    def remaining_for(self, assignment: Assignment, attempt: AssessmentAttempt) -> int:
        """Public accessor for the attempt's remaining time budget.

        Args:
            assignment: Assignment carrying the time limit.
            attempt: The caller's attempt.

        Returns:
            Seconds remaining (large when no limit is configured).
        """
        return self._remaining_seconds(assignment, attempt, self._clock.now())

    # ── Internals ────────────────────────────────────────────────────────

    async def _active_context(
        self, principal: UserPrincipal, assignment_id: UUID, question_id: str
    ) -> tuple[AssessmentAttempt, Question]:
        """Resolve the active attempt and validate the question belongs to it.

        Args:
            principal: Authenticated student.
            assignment_id: Assignment id.
            question_id: Question id.

        Returns:
            Tuple of (attempt, question).

        Raises:
            ConflictError: When no active attempt exists or time expired.
            NotFoundError: When the question is foreign.
        """
        attempt, terminated = await self._active_attempt(principal, assignment_id)
        if terminated:
            raise ConflictError("Time is up for this attempt.")
        question = await self._questions.get(UUID(question_id))
        if question is None or str(question.assignment_id) != str(attempt.assignment_id):
            raise NotFoundError("Question not found.")
        return attempt, question

    async def _active_attempt(
        self, principal: UserPrincipal, assignment_id: UUID
    ) -> tuple[AssessmentAttempt, bool]:
        """Fetch the active attempt, auto-timing-out when the window closed.

        Args:
            principal: Authenticated student.
            assignment_id: Assignment id.

        Returns:
            Tuple of (attempt, timed_out_now?).

        Raises:
            ConflictError: When no active attempt exists.
        """
        assignment = await self._assignment_service.get_assignment(principal, assignment_id)
        attempt = await self._attempts.get_active(assignment_id, str(principal.id))
        if attempt is None:
            raise ConflictError("No active attempt found.")
        remaining = self._remaining_seconds(assignment, attempt, self._clock.now())
        if remaining <= 0:
            attempt.terminate(at=self._clock.now(), timed_out=True)
            await self._attempts.session.flush()
            return attempt, True
        return attempt, False

    @staticmethod
    def _remaining_seconds(assignment: Assignment, attempt: AssessmentAttempt, now: object) -> int:
        """Compute seconds left on the attempt's time limit.

        Args:
            assignment: Assignment carrying the limit.
            attempt: Active attempt.
            now: Current instant (datetime).

        Returns:
            Seconds remaining; a large value when no limit is set.
        """
        from datetime import datetime as _dt

        from app.services.assignments import _aware

        assert isinstance(now, _dt)
        window = int((_aware(assignment.due_date) - now).total_seconds())
        if assignment.time_limit is None:
            return window
        elapsed = (now - _aware(attempt.started_at)).total_seconds()
        return min(window, int(assignment.time_limit * 60 - elapsed))

    def _auto_timeout(
        self,
        assignment: Assignment,
        attempt: AssessmentAttempt,
        now: object,
        remaining: int,
    ) -> None:
        """Terminate the attempt when its time budget is exhausted.

        Args:
            assignment: Assignment (unused beyond the limit check).
            attempt: Attempt to maybe terminate.
            now: Current instant.
            remaining: Seconds left.
        """
        _ = assignment, now
        if attempt.is_active and remaining <= 0:
            attempt.terminate(at=self._clock.now(), timed_out=True)
            self._attempts.session.add(attempt)

    @staticmethod
    def _prepare_questions(assignment: Assignment, questions: list[Question]) -> list[Question]:
        """Build the exam-time question view (shuffled and sanitized).

        Persisted entities are never mutated: values are copied onto
        detached instances.

        Args:
            assignment: Assignment (shuffle flags).
            questions: Persisted questions.

        Returns:
            Sanitized (and possibly reordered) question list.
        """
        sanitized: list[Question] = []
        for question in questions:
            clone = Question(
                assignment_id=question.assignment_id,
                type=question.type,
                text=question.text,
                points=question.points,
                is_required=question.is_required,
                display_order=question.display_order,
                explanation=None,
                min_char_length=question.min_char_length,
                max_char_length=question.max_char_length,
            )
            clone.id = question.id
            clone.created_at = question.created_at
            clone.updated_at = question.updated_at
            option_clones: list[QuestionOption] = []
            for option in question.options:
                option_clone = QuestionOption(
                    question_id=str(question.id),
                    text=option.text,
                    is_correct=False,
                    display_order=option.display_order,
                )
                option_clone.id = option.id
                option_clones.append(option_clone)
            if assignment.shuffle_options:
                random.shuffle(option_clones)
                for index, option_clone in enumerate(option_clones):
                    option_clone.display_order = index + 1
            clone.options = option_clones
            sanitized.append(clone)
        if assignment.shuffle_questions:
            random.shuffle(sanitized)
            for index, question in enumerate(sanitized):
                question.display_order = index + 1
        return sanitized
