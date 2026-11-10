"""Shared application of AI grading results onto submissions.

Both grading paths (synchronous :class:`DirectGradingService` and the
:class:`~app.workers.batch_grader.BatchGradingProcessor` worker) must write
results identically - grading type handling (NUMBER vs ALPHABET) lives here
exactly once.
"""

from app.domain.grading import Submission
from app.services.ai.schema import GradingResult, letter_for


def apply_grading_result(submission: Submission, result: GradingResult) -> None:
    """Write a validated AI grading result onto a submission.

    ALPHABET assignments convert the model's 0-4 anchor into a letter grade
    (``ai_grade_letter``); numeric assignments store the score directly.
    Plagiarism fields are written whenever the model assessed them.

    Args:
        submission: Submission to update (mutated).
        result: Validated model output.
    """
    submission.ai_summary = result.summary
    submission.ai_feedback = result.feedback
    submission.plagiarism_score = result.plagiarism_score
    submission.plagiarism_analysis = result.plagiarism_analysis
    if submission.assignment.grading_type.value == "ALPHABET":
        submission.ai_grade_letter = letter_for(result.score)
        submission.grade = None
        return
    submission.grade = result.score
