"""Session-cookie unit tests (no network, no DB).

Covers the security-critical bits of Sign in with GitHub: the signed session
token round-trips, tampered/foreign tokens are rejected, and a missing secret
fails closed (verify returns None rather than trusting anything).
"""

import uuid

import pytest

from axon.api import auth
from axon.config import get_settings
from axon.db.models import User


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _user() -> User:
    return User(id=uuid.uuid4(), github_id=42, login="octocat")


def test_session_round_trip(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "x" * 40)
    get_settings.cache_clear()
    user = _user()
    token = auth.create_session_token(user)
    claims = auth._verify(token)
    assert claims is not None
    assert claims["sub"] == str(user.id)


def test_tampered_token_rejected(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "x" * 40)
    get_settings.cache_clear()
    token = auth.create_session_token(_user())
    assert auth._verify(token + "tamper") is None
    assert auth._verify("not-a-jwt") is None


def test_token_from_other_secret_rejected(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "secret-A-secret-A-secret-A-secret-A")
    get_settings.cache_clear()
    token = auth.create_session_token(_user())
    # Rotate the secret: the old token must no longer verify.
    monkeypatch.setenv("SESSION_SECRET", "secret-B-secret-B-secret-B-secret-B")
    get_settings.cache_clear()
    assert auth._verify(token) is None


def test_no_secret_fails_closed(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "x" * 40)
    get_settings.cache_clear()
    token = auth.create_session_token(_user())
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    get_settings.cache_clear()
    assert auth._verify(token) is None


def test_oauth_configured_flag(monkeypatch):
    for var in ("GITHUB_OAUTH_CLIENT_ID", "GITHUB_OAUTH_CLIENT_SECRET", "SESSION_SECRET"):
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    assert get_settings().github_oauth_configured is False

    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_SECRET", "csec")
    monkeypatch.setenv("SESSION_SECRET", "x" * 40)
    get_settings.cache_clear()
    assert get_settings().github_oauth_configured is True
