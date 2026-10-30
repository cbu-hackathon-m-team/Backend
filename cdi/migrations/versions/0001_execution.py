"""Execution storage; project/test-suite CRUD is owned by the other backend."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "validations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("report", sa.JSON(), nullable=False),
    )
    op.create_table(
        "test_results",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("validations.id"), primary_key=True),
        sa.Column("env_id", sa.String(16), primary_key=True),
        sa.Column("case_id", sa.String(48), primary_key=True),
        sa.Column("evidence", sa.JSON(), nullable=False),
    )
    op.create_table(
        "execution_slot",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("validations.id"), nullable=False, unique=True),
    )


def downgrade():
    op.drop_table("execution_slot")
    op.drop_table("test_results")
    op.drop_table("validations")
