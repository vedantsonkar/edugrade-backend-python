"""Add grading retry queue: grading_attempts table and task retry columns.

Failed batch grading tasks become retryable: each execution is recorded as a
``grading_attempts`` row (PENDING/SUCCESS/FAILED) and tasks gain
``retry_count`` plus ``next_retry_at`` for the DB-backed retry schedule.

The initial revision builds tables via ``Base.metadata.create_all``, so a
database created against the current ORM already has the new schema; every
operation here is guarded by an inspector check and becomes a no-op then.

Revision ID: 0003_grading_retry_attempts
Revises: 0002_school_key_status
Create Date: 2026-11-10
"""

import sqlalchemy as sa
from alembic import op

from app.core.db.base import FlexibleUuid
from app.domain.enums import GradingAttemptStatus

revision = "0003_grading_retry_attempts"
down_revision = "0002_school_key_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the attempts table and add the retry bookkeeping columns."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "grading_attempts" not in tables:
        op.create_table(
            "grading_attempts",
            sa.Column("id", FlexibleUuid(), primary_key=True),
            sa.Column(
                "task_id",
                FlexibleUuid(),
                sa.ForeignKey("batch_grading_tasks.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("attempt_number", sa.Integer(), nullable=False),
            sa.Column("status", sa.Enum(GradingAttemptStatus), nullable=False),
            sa.Column("total_submissions", sa.Integer(), nullable=False),
            sa.Column("graded_submissions", sa.Integer(), nullable=False),
            sa.Column("failed_submissions", sa.Integer(), nullable=False),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_grading_attempts_task_id", "grading_attempts", ["task_id"])
        op.create_index("ix_grading_attempts_status", "grading_attempts", ["status"])

    if "batch_grading_tasks" in tables:
        columns = {column["name"] for column in inspector.get_columns("batch_grading_tasks")}
        if "retry_count" not in columns:
            op.add_column(
                "batch_grading_tasks",
                sa.Column(
                    "retry_count",
                    sa.Integer(),
                    nullable=False,
                    server_default=sa.text("0"),
                ),
            )
        if "next_retry_at" not in columns:
            op.add_column(
                "batch_grading_tasks",
                sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
            )


def downgrade() -> None:
    """Drop the retry bookkeeping columns and the attempts table."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "batch_grading_tasks" in tables:
        columns = {column["name"] for column in inspector.get_columns("batch_grading_tasks")}
        if "next_retry_at" in columns:
            op.drop_column("batch_grading_tasks", "next_retry_at")
        if "retry_count" in columns:
            op.drop_column("batch_grading_tasks", "retry_count")

    if "grading_attempts" in tables:
        indexes = {index["name"] for index in inspector.get_indexes("grading_attempts")}
        if "ix_grading_attempts_status" in indexes:
            op.drop_index("ix_grading_attempts_status", table_name="grading_attempts")
        if "ix_grading_attempts_task_id" in indexes:
            op.drop_index("ix_grading_attempts_task_id", table_name="grading_attempts")
        op.drop_table("grading_attempts")
