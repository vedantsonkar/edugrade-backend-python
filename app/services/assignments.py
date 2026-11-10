"""Assignment use cases: CRUD, questions, submissions, comments."""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from app.core.clock import Clock
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationFailedError
from app.core.pagination import Page, PageParams
from app.core.security.principal import UserPrincipal
from app.domain.assignments import (
    Assignment,
    AssignmentTarget,
    Question,
    QuestionOption,
    RubricComment,
)
from app.domain.enums import (
    AssignmentState,
    AssignmentType,
    GradingType,
    QuestionType,
    StrictnessLevel,
    UserRole,
)
from app.domain.grading import Attachment, Submission
from app.domain.taxonomy import Classroom
from app.domain.users import User

if TYPE_CHECKING:
    from sqlalchemy import Select
from app.domain.enums import BatchGradingMode, BatchGradingTaskStatus, BatchGradingTrigger
from app.domain.grading import BatchGradingTask
from app.repositories.assignments import (
    AssignmentRepository,
    AssignmentTargetRepository,
    QuestionRepository,
    RubricCommentRepository,
)
from app.repositories.grading import (
    AttachmentRepository,
    BatchGradingTaskRepository,
    SubmissionRepository,
)
from app.repositories.taxonomy import ClassroomRepository
from app.schemas.assignments import (
    AssignmentCreate,
    AssignmentResponse,
    AssignmentUpdate,
    AttemptSummary,
    QuestionCreate,
    QuestionUpdate,
    SubmissionCreate,
    SubmissionGradeRequest,
    SubmissionResponse,
)


