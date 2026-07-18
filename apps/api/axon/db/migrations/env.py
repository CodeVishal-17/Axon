"""Alembic environment.

Wires Alembic to Axon's own configuration and metadata:

* Database URL comes from :func:`axon.config.get_settings` (env / .env),
  never from alembic.ini — one source of truth.
* ``target_metadata`` is ``Base.metadata`` so autogenerate diffs against the
  real models (added in T0.4).
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from axon.config import get_settings
from axon.db.base import Base

# Import models so they register on Base.metadata before autogenerate runs.
# T0.4 introduces axon/db/models.py; the guard keeps env.py functional now.
try:  # noqa: SIM105
    from axon.db import models  # noqa: F401
except ImportError:
    pass

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    return get_settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live database ('offline' mode)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the configured database."""
    # NullPool: migration runs are one-shot; don't hold pooled connections.
    connectable = create_engine(_database_url(), poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
