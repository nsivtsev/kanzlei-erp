"""Allow conversations without a Mandant and generative assistant turns."""

from alembic import op
from sqlalchemy import Column, String

revision = "0014_chat_assistant"
down_revision = "0013_pdf_table_fragments"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_chat_conversations_scope", "chat_conversations", type_="check")
    op.create_check_constraint(
        "ck_chat_conversations_scope",
        "chat_conversations",
        "(scope_kind = 'client' AND client_id IS NOT NULL) OR "
        "(scope_kind IN ('general', 'assistant') AND client_id IS NULL)",
    )
    # Existing turns were produced by the exact-quote pipeline.
    op.add_column("chat_turns", Column("mode", String(16), nullable=False, server_default="strict"))
    op.create_check_constraint(
        "ck_chat_turns_mode", "chat_turns", "mode IN ('strict', 'assistant')"
    )


def downgrade():
    # This refuses any existing assistant conversation.
    op.drop_constraint("ck_chat_turns_mode", "chat_turns", type_="check")
    op.drop_column("chat_turns", "mode")
    op.drop_constraint("ck_chat_conversations_scope", "chat_conversations", type_="check")
    op.create_check_constraint(
        "ck_chat_conversations_scope",
        "chat_conversations",
        "(scope_kind = 'client' AND client_id IS NOT NULL) OR "
        "(scope_kind = 'general' AND client_id IS NULL)",
    )
