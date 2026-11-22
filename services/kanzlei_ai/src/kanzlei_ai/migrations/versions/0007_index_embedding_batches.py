"""Persist live index build progress and temporary paid embedding batches."""

from alembic import op
from sqlalchemy import Boolean, Column, ForeignKey, Integer, LargeBinary, String, false

revision = "0007_index_embedding_batches"
down_revision = "0006_ocr_checkpoints"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("jobs", Column("index_build_id", String(36)))
    op.add_column("jobs", Column("index_offset", Integer, nullable=False, server_default="0"))
    op.add_column(
        "jobs", Column("index_repair_started", Boolean, nullable=False, server_default=false())
    )
    op.create_table(
        "index_embedding_batches",
        Column("job_id", String(36), ForeignKey("jobs.id"), primary_key=True),
        Column("offset", Integer, primary_key=True),
        Column("vectors", LargeBinary, nullable=False),
    )


def downgrade():
    op.drop_table("index_embedding_batches")
    op.drop_column("jobs", "index_repair_started")
    op.drop_column("jobs", "index_offset")
    op.drop_column("jobs", "index_build_id")
