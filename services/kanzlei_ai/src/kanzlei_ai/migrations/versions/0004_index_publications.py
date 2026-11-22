"""Preserve extraction jobs and add a separately leased index stage."""

from alembic import op
from sqlalchemy import Column, ForeignKey, Integer, String, inspect

revision = "0004_index_publications"
down_revision = "0003_processing_error_codes"
branch_labels = None
depends_on = None

JOB_KEY = ("source_id", "revision", "pipeline_version", "generation")
STAGED_KEY = "uq_jobs_source_revision_pipeline_generation_stage"


def upgrade():
    constraints = inspect(op.get_bind()).get_unique_constraints("jobs")
    old_key = next(
        constraint["name"]
        for constraint in constraints
        if tuple(constraint["column_names"]) == JOB_KEY
    )
    op.drop_constraint(old_key, "jobs", type_="unique")
    op.add_column("jobs", Column("stage", String(16), nullable=False, server_default="extract"))
    op.add_column("jobs", Column("artifact_id", Integer, ForeignKey("document_artifacts.id")))
    op.create_unique_constraint(STAGED_KEY, "jobs", [*JOB_KEY, "stage"])
    op.create_table(
        "index_publications",
        Column("source_id", Integer, ForeignKey("sources.id"), primary_key=True),
        Column("publication_id", String(36), nullable=False, unique=True),
        Column("artifact_id", Integer, ForeignKey("document_artifacts.id"), nullable=False),
        Column("revision", String(64), nullable=False),
        Column("generation", Integer, nullable=False),
        Column("profile", String(100), nullable=False),
    )


def downgrade():
    op.execute("UPDATE sources SET status = 'extracted' WHERE status = 'ready'")
    op.drop_table("index_publications")
    op.execute("DELETE FROM outbox WHERE job_id IN (SELECT id FROM jobs WHERE stage = 'index')")
    op.execute("DELETE FROM jobs WHERE stage = 'index'")
    op.drop_constraint(STAGED_KEY, "jobs", type_="unique")
    op.drop_column("jobs", "artifact_id")
    op.drop_column("jobs", "stage")
    op.create_unique_constraint(
        "jobs_source_id_revision_pipeline_version_generation_key", "jobs", JOB_KEY
    )
