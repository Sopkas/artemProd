"""Each owner's monitoring settings: a pause and how often to check (S8-12)

The customer, 17.10.2026: «по кнопке в боте отключать ежедневную проверку, чтобы не
тратить деньги». A pause keeps the list and its last snapshots, so after it the comparison
goes on from where it stopped. No row means the defaults: every day, not paused.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "watch_settings",
        sa.Column("owner_id", sa.Integer, primary_key=True),
        # «daily», «weekdays» or «weekly» (each company once a week, a part every day).
        sa.Column("frequency", sa.String(16), nullable=False, server_default="daily"),
        # The business date the pause began; NULL — monitoring runs.
        sa.Column("paused_since", sa.String(10), nullable=True),
        sa.Column("updated_at", sa.String(32), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("watch_settings")
