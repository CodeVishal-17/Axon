#!/bin/bash
set -e

if [ "${RUN_MIGRATIONS}" = "true" ]; then
    echo "Ensuring base tables exist..."
    python -c "
from sqlalchemy import inspect
from axon.db.session import get_engine
from axon.db.base import Base

engine = get_engine()
inspector = inspect(engine)
if not inspector.has_table('events'):
    print('Fresh database detected. Initializing schema...')
    Base.metadata.create_all(engine)
    import os
    if os.system('alembic stamp head') != 0:
        exit(1)
else:
    print('Existing database detected.')
"

    echo "Running database migrations..."
    alembic upgrade head
fi

echo "Starting application..."
exec "$@"

