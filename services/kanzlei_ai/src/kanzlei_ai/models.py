"""PostgreSQL tables for durable feed consumption and processing."""

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
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
    Column("error_code", String(64)),
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
    Column("stage", String(16), nullable=False, server_default="extract"),
    Column("artifact_id", ForeignKey("document_artifacts.id")),
    Column("status", String(32), nullable=False),
    Column("error_code", String(64)),
    Column("lease_token", String(36)),
    Column("lease_until", DateTime(timezone=True)),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("retry_not_before", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint(
        "source_id",
        "revision",
        "pipeline_version",
        "generation",
        "stage",
        name="uq_jobs_source_revision_pipeline_generation_stage",
    ),
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

document_artifacts = Table(
    "document_artifacts",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("source_id", ForeignKey("sources.id"), nullable=False),
    Column("revision", String(64), nullable=False),
    Column("generation", Integer, nullable=False),
    Column("parser_version", String(100), nullable=False),
    Column("content_sha256", String(64), nullable=False),
    Column("artifact_key", String(256), nullable=False),
    Column("page_count", Integer, nullable=False),
    UniqueConstraint("source_id", "revision", "generation", "parser_version"),
)
document_chunks = Table(
    "document_chunks",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("artifact_id", ForeignKey("document_artifacts.id"), nullable=False),
    Column("source_id", ForeignKey("sources.id"), nullable=False),
    Column("revision", String(64), nullable=False),
    Column("generation", Integer, nullable=False),
    Column("parser_version", String(100), nullable=False),
    Column("content_sha256", String(64), nullable=False),
    Column("page", Integer, nullable=False),
    Column("kind", String(20), nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("text", Text, nullable=False),
    UniqueConstraint("source_id", "revision", "generation", "parser_version", "ordinal"),
)
index_publications = Table(
    "index_publications",
    metadata,
    Column("source_id", ForeignKey("sources.id"), primary_key=True),
    Column("publication_id", String(36), nullable=False, unique=True),
    Column("artifact_id", ForeignKey("document_artifacts.id"), nullable=False),
    Column("revision", String(64), nullable=False),
    Column("generation", Integer, nullable=False),
    Column("profile", String(100), nullable=False),
)

chat_conversations = Table(
    "chat_conversations",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("connector_id", String(200), nullable=False),
    Column("subject", String(200), nullable=False),
    Column("client_id", String(200), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index(
        "ix_chat_conversations_owner_client",
        "connector_id",
        "subject",
        "client_id",
        "created_at",
        "id",
    ),
)
chat_turns = Table(
    "chat_turns",
    metadata,
    Column("id", String(36), primary_key=True),
    Column(
        "conversation_id", ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False
    ),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("status", String(16), nullable=False),
    Column("question", Text, nullable=False),
    Column("answer", Text),
    # These immutable snapshots carry exact source versions without pinning old index rows.
    Column("dependencies", JSON, nullable=False),
    Column("citations", JSON, nullable=False),
    CheckConstraint(
        "(status = 'answered' AND answer IS NOT NULL) OR "
        "(status = 'no_evidence' AND answer IS NULL)",
        name="ck_chat_turns_completed",
    ),
    Index("ix_chat_turns_conversation_page", "conversation_id", "created_at", "id"),
)
