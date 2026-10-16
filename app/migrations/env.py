from alembic import context

from app.models import Base

engine = context.config.attributes["engine"]

with engine.connect() as connection:
    # Batch mode lets ALTER TABLE-style changes work on SQLite.
    context.configure(connection=connection, target_metadata=Base.metadata,
                      render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
