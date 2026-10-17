"""Tables for analysis runs and uploaded files (S2-01).

Technical timestamps are stored as ISO 8601 text in UTC; calendar dates as ISO dates.
Money is not stored here yet. Alembic migrations under ``migrations/`` are the source of
truth for the deployed schema; this metadata mirrors them for queries and autogenerate.
"""

from sqlalchemy import (
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()

analysis_runs = Table(
    "analysis_runs",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("owner_id", Integer, nullable=False),
    Column("analysis_date", String(10), nullable=False),
    Column("mode", String(8), nullable=False),
    Column("status", String(16), nullable=False),
    Column("created_at", String(32), nullable=False),
    Column("updated_at", String(32), nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("failure", String(500), nullable=True),
    Index("ix_analysis_runs_owner_created", "owner_id", "created_at", "sequence"),
    Index("ix_analysis_runs_status_updated", "status", "updated_at"),
)

uploaded_files = Table(
    "uploaded_files",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("run_id", String(32), ForeignKey("analysis_runs.id"), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("checksum", String(64), nullable=False),
    Column("size_bytes", Integer, nullable=False),
    Column("stored_path", String(512), nullable=False),
    Column("uploaded_at", String(32), nullable=False),
    Column("coverage_start", String(10), nullable=True),
    Column("coverage_end", String(10), nullable=True),
    Column("sequence", Integer, nullable=False),
    # Whose 1C export this is (migration 0005); NULL: a file of the whole package.
    Column("inn", String(12), nullable=True),
    UniqueConstraint("run_id", "kind", "checksum", name="uq_uploaded_files_run_kind_checksum"),
    Index("ix_uploaded_files_run", "run_id", "sequence"),
)

run_steps = Table(
    "run_steps",
    metadata,
    Column("run_id", String(32), ForeignKey("analysis_runs.id"), nullable=False),
    Column("inn", String(10), nullable=False),
    Column("step", String(64), nullable=False),
    Column("version", String(64), nullable=False),
    Column("status", String(8), nullable=False),
    Column("payload", Text, nullable=True),
    Column("error", String(500), nullable=True),
    Column("completed_at", String(32), nullable=False),
    Column("sequence", Integer, nullable=False),
    PrimaryKeyConstraint("run_id", "inn", "step", "version", name="pk_run_steps"),
    Index("ix_run_steps_run", "run_id", "sequence"),
)

report_artifacts = Table(
    "report_artifacts",
    metadata,
    Column("run_id", String(32), ForeignKey("analysis_runs.id"), primary_key=True),
    Column("stored_path", String(512), nullable=False),
    Column("created_at", String(32), nullable=False),
    Column("delivery", String(16), nullable=False),
    Column("delivered_at", String(32), nullable=True),
    Column("delivery_error", String(500), nullable=True),
)

# S5-03: what the model cost us, by calendar month in UTC. Roubles as text — SQLite has
# no decimal type, and money is added up as Decimal in the store.
ai_spend = Table(
    "ai_spend",
    metadata,
    Column("month", String(7), primary_key=True),
    Column("spent_rub", String(32), nullable=False, server_default="0"),
    Column("updated_at", String(32), nullable=False),
)

# S8-09: the monitoring list of each owner and the last good snapshot of every section.
watchlist = Table(
    "watchlist",
    metadata,
    Column("owner_id", Integer, nullable=False),
    Column("inn", String(12), nullable=False),
    Column("name", String(300), nullable=True),
    Column("added_on", String(10), nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("last_checked_on", String(10), nullable=True),
    Column("last_result", Text, nullable=True),
    Column("last_change", String(500), nullable=True),
    Column("last_change_on", String(10), nullable=True),
    PrimaryKeyConstraint("owner_id", "inn", name="pk_watchlist"),
)

watch_snapshots = Table(
    "watch_snapshots",
    metadata,
    Column("owner_id", Integer, nullable=False),
    Column("inn", String(12), nullable=False),
    Column("section", String(16), nullable=False),
    Column("payload", Text, nullable=False),
    Column("fetched_at", String(32), nullable=False),
    PrimaryKeyConstraint("owner_id", "inn", "section", name="pk_watch_snapshots"),
    ForeignKeyConstraint(
        ["owner_id", "inn"],
        ["watchlist.owner_id", "watchlist.inn"],
        name="fk_watch_snapshots_entry",
        ondelete="CASCADE",
    ),
)

monitoring_days = Table(
    "monitoring_days",
    metadata,
    Column("day", String(10), primary_key=True),
    Column("started_at", String(32), nullable=False),
    Column("finished_at", String(32), nullable=True),
)

monitoring_outbox = Table(
    "monitoring_outbox",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("owner_id", Integer, nullable=False),
    Column("day", String(10), nullable=False),
    Column("kind", String(8), nullable=False),
    Column("inn", String(12), nullable=True),
    Column("text", Text, nullable=False),
    Column("created_at", String(32), nullable=False),
    Column("sent_at", String(32), nullable=True),
    Column("failed", String(200), nullable=True),
    Index("ix_monitoring_outbox_owner_day", "owner_id", "day"),
)
