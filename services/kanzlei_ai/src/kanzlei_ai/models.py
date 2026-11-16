"""PostgreSQL tables for durable feed consumption and processing."""

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)

metadata = MetaData()
connectors = Table(
    "connectors",
    metadata,
    Column("id", String(200), primary_key=True),
    Column("epoch", Integer, nullable=False, default=0),
    Column("phase", String(16), nullable=False),
    Column("inventory_cursor", Text),
    Column("watermark", Text),
    Column("changes_cursor", Text),
    Column("scan_completed_at", DateTime(timezone=True)),
)
sources = Table(
    "sources",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("connector_id", ForeignKey("connectors.id"), nullable=False),
    Column("source_id", String(200), nullable=False),
    Column("revision", String(64), nullable=False),
    Column("manifest", JSON, nullable=False),
    Column("generation", Integer, nullable=False),
    Column("active", Boolean, nullable=False),
    Column("status", String(32), nullable=False),
    UniqueConstraint("connector_id", "source_id"),
)
relations = Table(
    "source_clients",
    metadata,
    Column("source_id", ForeignKey("sources.id"), primary_key=True),
    Column("client_id", String(200), primary_key=True),
    Column("seen_epoch", Integer, nullable=False),
)
jobs = Table(
    "jobs",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("source_id", ForeignKey("sources.id"), nullable=False),
    Column("revision", String(64), nullable=False),
    Column("pipeline_version", String(100), nullable=False),
    Column("generation", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("lease_token", String(36)),
    Column("lease_until", DateTime(timezone=True)),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("retry_not_before", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("source_id", "revision", "pipeline_version", "generation"),
)
outbox = Table(
    "outbox",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("job_id", ForeignKey("jobs.id"), nullable=False),
    Column("lease_token", String(36)),
    Column("lease_until", DateTime(timezone=True)),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("published_at", DateTime(timezone=True)),
)

page_receipts = Table(
    "page_receipts",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("connector_id", ForeignKey("connectors.id"), nullable=False),
    Column("epoch", Integer, nullable=False),
    Column("phase", String(16), nullable=False),
    Column("expected_cursor", Text),
    Column("next_cursor", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
inbox_items = Table(
    "inbox_items",
    metadata,
    Column("receipt_id", ForeignKey("page_receipts.id"), primary_key=True),
    Column("ordinal", Integer, primary_key=True),
    Column("payload", JSON, nullable=False),
)
