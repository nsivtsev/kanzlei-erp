"""Mark PDF table fragments so a single row cannot be treated as a whole table."""

from alembic import op
from sqlalchemy import Boolean, Column, false

revision = "0013_pdf_table_fragments"
down_revision = "0012_outbox_cancellation"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "document_chunks", Column("table_fragment", Boolean, nullable=False, server_default=false())
    )


def downgrade():
    op.drop_column("document_chunks", "table_fragment")
