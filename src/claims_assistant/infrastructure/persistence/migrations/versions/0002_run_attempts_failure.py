"""Worker bookkeeping on runs: attempts and a safe failure reason (S2-02)

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("analysis_runs") as batch:
        batch.add_column(sa.Column("attempts", sa.Integer, nullable=False, server_default="0"))
        batch.add_column(sa.Column("failure", sa.String(500), nullable=True))
    op.create_index("ix_analysis_runs_status_updated", "analysis_runs", ["status", "updated_at"])


def downgrade() -> None:
    op.drop_index("ix_analysis_runs_status_updated", table_name="analysis_runs")
    with op.batch_alter_table("analysis_runs") as batch:
        batch.drop_column("failure")
        batch.drop_column("attempts")
