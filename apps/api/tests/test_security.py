"""Security guarantees, pinned as tests.

Each test here corresponds to a hole that existed and was closed. They are
cheap to run and expensive to lose, because every one of them protects
something a reviewer cannot see by reading a single file:

* no endpoint that exposes repository data is reachable anonymously
* credentials never appear in a response body
* verification fails CLOSED in production when its secret is unset
* the interactive docs are withheld in production
* floods are rejected
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from axon.api.auth import current_user, optional_user
from axon.config import get_settings
from axon.db import Base, models
from axon.db.session import get_engine
from axon.main import create_app
from tests.conftest import requires_db

SECRET_TOKEN = "ghp_supersecret_should_never_appear"
SECRET_OAUTH = "gho_user_token_should_never_appear"


@pytest.fixture()
def db():
    Base.metadata.create_all(get_engine())
    with Session(get_engine(), expire_on_commit=False) as session:
        yield session
        session.rollback()
        for repo in session.scalars(
            select(models.Repo).where(models.Repo.full_name.like("axon-test/%"))
        ):
            session.delete(repo)
        for user in session.scalars(
            select(models.User).where(models.User.login.like("axon-test-%"))
        ):
            session.delete(user)
        session.commit()


@pytest.fixture()
def app(monkeypatch: pytest.MonkeyPatch):
    # Rate limiting off by default here: these tests make many calls and are
    # not what the limiter is for. test_rate_limit_* enable it explicitly.
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    get_settings.cache_clear()
    Base.metadata.create_all(get_engine())
    return create_app()


# --- Anonymous access -----------------------------------------------------


@requires_db
def test_repo_endpoints_reject_anonymous_callers(db: Session, app) -> None:
    """Every repo-scoped endpoint used to serve any repo whose owner_id was
    NULL to anyone at all — including the action endpoints, which open pull
    requests on a real repository."""
    repo = models.Repo(
        full_name=f"axon-test/anon-{uuid.uuid4().hex[:8]}",
        owner_id=None,  # the pre-auth shape that used to be world-readable
        ingest_status=models.IngestStatus.READY,
    )
    db.add(repo)
    db.commit()

    with TestClient(app) as client:
        for method, path in [
            ("GET", f"/api/repos/{repo.id}"),
            ("GET", f"/api/repos/{repo.id}/entities"),
            ("GET", f"/api/repos/{repo.id}/findings"),
            ("GET", f"/api/repos/{repo.id}/pulls"),
            ("POST", f"/api/repos/{repo.id}/pulls/1/review"),
            ("POST", f"/api/findings/{uuid.uuid4()}/action"),
            ("GET", "/api/dashboard"),
            ("GET", "/api/github/available-repos"),
        ]:
            response = client.request(method, path, json={"action": "dismiss"})
            assert response.status_code == 401, f"{method} {path} -> {response.status_code}"


@requires_db
def test_signed_in_user_cannot_reach_another_users_repo(db: Session, app) -> None:
    owner = models.User(github_id=910_001, login="axon-test-owner")
    other = models.User(github_id=910_002, login="axon-test-other")
    db.add_all([owner, other])
    db.commit()
    repo = models.Repo(
        full_name=f"axon-test/priv-{uuid.uuid4().hex[:8]}", owner_id=owner.id
    )
    db.add(repo)
    db.commit()

    app.dependency_overrides[current_user] = lambda: other
    app.dependency_overrides[optional_user] = lambda: other
    with TestClient(app) as client:
        # 404, never 403: the response must not confirm the id exists.
        assert client.get(f"/api/repos/{repo.id}").status_code == 404
        assert client.get(f"/api/repos/{repo.id}/findings").status_code == 404
    app.dependency_overrides.clear()


# --- Credential exposure --------------------------------------------------


@requires_db
def test_no_endpoint_echoes_stored_credentials(db: Session, app) -> None:
    """The GitHub PAT and the user's OAuth token live in the database. No
    response body may contain either."""
    user = models.User(
        github_id=910_003, login="axon-test-secrets", access_token=SECRET_OAUTH
    )
    db.add(user)
    db.commit()
    repo = models.Repo(
        full_name=f"axon-test/secret-{uuid.uuid4().hex[:8]}",
        owner_id=user.id,
        settings={"token": SECRET_TOKEN},
        ingest_status=models.IngestStatus.READY,
    )
    db.add(repo)
    db.commit()

    app.dependency_overrides[current_user] = lambda: user
    app.dependency_overrides[optional_user] = lambda: user
    with TestClient(app) as client:
        bodies = [
            client.get(f"/api/repos/{repo.id}").text,
            client.get(f"/api/repos/{repo.id}/entities").text,
            client.get(f"/api/repos/{repo.id}/findings").text,
            client.get("/api/dashboard").text,
            client.get("/api/auth/me").text,
        ]
    app.dependency_overrides.clear()

    for body in bodies:
        assert SECRET_TOKEN not in body
        assert SECRET_OAUTH not in body
        assert "access_token" not in body
        assert "settings" not in body


# --- Fail-closed verification ---------------------------------------------


def test_webhook_without_configured_secret_is_refused_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unset webhook secret used to mean "accept anything", which let a
    stranger drive ingest/verify work on any repository."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    get_settings.cache_clear()

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/webhooks/github",
            json={"zen": "hi"},
            headers={"X-GitHub-Event": "push"},
        )
    assert response.status_code == 503


def test_webhook_rejects_a_bad_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    get_settings.cache_clear()

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/webhooks/github",
            json={"zen": "hi"},
            headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": "sha256=deadbeef"},
        )
    assert response.status_code == 401


# --- Attack-surface exposure ----------------------------------------------


def test_docs_are_withheld_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    get_settings.cache_clear()

    with TestClient(create_app()) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_docs_available_in_development(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    get_settings.cache_clear()

    with TestClient(create_app()) as client:
        assert client.get("/openapi.json").status_code == 200


def test_security_headers_present(app) -> None:
    with TestClient(app) as client:
        headers = client.get("/healthz").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"


def test_wildcard_cors_origin_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """`*` with credentialed requests would hand the session cookie to any
    origin. It must never reach the middleware."""
    monkeypatch.setenv("CORS_ORIGINS", "*,http://localhost:3000")
    get_settings.cache_clear()
    assert get_settings().cors_origin_list == ["http://localhost:3000"]


# --- Rate limiting --------------------------------------------------------


def test_rate_limit_blocks_credential_flooding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_SENSITIVE", "3")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_S", "60")
    get_settings.cache_clear()

    with TestClient(create_app()) as client:
        codes = [client.get("/api/auth/me").status_code for _ in range(6)]
    assert codes[:3] == [401, 401, 401]  # allowed, and correctly unauthorized
    assert codes[3:] == [429, 429, 429]  # then throttled


def test_rate_limit_never_throttles_health(monkeypatch: pytest.MonkeyPatch) -> None:
    """The platform decides the container is dead if /healthz starts failing."""
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_DEFAULT", "2")
    monkeypatch.setenv("RATE_LIMIT_SENSITIVE", "2")
    get_settings.cache_clear()

    with TestClient(create_app()) as client:
        codes = {client.get("/healthz").status_code for _ in range(10)}
    assert codes == {200}
