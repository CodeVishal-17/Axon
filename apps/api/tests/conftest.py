"""Shared test configuration.

Postgres availability is decided ONCE here, not re-implemented in every test
module (it used to be copy-pasted into fourteen of them).

Two layers, deliberately:

* ``requires_db`` — the explicit marker, for tests that talk to the database
  without requesting a DB-backed fixture.
* ``pytest_collection_modifyitems`` — a safety net that skips any test whose
  fixtures touch the database, whether or not someone remembered the marker.
  Forgetting it used to turn "skipped, no Postgres" into a hard collection
  ERROR; now it cannot.
"""

from __future__ import annotations

import functools

import pytest
from sqlalchemy import text

from axon.config import get_settings
from axon.db.session import get_engine

# Fixtures that open a database connection (directly or transitively). A test
# requesting any of these needs Postgres.
DB_FIXTURES = frozenset({"db", "client", "app", "seeded", "repo"})

_SKIP_REASON = "Postgres not reachable — start it with `docker compose up -d db`"


@functools.lru_cache(maxsize=1)
def db_available() -> bool:
    """True when Postgres answers. Cached: one probe per test session."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(not db_available(), reason=_SKIP_REASON)


def pytest_collection_modifyitems(config, items) -> None:
    """Skip DB-backed tests when Postgres is down, marker or not."""
    if db_available():
        return
    skip = pytest.mark.skip(reason=_SKIP_REASON)
    for item in items:
        if DB_FIXTURES & set(getattr(item, "fixturenames", ())):
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _isolate_settings_cache():
    """``get_settings`` is process-wide and lru_cached. A test that patches the
    environment (keyless mode, OAuth config) would otherwise leak its settings
    into every test that ran after it. Clearing on the way out keeps tests
    order-independent."""
    yield
    get_settings.cache_clear()
