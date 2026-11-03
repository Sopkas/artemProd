"""Mark the release that can safely resume interrupted retention cleanup."""

from alembic import op
from sqlalchemy import text

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pending = (
        op.get_bind()
        .execute(text("SELECT COUNT(*) FROM analysis_runs WHERE status = 'purging'"))
        .scalar_one()
    )
    if pending:
        raise RuntimeError("Завершите очистку проверок перед откатом версии базы.")
