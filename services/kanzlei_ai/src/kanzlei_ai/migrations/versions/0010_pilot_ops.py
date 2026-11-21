"""Store successful poll time and the creation time of durable jobs."""

from alembic import op
from sqlalchemy import Column, DateTime, func

revision = "0010_pilot_ops"
down_revision = "0009_chat_general_scope"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("connectors", Column("last_successful_poll_at", DateTime(timezone=True)))
    op.add_column("jobs", Column("created_at", DateTime(timezone=True)))
    op.execute("UPDATE jobs SET created_at = updated_at")
    op.alter_column("jobs", "created_at", nullable=False, server_default=func.now())


def downgrade():
    op.drop_column("jobs", "created_at")
    op.drop_column("connectors", "last_successful_poll_at")
