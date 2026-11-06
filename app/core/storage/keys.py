"""Storage key construction and access-scope validation.

Every object key in the system is produced here so naming stays consistent
(``uploads/{schoolId}/...``, ``snapshots/...``, ``avatars/...``,
``audit-logs/...``) and so scoping rules have a single home. Services call
:meth:`ensure_key_accessible` before serving any client-supplied key.
"""

import re
from datetime import date
from uuid import UUID

from uuid_utils.compat import uuid7

from app.core.errors import ForbiddenError, ValidationFailedError

_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/._-]*$")
"""Characters allowed in storage keys; blocks traversal and control chars."""

MAX_KEY_LENGTH = 512
"""Maximum accepted key length in characters."""

UNSAFE_FILENAME_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")
"""Characters stripped from user-supplied filenames inside keys."""


def sanitize_filename(filename: str) -> str:
    """Reduce a client-supplied filename to a safe key segment.

    Args:
        filename: Raw filename from the client.

    Returns:
        Sanitized filename (letters, digits, dot, underscore, dash only).

    Raises:
        ValidationFailedError: If nothing safe remains.
    """
    base = filename.rsplit("/", maxsplit=1)[-1].rsplit("\\", maxsplit=1)[-1]
    cleaned = UNSAFE_FILENAME_PATTERN.sub("_", base).strip("._")
    if not cleaned:
        raise ValidationFailedError("Filename contains no usable characters.")
    return cleaned[:150]


def _validate_key(key: str) -> str:
    """Validate the shape of a storage key.

    Args:
        key: Candidate key.

    Returns:
        The validated key.

    Raises:
        ValidationFailedError: If the key is empty, oversized, or malformed.
    """
    if not key or len(key) > MAX_KEY_LENGTH or not _KEY_PATTERN.match(key):
        raise ValidationFailedError("Malformed storage key.")
    if ".." in key:
        raise ValidationFailedError("Malformed storage key.")
    return key


def upload_key(school_id: str, filename: str) -> str:
    """Build the key for a school-scoped user upload.

    Args:
        school_id: Owner school id.
        filename: Client-supplied filename (sanitized here).

    Returns:
        Key of the form ``uploads/{schoolId}/{uuid}_{name}``.

    Raises:
        ValidationFailedError: If the filename is unusable.
    """
    return _validate_key(f"uploads/{school_id}/{uuid7()}_{sanitize_filename(filename)}")


def snapshot_key(assignment_id: str, student_id: str) -> str:
    """Build the key for a proctoring webcam snapshot.

    Args:
        assignment_id: Assignment the snapshot belongs to.
        student_id: Student captured in the snapshot.

    Returns:
        Key of the form ``snapshots/{assignmentId}/{studentId}/{ts}.jpg``.
    """
    stamp = uuid7().hex[:16]
    return _validate_key(
        f"snapshots/{assignment_id}/{student_id}/{stamp}.jpg"
    )


def avatar_key(user_id: str, filename: str) -> str:
    """Build the key for a user's avatar image.

    Args:
        user_id: Avatar owner.
        filename: Client-supplied filename (sanitized here).

    Returns:
        Key of the form ``avatars/{userId}/{uuid}_{name}``.

    Raises:
        ValidationFailedError: If the filename is unusable.
    """
    return _validate_key(f"avatars/{user_id}/{uuid7()}_{sanitize_filename(filename)}")


def audit_archive_key(school_id: str, day: date) -> str:
    """Build the key for a daily audit-log archive file.

    Args:
        school_id: Owning school.
        day: UTC day the archive covers.

    Returns:
        Key of the form ``audit-logs/{schoolId}/{YYYY-MM-DD}.json``.
    """
    return _validate_key(f"audit-logs/{school_id}/{day.isoformat()}.json")


def ensure_key_accessible(key: str, principal: object) -> None:
    """Verify the caller may access the given key, or raise.

    Scope rules:
      - ``uploads/{schoolId}/...`` - any member of that school;
      - ``snapshots/...`` and ``avatars/{userId}/...`` - the owning user or a
        staff member (teacher/admin) of any school;
      - ``audit-logs/{schoolId}/...`` - staff of that school (enforced at the
        route level, accepted here for shape only).

    Args:
        key: Storage key being requested.
        principal: Authenticated principal (duck-typed to avoid a core→app
            import; must expose ``id``, ``role``, ``school_id``).

    Raises:
        ValidationFailedError: If the key is malformed.
        ForbiddenError: If the key is outside the caller's scope.
    """
    _validate_key(key)
    caller_id = str(getattr(principal, "id", ""))
    caller_role = str(getattr(principal, "role", ""))
    caller_school = str(getattr(principal, "school_id", "") or "")
    is_staff = caller_role in ("TEACHER", "ADMIN")

    segments = key.split("/")
    if key.startswith("uploads/"):
        if len(segments) > 1 and segments[1] == caller_school:
            return
        raise ForbiddenError("Access to this file is not permitted.")

    if key.startswith("avatars/") and len(segments) > 1:
        if segments[1] == caller_id or is_staff:
            return
        raise ForbiddenError("Access to this file is not permitted.")

    if key.startswith("snapshots/"):
        if is_staff or caller_id in segments:
            return
        raise ForbiddenError("Access to this file is not permitted.")

    if key.startswith("audit-logs/"):
        if is_staff and len(segments) > 1 and segments[1] == caller_school:
            return
        raise ForbiddenError("Access to this file is not permitted.")

    raise ForbiddenError("Access to this file is not permitted.")


def school_of_key(key: str) -> str | None:
    """Extract the owning school id from a school-scoped key, if any.

    Args:
        key: Storage key.

    Returns:
        School id embedded in the key, or ``None`` for non-scoped prefixes.
    """
    segments = key.split("/")
    if len(segments) > 1 and segments[0] in ("uploads", "audit-logs"):
        try:
            UUID(segments[1])
        except ValueError:
            return None
        return segments[1]
    return None
