"""Domain package aggregator.

Imports every entity module so the SQLAlchemy registry is fully populated
regardless of which submodule a caller imports first. Relationship resolution
(``Mapped["User"]`` etc.) then always finds its target class.
"""

import app.domain.assessments
import app.domain.assignments
import app.domain.audit
import app.domain.enums
import app.domain.grading
import app.domain.schools
import app.domain.taxonomy
import app.domain.users  # noqa: F401
