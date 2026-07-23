"""GitHub App auth unit tests (no network, no DB).

Covers the parts that decide *who authors a PR*: the PAT fallback when the
app isn't configured, and that a configured app mints a valid RS256 JWT.
"""

import types

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from axon.adapters.github import app_auth
from axon.config import get_settings


@pytest.fixture
def rsa_pem() -> tuple[str, rsa.RSAPrivateKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,  # GitHub's "RSA PRIVATE KEY"
        serialization.NoEncryption(),
    ).decode()
    return pem, key


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_unconfigured_falls_back_to_repo_pat(monkeypatch):
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY_PATH", raising=False)
    get_settings.cache_clear()

    assert app_auth.app_configured() is False
    repo = types.SimpleNamespace(full_name="acme/widgets", settings={"token": "pat-123"})
    assert app_auth.token_for_repo(repo) == "pat-123"
    # No PAT and no app -> None, so the adapter falls back to the global token.
    bare = types.SimpleNamespace(full_name="acme/widgets", settings={})
    assert app_auth.token_for_repo(bare) is None


def test_configured_app_signs_valid_jwt(monkeypatch, rsa_pem):
    pem, key = rsa_pem
    monkeypatch.setenv("GITHUB_APP_ID", "4371782")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", pem)
    get_settings.cache_clear()

    assert app_auth.app_configured() is True
    decoded = jwt.decode(app_auth._app_jwt(), key.public_key(), algorithms=["RS256"])
    assert decoded["iss"] == "4371782"  # string, per PyJWT/GitHub
    assert 0 < decoded["exp"] - decoded["iat"] <= 600


def test_inline_key_tolerates_literal_newlines(monkeypatch, rsa_pem):
    pem, _ = rsa_pem
    monkeypatch.setenv("GITHUB_APP_ID", "4371782")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", pem.replace("\n", "\\n"))
    get_settings.cache_clear()
    assert app_auth._private_key_pem() == pem
