"""Track observations and retry state for operator retention."""

from alembic import op
from sqlalchemy import CheckConstraint, Column, DateTime, Integer, String

revision = "0011_retention"
down_revision = "0010_pilot_ops"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "retention_candidates",
        Column("kind", String(16), primary_key=True),
        Column("scope", String(64), primary_key=True),
        Column("target", String(256), primary_key=True),
        Column("identity", String(256), nullable=False),
        Column("first_observed_at", DateTime(timezone=True), nullable=False),
        Column("last_observed_at", DateTime(timezone=True), nullable=False),
        Column("last_attempt_at", DateTime(timezone=True)),
        Column("failure_code", String(32)),
    )
    op.create_index(
        "ix_retention_candidates_due", "retention_candidates", ["first_observed_at", "kind"]
    )
    op.create_table(
        "retention_mark",
        Column("id", Integer, primary_key=True),
        Column("marked_at", DateTime(timezone=True), nullable=False),
        CheckConstraint("id = 1", name="ck_retention_mark_singleton"),
    )


def downgrade():
    op.drop_table("retention_mark")
    op.drop_table("retention_candidates")
