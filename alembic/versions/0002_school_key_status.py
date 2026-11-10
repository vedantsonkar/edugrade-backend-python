"""Rename schools.subdomain to school_key; add is_active and deleted_at.

Subdomain-based tenant routing is retired in favor of school keys typed on
the login page (single dashboard domain). Existing values are preserved by
the rename; status fields add disable/soft-delete support.

The initial revision builds tables via ``Base.metadata.create_all``, so a
database created against the current ORM already has the new schema; every
operation here is guarded by an inspector check and becomes a no-op then.

Revision ID: 0002_school_key_status
Revises: 0001_initial
Create Date: 2026-11-10
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_school_key_status"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Rename the tenant identifier column and add status fields."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("schools")}

    if "subdomain" in columns:
        indexes = {index["name"] for index in inspector.get_indexes("schools")}
        if "ix_schools_subdomain" in indexes:
            op.drop_index("ix_schools_subdomain", table_name="schools")
        op.alter_column(
            "schools",
            "subdomain",
            new_column_name="school_key",
            existing_type=sa.String(length=63),
        )
        op.create_index(
            "ix_schools_school_key", "schools", ["school_key"], unique=True
        )

    if "is_active" not in columns:
        op.add_column(
            "schools",
            sa.Column(
                "is_active",
                sa.Boolean(),
                server_default=sa.text("true"),
                nullable=False,
            ),
        )
        op.create_index("ix_schools_is_active", "schools", ["is_active"])

    if "deleted_at" not in columns:
        op.add_column(
            "schools",
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    """Restore the subdomain column and drop the status fields."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("schools")}

    if "deleted_at" in columns:
        op.drop_column("schools", "deleted_at")

    if "is_active" in columns:
        op.drop_index("ix_schools_is_active", table_name="schools")
        op.drop_column("schools", "is_active")

    if "school_key" in columns:
        indexes = {index["name"] for index in inspector.get_indexes("schools")}
        if "ix_schools_school_key" in indexes:
            op.drop_index("ix_schools_school_key", table_name="schools")
        op.alter_column(
            "schools",
            "school_key",
            new_column_name="subdomain",
            existing_type=sa.String(length=63),
        )
        op.create_index(
            "ix_schools_subdomain", "schools", ["subdomain"], unique=True
        )
