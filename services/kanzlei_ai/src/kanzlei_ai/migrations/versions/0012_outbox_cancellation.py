"""Record cancellation independently of successful broker publication."""

from alembic import op
from sqlalchemy import Column, DateTime

revision = "0012_outbox_cancellation"
down_revision = "0011_retention"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("outbox", Column("cancelled_at", DateTime(timezone=True)))
    op.create_check_constraint(
        "ck_outbox_cancelled_delivery",
        "outbox",
        "cancelled_at IS NULL OR (published_at IS NULL AND "
        "lease_token IS NULL AND lease_until IS NULL)",
    )


def downgrade():
    op.drop_constraint("ck_outbox_cancelled_delivery", "outbox", type_="check")
    op.drop_column("outbox", "cancelled_at")
