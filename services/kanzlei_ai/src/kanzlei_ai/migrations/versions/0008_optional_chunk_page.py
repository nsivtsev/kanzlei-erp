"""Allow nonpaginated excerpts to refer to their original source."""

from alembic import op

revision = "0008_optional_chunk_page"
down_revision = "0007_index_embedding_batches"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("document_chunks", "page", nullable=True)


def downgrade():
    op.alter_column("document_chunks", "page", nullable=False)
