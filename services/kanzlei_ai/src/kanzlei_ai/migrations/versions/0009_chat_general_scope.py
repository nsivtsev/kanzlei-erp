"""Persist general conversations alongside existing client conversations."""

from alembic import op
from sqlalchemy import Column, String

revision = "0009_chat_general_scope"
down_revision = "0008_optional_chunk_page"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "chat_conversations",
        Column("scope_kind", String(16), nullable=False, server_default="client"),
    )
    op.alter_column("chat_conversations", "client_id", nullable=True)
    op.create_check_constraint(
        "ck_chat_conversations_scope",
        "chat_conversations",
        "(scope_kind = 'client' AND client_id IS NOT NULL) OR "
        "(scope_kind = 'general' AND client_id IS NULL)",
    )


def downgrade():
    # This takes the PostgreSQL table lock and refuses any existing general row.
    op.alter_column("chat_conversations", "client_id", nullable=False)
    op.drop_constraint("ck_chat_conversations_scope", "chat_conversations", type_="check")
    op.drop_column("chat_conversations", "scope_kind")
