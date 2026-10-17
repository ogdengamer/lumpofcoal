from alembic import context
from sqlalchemy import text

from app.models import Base

engine = context.config.attributes["engine"]

with engine.connect() as connection:
    # Batch mode rebuilds a table to alter it on SQLite. With foreign keys on, dropping
    # the old table would cascade-delete rows that point at it (a gift's photo links),
    # so they're off while migrating and checked afterwards, as SQLite advises. The
    # pragma can't change inside a transaction, hence before and after it.
    connection.execute(text("PRAGMA foreign_keys = OFF"))
    connection.commit()
    try:
        context.configure(connection=connection, target_metadata=Base.metadata,
                          render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
            if broken := connection.execute(text("PRAGMA foreign_key_check")).all():
                raise RuntimeError(f"Migration left broken foreign keys: {broken[:5]}")
    finally:
        connection.execute(text("PRAGMA foreign_keys = ON"))
        connection.commit()
