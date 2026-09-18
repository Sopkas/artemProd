"""Saved step results and report artifacts (S3-03)

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-19
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_steps",
        sa.Column("run_id", sa.String(32), sa.ForeignKey("analysis_runs.id"), nullable=False),
        sa.Column("inn", sa.String(10), nullable=False),
        sa.Column("step", sa.String(64), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("payload", sa.Text, nullable=True),
        sa.Column("error", sa.String(500), nullable=True),
        sa.Column("completed_at", sa.String(32), nullable=False),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint("run_id", "inn", "step", "version", name="pk_run_steps"),
    )
    op.create_index("ix_run_steps_run", "run_steps", ["run_id", "sequence"])
    op.create_table(
        "report_artifacts",
        sa.Column("run_id", sa.String(32), sa.ForeignKey("analysis_runs.id"), primary_key=True),
        sa.Column("stored_path", sa.String(512), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.Column("delivery", sa.String(16), nullable=False),
        sa.Column("delivered_at", sa.String(32), nullable=True),
        sa.Column("delivery_error", sa.String(500), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("report_artifacts")
    op.drop_index("ix_run_steps_run", table_name="run_steps")
    op.drop_table("run_steps")
