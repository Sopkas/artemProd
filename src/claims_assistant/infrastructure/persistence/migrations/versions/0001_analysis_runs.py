"""Analysis runs and uploaded files (S2-01)

Revision ID: 0001
Revises:
Create Date: 2026-09-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analysis_runs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("owner_id", sa.Integer, nullable=False),
        sa.Column("analysis_date", sa.String(10), nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.Column("updated_at", sa.String(32), nullable=False),
        sa.Column("sequence", sa.Integer, nullable=False),
    )
    op.create_index(
        "ix_analysis_runs_owner_created",
        "analysis_runs",
        ["owner_id", "created_at", "sequence"],
    )
    op.create_table(
        "uploaded_files",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("run_id", sa.String(32), sa.ForeignKey("analysis_runs.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer, nullable=False),
        sa.Column("stored_path", sa.String(512), nullable=False),
        sa.Column("uploaded_at", sa.String(32), nullable=False),
        sa.Column("coverage_start", sa.String(10), nullable=True),
        sa.Column("coverage_end", sa.String(10), nullable=True),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.UniqueConstraint(
            "run_id", "kind", "checksum", name="uq_uploaded_files_run_kind_checksum"
        ),
    )
    op.create_index("ix_uploaded_files_run", "uploaded_files", ["run_id", "sequence"])


def downgrade() -> None:
    op.drop_index("ix_uploaded_files_run", table_name="uploaded_files")
    op.drop_table("uploaded_files")
    op.drop_index("ix_analysis_runs_owner_created", table_name="analysis_runs")
    op.drop_table("analysis_runs")
