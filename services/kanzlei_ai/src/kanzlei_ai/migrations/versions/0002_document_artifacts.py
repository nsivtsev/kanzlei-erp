"""Persist fenced extracted-document artifacts and page-aware chunks."""

from alembic import op
from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table, Text, UniqueConstraint

from kanzlei_ai.models import metadata

revision = "0002_document_artifacts"
down_revision = "0001_sources_jobs"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    schema = MetaData()
    metadata.tables["sources"].to_metadata(schema)
    Table(
        "document_artifacts",
        schema,
        Column("id", Integer, primary_key=True),
        Column("source_id", ForeignKey("sources.id"), nullable=False),
        Column("revision", String(64), nullable=False),
        Column("generation", Integer, nullable=False),
        Column("parser_version", String(100), nullable=False),
        Column("content_sha256", String(64), nullable=False),
        Column("artifact_key", String(256), nullable=False),
        Column("page_count", Integer, nullable=False),
        UniqueConstraint("source_id", "revision", "generation", "parser_version"),
    ).create(bind)
    Table(
        "document_chunks",
        schema,
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
    ).create(bind)


def downgrade():
    bind = op.get_bind()
    metadata.tables["document_chunks"].drop(bind)
    metadata.tables["document_artifacts"].drop(bind)
