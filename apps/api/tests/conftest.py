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


def authenticate(app, user) -> None:
    """Make a TestClient built on ``app`` act as ``user``.

    Repo-scoped endpoints require a session, so endpoint tests must say who
    they are. Overriding the dependencies is preferred over minting a real
    cookie: it keeps the tests about the endpoint under test rather than
    about OAuth.
    """
    from axon.api.auth import current_user, optional_user  # noqa: PLC0415

    app.dependency_overrides[current_user] = lambda: user
    app.dependency_overrides[optional_user] = lambda: user


def get_or_create_user(session, login: str, github_id: int):
    """A stable test user, reused across runs in the same database."""
    from sqlalchemy import select  # noqa: PLC0415

    from axon.db.models import User  # noqa: PLC0415

    user = session.scalar(select(User).where(User.login == login))
    if user is None:
        user = User(github_id=github_id, login=login)
        session.add(user)
        session.commit()
    return user


@pytest.fixture()
def authed_app(monkeypatch):
    """An app whose requests are authenticated as a stable test user.

    Repo-scoped endpoints require a session, so endpoint tests need an
    identity. Rate limiting is off here: these tests exercise endpoints, not
    the limiter (which has its own tests in test_security.py).
    """
    from sqlalchemy.orm import Session  # noqa: PLC0415

    from axon.api.auth import current_user, optional_user  # noqa: PLC0415
    from axon.db import Base  # noqa: PLC0415
    from axon.db.models import User  # noqa: PLC0415
    from axon.main import create_app  # noqa: PLC0415

    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    get_settings.cache_clear()

    Base.metadata.create_all(get_engine())
    with Session(get_engine(), expire_on_commit=False) as setup:
        user_id = get_or_create_user(setup, "axon-test-shared", 900_000).id

    def _user():
        with Session(get_engine(), expire_on_commit=False) as session:
            return session.get(User, user_id)

    app = create_app()
    app.dependency_overrides[current_user] = _user
    app.dependency_overrides[optional_user] = _user
    yield app
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _isolate_settings_cache():
    """``get_settings`` is process-wide and lru_cached. A test that patches the
    environment (keyless mode, OAuth config) would otherwise leak its settings
    into every test that ran after it. Clearing on the way out keeps tests
    order-independent."""
    yield
    get_settings.cache_clear()
