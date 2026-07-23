"""GitHub App authentication — installation tokens for `axon-ai[bot]`.

Why this exists: a personal access token authors every PR as the token's
owner and cannot write to a customer's private repo it isn't a collaborator
on. A GitHub App is installed per account/org; Axon authenticates *as the
installation*, so PRs are attributed to the app's bot identity and scoped to
exactly the repos the customer granted.

Auth flow (server-to-server, no user OAuth):
  1. Sign a short-lived JWT (RS256) with the App's private key.
  2. Resolve the installation covering a repo via
     GET /repos/{owner}/{repo}/installation (so no callback route is needed —
     install the app and Axon discovers the id).
  3. Exchange the JWT for an installation access token
     (POST /app/installations/{id}/access_tokens), scoped + ~1h expiry.
  4. Hand that token to GitHubAdapter, unchanged downstream.

Both discovery and tokens are cached in-process; tokens honour their real
expiry with a safety margin. If the app isn't configured or isn't installed
on a repo, `token_for_repo` falls back to the per-repo PAT so existing
deployments keep working.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx
import jwt

from axon.adapters.base import AdapterError
from axon.config import get_settings

logger = logging.getLogger("axon.adapters.github.app_auth")

_API = "https://api.github.com"
# Refresh a token this many seconds before its real expiry (clock drift + the
# time a job takes to run after acquiring it).
_TOKEN_SKEW_S = 300

_lock = threading.Lock()
# installation_id -> (token, expiry_epoch_seconds)
_token_cache: dict[int, tuple[str, float]] = {}
# repo full_name -> installation_id (installations are stable; cache for the
# process lifetime, refreshed on miss).
_install_id_cache: dict[str, int] = {}


def app_configured() -> bool:
    """True when both the App ID and a readable private key are present."""
    settings = get_settings()
    return settings.github_app_id is not None and _private_key_pem() is not None


def _private_key_pem() -> str | None:
    settings = get_settings()
    path = settings.github_app_private_key_path
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                return handle.read()
        except OSError as exc:  # pragma: no cover - config error path
            logger.error("cannot read GITHUB_APP_PRIVATE_KEY_PATH=%s: %s", path, exc)
            return None
    inline = settings.github_app_private_key
    if inline:
        # Tolerate a single-line env var with literal "\n" escapes.
        return inline.replace("\\n", "\n")
    return None


def _app_jwt() -> str:
    """A GitHub App JWT: iss=App ID, backdated iat for clock skew, <=10m exp."""
    settings = get_settings()
    pem = _private_key_pem()
    if settings.github_app_id is None or pem is None:
        raise AdapterError("GitHub App is not configured (app id / private key)")
    now = int(time.time())
    return jwt.encode(
        # iss must be a string (PyJWT enforces it; GitHub accepts the App ID
        # in string form).
        {"iat": now - 60, "exp": now + 540, "iss": str(settings.github_app_id)},
        pem,
        algorithm="RS256",
    )


def _app_request(method: str, path: str, **kwargs: Any) -> httpx.Response:
    """One App-JWT-authenticated call (installation discovery / token mint)."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "axon-github-app",
        "Authorization": f"Bearer {_app_jwt()}",
    }
    with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        return client.request(method, f"{_API}{path}", headers=headers, **kwargs)


def installation_id_for_repo(full_name: str) -> int | None:
    """The installation id covering ``owner/repo``, or None if the app isn't
    installed there. Cached per process."""
    cached = _install_id_cache.get(full_name)
    if cached is not None:
        return cached
    response = _app_request("GET", f"/repos/{full_name}/installation")
    if response.status_code == 404:
        return None  # app not installed on this repo
    if response.is_error:
        raise AdapterError(
            f"GitHub App installation lookup failed for {full_name}: "
            f"{response.status_code} {response.text[:200]}"
        )
    install_id = int(response.json()["id"])
    _install_id_cache[full_name] = install_id
    return install_id


def installation_token(installation_id: int) -> str:
    """A cached installation access token, minted on demand and reused until
    shortly before its real expiry."""
    now = time.time()
    with _lock:
        cached = _token_cache.get(installation_id)
        if cached and cached[1] - _TOKEN_SKEW_S > now:
            return cached[0]

    response = _app_request(
        "POST", f"/app/installations/{installation_id}/access_tokens"
    )
    if response.is_error:
        raise AdapterError(
            f"minting installation token for {installation_id} failed: "
            f"{response.status_code} {response.text[:200]}"
        )
    body = response.json()
    token = body["token"]
    # expires_at is ISO-8601 (…Z); parse to epoch for the skew comparison.
    expiry = _parse_iso_epoch(body.get("expires_at"), default=now + 3600)
    with _lock:
        _token_cache[installation_id] = (token, expiry)
    return token


def _parse_iso_epoch(value: str | None, default: float) -> float:
    if not value:
        return default
    from datetime import datetime

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:  # pragma: no cover - defensive
        return default


def token_for_repo(repo: Any) -> str | None:
    """Resolve the token GitHubAdapter should use for ``repo``.

    Preference: GitHub App installation token (bot identity) → per-repo PAT in
    ``repo.settings`` → None (adapter then falls back to the global
    ``GITHUB_TOKEN``). A stored ``installation_id`` in settings wins over
    discovery; discovery covers repos connected before the app existed.
    """
    if app_configured():
        install_id = repo.settings.get("installation_id")
        try:
            if install_id is None:
                install_id = installation_id_for_repo(repo.full_name)
            if install_id is not None:
                return installation_token(int(install_id))
        except AdapterError as exc:
            # Never let an App hiccup block a fix that a PAT could still open.
            logger.warning(
                "GitHub App auth unavailable for %s (%s) — falling back to PAT",
                repo.full_name, exc,
            )
    return repo.settings.get("token")
