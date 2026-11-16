from alembic import context

from kanzlei_ai.models import metadata

context.configure(connection=context.config.attributes["connection"], target_metadata=metadata)
with context.begin_transaction():
    context.run_migrations()