class AssignmentService:
    """Business workflows for assignments and their children."""

    def __init__(
        self,
        *,
        assignments: AssignmentRepository,
        targets: AssignmentTargetRepository,
        questions: QuestionRepository,
        comments: RubricCommentRepository,
        submissions: SubmissionRepository,
        attachments: AttachmentRepository,
        classrooms: ClassroomRepository,
        clock: Clock,
        tasks: BatchGradingTaskRepository,
    ) -> None:
        """Inject repositories and the clock.

        Args:
            assignments: Assignment repository.
            targets: Target repository.
            questions: Question repository.
            comments: Rubric-comment repository.
            submissions: Submission repository.
            attachments: Attachment repository (hand-in files).
            classrooms: Classroom repository (target validation).
            clock: Time source.
            tasks: Batch grading persistence.
        """
        self._assignments = assignments
        self._targets = targets
        self._questions = questions
        self._comments = comments
        self._submissions = submissions
        self._attachments = attachments
        self._classrooms = classrooms
        self._clock = clock
        self._tasks = tasks

    # ── Queries ──────────────────────────────────────────────────────────

    async def queue_grading(
        self, principal: UserPrincipal, assignment_id: UUID, mode: str
    ) -> BatchGradingTask:
        """Queue offline grading once; reject invalid modes or active duplicate work.

        Raises:
            ConflictError: If active work exists or the assignment is online.
            ValidationFailedError: If the mode is unknown.
            ForbiddenError: If the caller cannot manage the assignment.
        """
        assignment = await self._require_manageable(principal, assignment_id)
        if assignment.type != AssignmentType.OFFLINE:
            raise ConflictError("Grade-all is available for offline assignments only.")
        try:
            selected_mode = BatchGradingMode(mode)
        except ValueError as exc:
            raise ValidationFailedError("Unknown grading mode.") from exc
        await self._tasks.lock_assignment(assignment_id)
        if await self._tasks.active_for_assignment(str(assignment_id)) is not None:
            raise ConflictError("A grading task is already queued or running.")
        return await self._tasks.add(
            BatchGradingTask(
                assignment_id=str(assignment_id),
                school_id=str(principal.school_id),
                trigger=BatchGradingTrigger.MANUAL,
                mode=selected_mode,
                status=BatchGradingTaskStatus.QUEUED,
                created_by_id=str(principal.id),
            )
        )

    async def latest_grading(
        self, principal: UserPrincipal, assignment_id: UUID
    ) -> BatchGradingTask | None:
        """Read the latest task for an assignment the staff caller may view."""
        await self.get_assignment(principal, assignment_id)
        if principal.role not in (UserRole.TEACHER.value, UserRole.ADMIN.value):
            raise ForbiddenError("Staff access required.")
        return await self._tasks.latest_for_assignment(str(assignment_id))

    def present(self, assignment: Assignment, principal: UserPrincipal) -> AssignmentResponse:
        """Project viewer progress without exposing another student's data."""
        from app.domain.enums import AttemptStatus

        student = principal.role == UserRole.STUDENT.value
        submissions = [
            s
            for s in assignment.submissions
            if not student or str(s.student_id) == str(principal.id)
        ]
        attempts = [
            a for a in assignment.attempts if not student or str(a.student_id) == str(principal.id)
        ]
        graded = any(
            s.grade is not None or s.ai_grade_letter is not None or s.grade_override is not None
            for s in submissions
        )
        graded = graded or (
            (not student or assignment.show_results_after)
            and any(a.effective_score is not None for a in attempts)
        )
        now = self._clock.now()
        if graded:
            state = "graded"
        elif student and (
            submissions or any(a.status == AttemptStatus.SUBMITTED for a in attempts)
        ):
            state = "submitted"
        elif student and any(a.is_active for a in attempts):
            state = "ongoing"
        elif _aware(assignment.due_date) < now:
            state = "overdue"
        elif _aware(assignment.start_date) > now:
            state = "upcoming"
        elif _aware(assignment.due_date) <= now + timedelta(days=2):
            state = "due_soon"
        else:
            state = "ongoing"
        own_attempt = AttemptSummary.model_validate(attempts[0]) if student and attempts else None
        if own_attempt is not None and not assignment.show_results_after:
            own_attempt = own_attempt.model_copy(update={"score": None, "grade_override": None})
        return AssignmentResponse.model_validate(assignment).model_copy(
            update={
                "state": state,
                "submission_count": len(
                    {str(s.student_id) for s in submissions}
                    | {str(a.student_id) for a in attempts if not a.is_active}
                ),
                "my_submission": SubmissionResponse.model_validate(submissions[0])
                if student and submissions
                else None,
                "my_attempt": own_attempt,
            }
        )

    async def list_assignments(
        self,
        principal: UserPrincipal,
        params: PageParams,
        *,
        q: str | None = None,
        state: str | None = None,
        class_ids: list[str] | None = None,
    ) -> Page[Assignment]:
        """List assignments according to the caller's role.

        Args:
            principal: Authenticated caller.
            params: Pagination window.
            q: Title/subject substring.
            state: Lifecycle-state filter value.
            class_ids: Optional classroom filter (comma-separated upstream).

        Returns:
            Paginated assignments.

        Raises:
            ValidationFailedError: On unknown state value.
            ForbiddenError: When the caller has no school context.
        """
        if principal.school_id is None:
            raise ForbiddenError("Caller has no school context.")
        school_id = principal.school_id
        now = self._clock.now()
        state_filter = self._parse_state(state)

        if principal.role == UserRole.STUDENT.value:
            class_ids_own = await self._student_class_ids(school_id, principal.id)
            items, total = await self._assignments.list_for_student(
                school_id,
                class_ids_own,
                params,
                student_id=str(principal.id),
                q=q,
                class_filter=class_ids,
                state=state_filter,
                now=now,
            )
        elif principal.role == UserRole.TEACHER.value:
            items, total = await self._assignments.list_for_teacher(
                school_id,
                str(principal.id),
                params,
                q=q,
                class_filter=class_ids,
                state=state_filter,
                now=now,
            )
        else:
            items, total = await self._assignments.list_for_admin(
                school_id,
                params,
                q=q,
                class_filter=class_ids,
                state=state_filter,
                now=now,
            )
        return Page.of(items, total, params)

    async def get_assignment(self, principal: UserPrincipal, assignment_id: UUID) -> Assignment:
        """Fetch one assignment enforced to the caller's school and role.

        Students may only open targeted assignments.

        Args:
            principal: Authenticated caller.
            assignment_id: Assignment id.

        Returns:
            The assignment.

        Raises:
            NotFoundError: When absent or invisible to the caller.
            ForbiddenError: When the caller has no school context.
        """
        if principal.school_id is None:
            raise ForbiddenError("Caller has no school context.")
        assignment = await self._assignments.get_scoped(assignment_id, principal.school_id)
        if assignment is None:
            raise NotFoundError("Assignment not found.")
        if principal.role == UserRole.STUDENT.value:
            class_ids = await self._student_class_ids(principal.school_id, principal.id)
            if not any(str(t.class_id) in class_ids for t in assignment.targets):
                raise NotFoundError("Assignment not found.")
        return assignment

    # ── Mutations (staff) ────────────────────────────────────────────────

    async def create_assignment(
        self, principal: UserPrincipal, payload: AssignmentCreate
    ) -> Assignment:
        """Create an assignment with classroom targets.

        Args:
            principal: Authenticated staff caller.
            payload: Creation payload.

        Returns:
            The created assignment.

        Raises:
            ValidationFailedError: On invalid enum values or target sets.
            ForbiddenError: When the caller has no school context.
        """
        if principal.school_id is None:
            raise ForbiddenError("Caller has no school context.")
        assignment_type = self._parse_type(payload.type)
        if assignment_type is AssignmentType.ONLINE and payload.time_limit is None:
            raise ValidationFailedError("Online assignments require a time limit.")

        if payload.class_ids:
            await self._validate_targets(principal.school_id, payload.class_ids)

        assignment = await self._assignments.add(
            Assignment(
                school_id=principal.school_id,
                teacher_id=str(principal.id),
                type=assignment_type,
                title=payload.title.strip(),
                description=payload.description,
                subject=payload.subject.strip(),
                start_date=payload.start_date or self._clock.now(),
                due_date=payload.due_date,
                auto_grade_on_due_date=payload.auto_grade_on_due_date,
                allow_late_submissions=payload.allow_late_submissions,
                strictness_level=StrictnessLevel(payload.strictness_level),
                grading_prompt=payload.grading_prompt,
                check_spelling=payload.check_spelling,
                check_grammar=payload.check_grammar,
                check_plagiarism=payload.check_plagiarism,
                grading_type=GradingType(payload.grading_type),
                grading_config=payload.grading_config,
                time_limit=payload.time_limit,
                shuffle_questions=payload.shuffle_questions,
                shuffle_options=payload.shuffle_options,
                show_results_after=payload.show_results_after,
                require_camera=payload.require_camera,
                require_screen_share=payload.require_screen_share,
                enable_tab_detection=payload.enable_tab_detection,
                max_tab_switches=payload.max_tab_switches,
            )
        )
        for class_id in payload.class_ids:
            await self._targets.add(
                AssignmentTarget(assignment_id=str(assignment.id), class_id=class_id)
            )
        refreshed = await self._assignments.get_scoped(assignment.id, principal.school_id)
        return refreshed or assignment

    async def update_assignment(
        self,
        principal: UserPrincipal,
        assignment_id: UUID,
        payload: AssignmentUpdate,
    ) -> Assignment:
        """Update an assignment; only the creator or an admin may do so.

        Args:
            principal: Authenticated staff caller.
            assignment_id: Assignment id.
            payload: Patch payload.

        Returns:
            The updated assignment.

        Raises:
            NotFoundError: When absent in the caller's school.
            ForbiddenError: When a non-creator teacher attempts the edit.
            ValidationFailedError: On invalid enum values.
        """
        assignment = await self._require_manageable(principal, assignment_id)
        fields = payload.model_dump(exclude_unset=True)

        enum_map = {
            "strictness_level": StrictnessLevel,
            "grading_type": GradingType,
        }
        for key, enum_cls in enum_map.items():
            if key in fields and fields[key] is not None:
                fields[key] = enum_cls(str(fields[key]))

        if "class_ids" in fields and fields["class_ids"] is not None:
            class_ids = [str(c) for c in fields.pop("class_ids")]
            if principal.school_id is not None:
                await self._validate_targets(principal.school_id, class_ids)
            await self._targets.replace_for_assignment(str(assignment.id), class_ids)

        for key, value in fields.items():
            setattr(assignment, key, value)
        await self._assignments.session.flush()
        refreshed = await self._assignments.get_scoped(
            assignment.id,
            principal.school_id if principal.school_id is not None else assignment.school_id,
        )
        return refreshed or assignment

    async def delete_assignment(self, principal: UserPrincipal, assignment_id: UUID) -> None:
        """Delete an assignment; only the creator or an admin may do so.

        Questions, submissions, attempts, targets, and related rows are
        removed with it (ORM ``cascade="all, delete-orphan"`` backed by
        ``ondelete="CASCADE"`` foreign keys).

        Args:
            principal: Authenticated staff caller.
            assignment_id: Assignment id.

        Raises:
            NotFoundError: When absent in the caller's school.
            ForbiddenError: When a non-creator teacher attempts the delete.
        """
        assignment = await self._require_manageable(principal, assignment_id)
        await self._assignments.delete(assignment)

    # ── Questions ────────────────────────────────────────────────────────

    async def list_questions(self, principal: UserPrincipal, assignment_id: UUID) -> list[Question]:
        """List an assignment's questions.

        Students receive the questions with ``is_correct`` and explanations
        stripped (exam security; the leak closed from the legacy backend).

        Args:
            principal: Authenticated caller.
            assignment_id: Assignment id.

        Returns:
            Ordered questions (sanitized for students).
        """
        assignment = await self.get_assignment(principal, assignment_id)
        questions = await self._questions.list_for_assignment(str(assignment.id))
        if principal.role == UserRole.STUDENT.value:
            return self._sanitize_questions(questions)
        return questions

    async def create_questions(
        self,
        principal: UserPrincipal,
        assignment_id: UUID,
        payloads: list[QuestionCreate],
    ) -> list[Question]:
        """Append one or many questions to an online assignment.

        Args:
            principal: Authenticated staff caller.
            assignment_id: Assignment id.
            payloads: Questions in display order.

        Returns:
            The created questions.

        Raises:
            ValidationFailedError: When options are missing for objective
                types or no correct option is marked.
        """
        assignment = await self._require_manageable(principal, assignment_id)
        if assignment.type is not AssignmentType.ONLINE:
            raise ValidationFailedError("Questions belong to online assignments.")
        for payload in payloads:
            self._validate_question_payload(payload)
        order = await self._questions.next_display_order(str(assignment.id))
        created: list[Question] = []
        for payload in payloads:
            question = await self._questions.add(
                Question(
                    assignment_id=str(assignment.id),
                    type=QuestionType(payload.type),
                    text=payload.text,
                    points=payload.points,
                    is_required=payload.is_required,
                    display_order=order,
                    explanation=payload.explanation,
                    min_char_length=payload.min_char_length,
                    max_char_length=payload.max_char_length,
                    options=[
                        QuestionOption(
                            text=option.text,
                            is_correct=option.is_correct,
                            display_order=option.display_order,
                        )
                        for option in payload.options
                    ],
                )
            )
            created.append(question)
            order += 1
        return created

    async def update_question(
        self,
        principal: UserPrincipal,
        assignment_id: UUID,
        question_id: UUID,
        payload: QuestionUpdate,
    ) -> Question:
        """Update a question; providing options replaces the option set.

        Args:
            principal: Authenticated staff caller.
            assignment_id: Assignment id.
            question_id: Question id.
            payload: Patch payload.

        Returns:
            The updated question.

        Raises:
            NotFoundError: When the question is not in the assignment.
        """
        await self._require_manageable(principal, assignment_id)
        question = await self._questions.get_loaded(question_id)
        if question is None or str(question.assignment_id) != str(assignment_id):
            raise NotFoundError("Question not found.")
        try:
            selected_type = (
                QuestionType(payload.type) if payload.type is not None else question.type
            )
        except ValueError as exc:
            raise ValidationFailedError("Unknown question type.") from exc
        from app.schemas.assignments import QuestionOptionCreate

        candidate_options = (
            payload.options
            if payload.options is not None
            else [
                QuestionOptionCreate(
                    text=o.text, is_correct=o.is_correct, display_order=o.display_order
                )
                for o in question.options
            ]
        )
        if selected_type == QuestionType.FREE_TEXT:
            candidate_options = []
        candidate = QuestionCreate(
            type=selected_type.value, text=payload.text or question.text, options=candidate_options
        )
        self._validate_question_payload(candidate)
        fields = payload.model_dump(exclude_unset=True)
        options = fields.pop("options", None)
        if selected_type == QuestionType.FREE_TEXT:
            options = []
        fields.pop("type", None)
        question.type = selected_type
        for key, value in fields.items():
            setattr(question, key, value)
        if options is not None:
            question.options = [
                QuestionOption(
                    text=str(option["text"]),
                    is_correct=bool(option["is_correct"]),
                    display_order=int(option.get("display_order", index)),
                )
                for index, option in enumerate(options)
            ]
        await self._questions.session.flush()
        return question

    async def delete_question(
        self, principal: UserPrincipal, assignment_id: UUID, question_id: UUID
    ) -> None:
        """Delete a question and its options.

        Args:
            principal: Authenticated staff caller.
            assignment_id: Assignment id.
            question_id: Question id.

        Raises:
            NotFoundError: When the question is not in the assignment.
        """
        await self._require_manageable(principal, assignment_id)
        question = await self._questions.get(question_id)
        if question is None or str(question.assignment_id) != str(assignment_id):
            raise NotFoundError("Question not found.")
        await self._questions.delete(question)

    async def reorder_questions(
        self,
        principal: UserPrincipal,
        assignment_id: UUID,
        ordered_question_ids: list[str],
    ) -> None:
        """Apply a new display order to the assignment's questions.

        Args:
            principal: Authenticated staff caller.
            assignment_id: Assignment id.
            ordered_question_ids: Question ids in the desired order; every
                question of the assignment must appear exactly once.

        Raises:
            ValidationFailedError: When the id set does not match.
        """
        await self._require_manageable(principal, assignment_id)
        questions = await self._questions.list_for_assignment(str(assignment_id))
        by_id = {str(q.id): q for q in questions}
        if len(ordered_question_ids) != len(by_id) or not all(
            qid in by_id for qid in ordered_question_ids
        ):
            raise ValidationFailedError("Reorder payload must contain every question exactly once.")
        for index, qid in enumerate(ordered_question_ids):
            by_id[qid].display_order = index + 1
        await self._questions.session.flush()

    # ── Submissions ──────────────────────────────────────────────────────

    async def list_submissions(
        self,
        principal: UserPrincipal,
        assignment_id: UUID,
        params: PageParams,
        *,
        only_ungraded: bool = False,
    ) -> Page[Submission]:
        """List an assignment's submissions (staff only).

        Args:
            principal: Authenticated staff caller.
            assignment_id: Assignment id.
            params: Pagination window.
            only_ungraded: Restrict to ungraded rows.

        Returns:
            Paginated submissions.
        """
        await self.get_assignment(principal, assignment_id)
        if principal.role == UserRole.STUDENT.value:
            own = await self._submissions.get_for_student(str(assignment_id), str(principal.id))
            rows = [own] if own is not None and (not only_ungraded or own.grade is None) else []
            return Page.of(rows if params.page == 1 else [], len(rows), params)
        rows, total = await self._submissions.list_for_assignment(
            str(assignment_id), params, only_ungraded=only_ungraded
        )
        return Page.of(rows, total, params)

    async def submit_assignment(
        self,
        principal: UserPrincipal,
        assignment_id: UUID,
        payload: SubmissionCreate,
    ) -> Submission:
        """Create a student's submission with attachments.

        Args:
            principal: Authenticated student.
            assignment_id: Assignment id.
            payload: Attachment descriptors (storage keys from presign).

        Returns:
            The created submission.

        Raises:
            ForbiddenError: When the caller is not a targeted student.
            ConflictError: On duplicate submission or past the late window.
        """
        assignment = await self.get_assignment(principal, assignment_id)
        if principal.role != UserRole.STUDENT.value:
            raise ForbiddenError("Only students submit assignments.")
        existing = await self._submissions.get_for_student(str(assignment.id), str(principal.id))
        if existing is not None:
            raise ConflictError("Assignment already submitted.")
        now = self._clock.now()
        due = _aware(assignment.due_date)
        if now < _aware(assignment.start_date) or assignment.type != AssignmentType.OFFLINE:
            raise ConflictError("The file submission window is not open.")
        if now > due and not assignment.allow_late_submissions:
            raise ConflictError("The submission window has closed.")

        submission = await self._submissions.add(
            Submission(
                assignment_id=str(assignment.id),
                student_id=str(principal.id),
                submitted_at=now,
            )
        )
        for att in payload.attachments:
            await self._attachments.add(
                Attachment(
                    submission_id=str(submission.id),
                    name=att.name,
                    type=att.type,
                    url=att.url,
                    size=att.size,
                )
            )
        return submission

    async def override_grade(
        self,
        principal: UserPrincipal,
        assignment_id: UUID,
        submission_id: UUID,
        payload: SubmissionGradeRequest,
    ) -> Submission:
        """Apply a manual grade/notes override to a submission.

        Args:
            principal: Authenticated staff caller.
            assignment_id: Assignment id.
            submission_id: Submission id.
            payload: Override payload.

        Returns:
            The updated submission.
        """
        await self._require_manageable(principal, assignment_id)
        submission = await self._submissions.get(submission_id)
        if submission is None or str(submission.assignment_id) != str(assignment_id):
            raise NotFoundError("Submission not found.")
        submission.grade_override = payload.grade_override
        submission.teacher_notes = payload.teacher_notes
        submission.is_overridden = True
        await self._submissions.session.flush()
        loaded = await self._submissions.get_loaded(submission.id)
        return loaded or submission

    # ── Comments ─────────────────────────────────────────────────────────

    async def list_comments(
        self, principal: UserPrincipal, assignment_id: UUID
    ) -> list[RubricComment]:
        """List an assignment's rubric thread.

        Args:
            principal: Authenticated caller.
            assignment_id: Assignment id.

        Returns:
            Oldest-first comments.
        """
        await self.get_assignment(principal, assignment_id)
        return await self._comments.list_for_assignment(str(assignment_id))

    async def add_comment(
        self,
        principal: UserPrincipal,
        assignment_id: UUID,
        comment: str,
    ) -> RubricComment:
        """Post a comment to the rubric thread.

        Args:
            principal: Authenticated caller.
            assignment_id: Assignment id.
            comment: Message body.

        Returns:
            The created comment.
        """
        await self.get_assignment(principal, assignment_id)
        created = await self._comments.add(
            RubricComment(
                assignment_id=str(assignment_id),
                user_id=str(principal.id),
                comment=comment,
            )
        )
        loaded = await self._comments.get_loaded(created.id)
        return loaded or created

    # ── Internals ────────────────────────────────────────────────────────

    async def _student_class_ids(self, school_id: str, student_id: UUID) -> list[str]:
        """Resolve the classrooms a student belongs to.

        Args:
            school_id: Tenant.
            student_id: Student id.

        Returns:
            Classroom ids.
        """
        stmt = select_classrooms_for_student(school_id, str(student_id))
        rows = (await self._classrooms.session.execute(stmt)).scalars().all()
        return [str(row.id) for row in rows]

    async def _validate_targets(self, school_id: str, class_ids: list[str]) -> None:
        """Ensure target classrooms exist in the caller's school.

        Args:
            school_id: Tenant.
            class_ids: Candidate classroom ids.

        Raises:
            ValidationFailedError: When any id is foreign or unknown.
        """
        if not class_ids:
            return
        known = {str(c.id) for c in await self._classrooms.list_for_school(school_id)}
        unknown = set(class_ids) - known
        if unknown:
            raise ValidationFailedError(f"Unknown classroom targets: {', '.join(sorted(unknown))}")

    async def _require_manageable(
        self, principal: UserPrincipal, assignment_id: UUID
    ) -> Assignment:
        """Fetch an assignment the caller may manage (creator or admin).

        Args:
            principal: Authenticated staff caller.
            assignment_id: Assignment id.

        Returns:
            The assignment.

        Raises:
            NotFoundError: When absent in the school.
            ForbiddenError: When a non-creator teacher attempts management.
        """
        if principal.school_id is None:
            raise ForbiddenError("Caller has no school context.")
        assignment = await self._assignments.get_scoped(assignment_id, principal.school_id)
        if assignment is None:
            raise NotFoundError("Assignment not found.")
        if principal.role == UserRole.TEACHER.value and str(assignment.teacher_id) != str(
            principal.id
        ):
            raise ForbiddenError("Only the assignment's creator may manage it.")
        return assignment

    @staticmethod
    def _sanitize_questions(questions: list[Question]) -> list[Question]:
        """Build detached student projections without changing stored correct answers."""
        results: list[Question] = []
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
            clone.options = [
                QuestionOption(
                    id=option.id,
                    question_id=option.question_id,
                    text=option.text,
                    is_correct=False,
                    display_order=option.display_order,
                )
                for option in question.options
            ]
            results.append(clone)
        return results

    @staticmethod
    def _validate_question_payload(payload: QuestionCreate) -> None:
        """Validate type/option consistency for a new question.

        Args:
            payload: Candidate question.

        Raises:
            ValidationFailedError: On missing/extra options or missing
                correct answer.
        """
        qtype = QuestionType(payload.type)
        objective = qtype in (
            QuestionType.MCQ_SINGLE,
            QuestionType.MCQ_MULTIPLE,
            QuestionType.TRUE_FALSE,
        )
        if objective and len(payload.options) < 2:
            raise ValidationFailedError(f"{qtype.value} questions require at least two options.")
        if not objective and payload.options:
            raise ValidationFailedError("Free-text questions take no options.")
        if objective and not any(o.is_correct for o in payload.options):
            raise ValidationFailedError("Mark at least one correct option.")

    @staticmethod
    def _parse_type(value: str) -> AssignmentType:
        """Parse an assignment type string.

        Args:
            value: ``OFFLINE``/``ONLINE``.

        Returns:
            Parsed enum.

        Raises:
            ValidationFailedError: On unknown value.
        """
        try:
            return AssignmentType(value)
        except ValueError as exc:
            raise ValidationFailedError(f"Unknown assignment type: {value}") from exc

    @staticmethod
    def _parse_state(value: str | None) -> AssignmentState | None:
        """Parse a lifecycle-state filter value.

        Args:
            value: Raw state string.

        Returns:
            Parsed enum or ``None``.

        Raises:
            ValidationFailedError: On unknown value.
        """
        if value is None:
            return None
        try:
            return AssignmentState(value)
        except ValueError as exc:
            raise ValidationFailedError(f"Unknown assignment state: {value}") from exc


def _aware(value: datetime) -> datetime:
    """Normalize a stored datetime to timezone-aware UTC for comparisons.

    Sqlite returns naive datetimes; PG returns aware ones.

    Args:
        value: Datetime from the ORM.

    Returns:
        Aware datetime (unchanged when already aware).
    """
    from datetime import UTC

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def select_classrooms_for_student(school_id: str, student_id: str) -> Select[Classroom]:
    """Build the student→classrooms select (module-level for reuse in tests).

    Args:
        school_id: Tenant.
        student_id: Student id.

    Returns:
        A SQLAlchemy select over :class:`Classroom`.
    """
    from sqlalchemy import select

    return select(Classroom).where(
        Classroom.school_id == school_id,
        Classroom.grade_id == select(User.grade_id).where(User.id == student_id).scalar_subquery(),
        Classroom.division_id
        == select(User.division_id).where(User.id == student_id).scalar_subquery(),
    )
