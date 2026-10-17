"""The monitoring list, its last good snapshots, the daily runs and their messages (S8-09)

A list is its owner's: one Telegram user's companies, as checks are. For every company the
last *successful* snapshot of each watched section is kept — the rules of S8-06 compare
today's answer with it, so a case that appeared on a day the source was down is still new
the next day. A run is one business day; its messages wait in an outbox until Telegram
takes them, so an hour without Telegram does not lose a bankruptcy notice.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "watchlist",
        sa.Column("owner_id", sa.Integer, nullable=False),
        sa.Column("inn", sa.String(12), nullable=False),
        sa.Column("name", sa.String(300), nullable=True),
        sa.Column("added_on", sa.String(10), nullable=False),
        # Order of adding: the newest come first on the screen and in the file.
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("last_checked_on", sa.String(10), nullable=True),
        # The comparison of the last check, for the day's summary after a restart.
        sa.Column("last_result", sa.Text, nullable=True),
        sa.Column("last_change", sa.String(500), nullable=True),
        sa.Column("last_change_on", sa.String(10), nullable=True),
        sa.PrimaryKeyConstraint("owner_id", "inn", name="pk_watchlist"),
    )
    op.create_table(
        "watch_snapshots",
        sa.Column("owner_id", sa.Integer, nullable=False),
        sa.Column("inn", sa.String(12), nullable=False),
        sa.Column("section", sa.String(16), nullable=False),
        sa.Column("payload", sa.Text, nullable=False),
        sa.Column("fetched_at", sa.String(32), nullable=False),
        sa.PrimaryKeyConstraint("owner_id", "inn", "section", name="pk_watch_snapshots"),
        sa.ForeignKeyConstraint(
            ["owner_id", "inn"],
            ["watchlist.owner_id", "watchlist.inn"],
            name="fk_watch_snapshots_entry",
            ondelete="CASCADE",
        ),
    )
    op.create_table(
        "monitoring_days",
        # The business date of the run («2026-10-17»), not the UTC one.
        sa.Column("day", sa.String(10), primary_key=True),
        sa.Column("started_at", sa.String(32), nullable=False),
        sa.Column("finished_at", sa.String(32), nullable=True),
    )
    op.create_table(
        "monitoring_outbox",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer, nullable=False),
        sa.Column("day", sa.String(10), nullable=False),
        # «change» — one company, with the «Больше не следить» button; «summary» — the day.
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("inn", sa.String(12), nullable=True),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.Column("sent_at", sa.String(32), nullable=True),
        # Telegram refused it for good (the user blocked the bot): not retried.
        sa.Column("failed", sa.String(200), nullable=True),
    )
    op.create_index("ix_monitoring_outbox_owner_day", "monitoring_outbox", ["owner_id", "day"])


def downgrade() -> None:
    op.drop_index("ix_monitoring_outbox_owner_day", table_name="monitoring_outbox")
    op.drop_table("monitoring_outbox")
    op.drop_table("monitoring_days")
    op.drop_table("watch_snapshots")
    op.drop_table("watchlist")
