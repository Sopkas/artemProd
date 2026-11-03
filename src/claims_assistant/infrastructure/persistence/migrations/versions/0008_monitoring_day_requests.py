"""What a monitoring day has spent: the ceiling is the day's, not the attempt's

``MONITORING_REQUEST_LIMIT`` is the day's ceiling, but every attempt of the day — a restart,
the scheduler's retry after a failed run — began with a fresh budget, and those the
requests had not reached were asked with another full day's (review of S8-09).

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-26
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("monitoring_days") as batch:
        # Requests the day's attempts took from the budget so far.
        batch.add_column(sa.Column("requests_used", sa.Integer, nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("monitoring_days") as batch:
        batch.drop_column("requests_used")
