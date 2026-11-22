"""Persist bounded, generation-fenced processing failure codes."""

from alembic import op
from sqlalchemy import Column, String

revision = "0003_processing_error_codes"
down_revision = "0002_document_artifacts"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sources", Column("error_code", String(64), nullable=True))
    op.add_column("jobs", Column("error_code", String(64), nullable=True))


def downgrade():
    op.drop_column("jobs", "error_code")
    op.drop_column("sources", "error_code")
