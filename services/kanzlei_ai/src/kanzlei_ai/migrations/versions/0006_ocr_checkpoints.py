"""Persist extraction routes and resumable, normalized OCR pages."""

from alembic import op
from sqlalchemy import JSON, Column, ForeignKey, Integer, String

revision = "0006_ocr_checkpoints"
down_revision = "0005_chat_conversations"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("connectors", Column("ocr_profile_override", String(16)))
    op.add_column(
        "jobs", Column("ocr_profile", String(16), nullable=False, server_default="native")
    )
    op.create_table(
        "extraction_plans",
        Column("job_id", String(36), ForeignKey("jobs.id"), primary_key=True),
        Column("parser_version", String(100), nullable=False),
        Column("content_sha256", String(64), nullable=False),
        Column("page_count", Integer, nullable=False),
        Column("native_blocks", JSON, nullable=False),
        Column("ocr_pages", JSON, nullable=False),
    )
    op.create_table(
        "ocr_page_checkpoints",
        Column("job_id", String(36), ForeignKey("extraction_plans.job_id"), primary_key=True),
        Column("page", Integer, primary_key=True),
        Column("provider_profile", String(100), nullable=False),
        Column("blocks", JSON, nullable=False),
    )


def downgrade():
    op.drop_table("ocr_page_checkpoints")
    op.drop_table("extraction_plans")
    op.drop_column("jobs", "ocr_profile")
    op.drop_column("connectors", "ocr_profile_override")
