"""Record the Mandanten whose structured ERP data an assistant turn used."""

from alembic import op
from sqlalchemy import JSON, Column, text

revision = "0015_chat_erp_clients"
down_revision = "0014_chat_assistant"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "chat_turns",
        Column("erp_clients", JSON, nullable=False, server_default=text("'[]'")),
    )


def downgrade():
    op.drop_column("chat_turns", "erp_clients")
