"""Whose 1C export a payments file is (block 5 of the manual test)

A 1C export is printed for one counterparty, so its period vouches for that company only;
our template covers the whole package. NULL keeps the old meaning for every file stored
before this revision.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-02
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("uploaded_files", sa.Column("inn", sa.String(12), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("uploaded_files") as batch:
        batch.drop_column("inn")
