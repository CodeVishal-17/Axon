"""Engine and session management.

Design decisions:

* **Sync SQLAlchemy** (not asyncio). The worker is a plain process loop and
  the API's DB work is lightweight; FastAPI runs sync endpoints in a thread
  pool. One execution model across API + worker beats async ceremony for a
  two-person codebase.

* **Lazy, cached construction.** The engine is built on first use, not at
  import time. Import-time engines break Alembic autogenerate, tests, and
  any tooling that imports the package without a database available.
"""

from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from axon.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Process-wide engine. ``pool_pre_ping`` transparently replaces
    connections dropped by Postgres restarts (frequent during a hackathon)."""
    settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        echo=settings.debug,
        # Fail fast when Postgres is down — without this, Windows TCP retry
        # behavior turns every connection attempt into a multi-minute hang
        # (healthz checks, test skip-guards).
        connect_args={"connect_timeout": 3},
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[Session]:
    """Shared session factory bound to the process engine.

    ``expire_on_commit=False`` lets services return ORM objects after commit
    without surprise lazy-load queries once the session is closed.
    """
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a request-scoped session.

    Usage: ``db: Session = Depends(get_db)``. The session is always closed;
    transaction control (commit/rollback) belongs to the service layer.
    """
    db = get_sessionmaker()()
    try:
        yield db
    finally:
        db.close()


def dispose_engine() -> None:
    """Close all pooled connections (called on app shutdown)."""
    if get_engine.cache_info().currsize:  # don't build an engine just to kill it
        get_engine().dispose()
