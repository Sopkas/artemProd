"""Tables for analysis runs and uploaded files (S2-01).

Technical timestamps are stored as ISO 8601 text in UTC; calendar dates as ISO dates.
Money is not stored here yet. Alembic migrations under ``migrations/`` are the source of
truth for the deployed schema; this metadata mirrors them for queries and autogenerate.
"""

from sqlalchemy import (
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
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
    UniqueConstraint("run_id", "kind", "checksum", name="uq_uploaded_files_run_kind_checksum"),
    Index("ix_uploaded_files_run", "run_id", "sequence"),
)
