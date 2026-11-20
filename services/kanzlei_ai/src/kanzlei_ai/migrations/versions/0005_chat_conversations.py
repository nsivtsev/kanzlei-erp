"""Add scoped conversations and completed turns without changing the index."""

from alembic import op
from sqlalchemy import JSON, CheckConstraint, Column, DateTime, ForeignKey, String, Text, func

revision = "0005_chat_conversations"
down_revision = "0004_index_publications"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "chat_conversations",
        Column("id", String(36), primary_key=True),
        Column("connector_id", String(200), nullable=False),
        Column("subject", String(200), nullable=False),
        Column("client_id", String(200), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
    op.create_index(
        "ix_chat_conversations_owner_client",
        "chat_conversations",
        ["connector_id", "subject", "client_id", "created_at", "id"],
    )
    op.create_table(
        "chat_turns",
        Column("id", String(36), primary_key=True),
        Column(
            "conversation_id",
            String(36),
            ForeignKey("chat_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("status", String(16), nullable=False),
        Column("question", Text, nullable=False),
        Column("answer", Text),
        Column("dependencies", JSON, nullable=False),
        Column("citations", JSON, nullable=False),
        CheckConstraint(
            "(status = 'answered' AND answer IS NOT NULL) OR "
            "(status = 'no_evidence' AND answer IS NULL)",
            name="ck_chat_turns_completed",
        ),
    )
    op.create_index(
        "ix_chat_turns_conversation_page", "chat_turns", ["conversation_id", "created_at", "id"]
    )


def downgrade():
    op.drop_table("chat_turns")
    op.drop_table("chat_conversations")
