"""What the model cost us, by calendar month (S5-03)

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-23
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_spend",
        # «2026-09»: the calendar month in UTC, the same boundary the provider bills by.
        sa.Column("month", sa.String(7), primary_key=True),
        # Roubles as text: money is never added up in binary floats, and SQLite has no
        # decimal type of its own.
        sa.Column("spent_rub", sa.String(32), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.String(32), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("ai_spend")
