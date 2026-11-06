"""Initial schema: all domain tables via ORM metadata.

The initial revision creates every table declared in ``app.domain`` through
``Base.metadata.create_all`` — deterministic and portable. Future revisions
must use explicit ``op.*`` operations (autogenerate).

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-07
"""

from alembic import op

from app.core.db.base import Base

# Import all domain modules so metadata is fully populated.
import app.domain.audit  # noqa: F401
import app.domain.assessments  # noqa: F401
import app.domain.assignments  # noqa: F401
import app.domain.grading  # noqa: F401
import app.domain.schools  # noqa: F401
import app.domain.taxonomy  # noqa: F401
import app.domain.users  # noqa: F401

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the initial schema."""
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    """Drop the initial schema."""
    Base.metadata.drop_all(bind=op.get_bind())
