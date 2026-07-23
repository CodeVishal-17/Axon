"""Authentication — Sign in with GitHub (OAuth) + session cookie.

The Axon GitHub App doubles as the OAuth provider (same Client ID). The whole
flow is server-side so the client secret never reaches the browser:

    /api/auth/github/login     -> 302 to GitHub's authorize screen (signed
                                  `state` for CSRF; no server-side storage)
    /api/auth/github/callback  -> exchange code -> fetch GitHub identity ->
                                  upsert User -> set signed session cookie ->
                                  302 back to the web app
    /api/auth/me               -> the current user (safe fields) or 401
    /api/auth/logout           -> clear the cookie

The session is a stateless HS256 JWT in an httpOnly cookie (SameSite=Lax works
across the same-host :3000/:8000 ports). `current_user` / `optional_user` are
the dependencies other routers use to gate and scope requests.
"""

from __future__ import annotations

import logging
import secrets
import time
import uuid

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from axon.config import get_settings
from axon.db.models import Repo, User
from axon.db.session import get_db

logger = logging.getLogger("axon.api.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])

SESSION_COOKIE = "axon_session"
_GITHUB_AUTHORIZE = "https://github.com/login/oauth/authorize"
_GITHUB_TOKEN = "https://github.com/login/oauth/access_token"
_GITHUB_API = "https://api.github.com"


# --- Schemas -------------------------------------------------------------


class UserOut(BaseModel):
    """Safe user fields — never includes access_token."""

    id: uuid.UUID
    github_id: int
    login: str
    name: str | None
    avatar_url: str | None
    email: str | None


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        github_id=user.github_id,
        login=user.login,
        name=user.name,
        avatar_url=user.avatar_url,
        email=user.email,
    )


# --- Session cookie ------------------------------------------------------


def _sign(payload: dict, ttl_seconds: int) -> str:
    settings = get_settings()
    now = int(time.time())
    return jwt.encode(
        {**payload, "iat": now, "exp": now + ttl_seconds},
        settings.session_secret,
        algorithm="HS256",
    )


def _verify(token: str) -> dict | None:
    settings = get_settings()
    if not settings.session_secret:
        return None
    try:
        return jwt.decode(token, settings.session_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def create_session_token(user: User) -> str:
    return _sign({"sub": str(user.id)}, get_settings().session_ttl_hours * 3600)


def _user_from_request(request: Request, db: Session) -> User | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    claims = _verify(token)
    if not claims or "sub" not in claims:
        return None
    try:
        user_id = uuid.UUID(claims["sub"])
    except (ValueError, TypeError):
        return None
    return db.get(User, user_id)


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Require a signed-in user; 401 otherwise."""
    user = _user_from_request(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


def optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """The signed-in user, or None — for endpoints that scope by owner but
    still serve legacy (null-owner) repos."""
    return _user_from_request(request, db)


def authorize_repo(repo: Repo, user: User | None) -> None:
    """Enforce repo ownership on reads. Legacy null-owner repos stay accessible
    to anyone (demo continuity); an owned repo is private to its owner. Raises
    404 (not 403) on mismatch so we don't leak that the repo exists."""
    if repo.owner_id is not None and (user is None or repo.owner_id != user.id):
        raise HTTPException(status_code=404, detail="repository not found")


# --- OAuth endpoints -----------------------------------------------------


def _redirect_uri(request: Request) -> str:
    # Absolute URL of our callback, derived from the incoming request host so
    # it matches whatever origin the user reached us on (must equal a callback
    # URL registered on the GitHub App).
    return str(request.url_for("github_callback"))


@router.get("/github/login")
def github_login(request: Request) -> RedirectResponse:
    settings = get_settings()
    if not settings.github_oauth_configured:
        raise HTTPException(status_code=503, detail="GitHub sign-in is not configured")
    # Signed, short-lived state carries a nonce — verified on callback, no
    # server-side session store needed.
    state = _sign({"nonce": secrets.token_urlsafe(16), "purpose": "oauth_state"}, 600)
    query = httpx.QueryParams(
        {
            "client_id": settings.github_oauth_client_id,
            "redirect_uri": _redirect_uri(request),
            "scope": "read:user user:email",
            "state": state,
        }
    )
    return RedirectResponse(url=f"{_GITHUB_AUTHORIZE}?{query}", status_code=302)


@router.get("/github/callback", name="github_callback")
def github_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    settings = get_settings()
    if not settings.github_oauth_configured:
        raise HTTPException(status_code=503, detail="GitHub sign-in is not configured")
    claims = _verify(state or "")
    if not code or not claims or claims.get("purpose") != "oauth_state":
        raise HTTPException(status_code=400, detail="invalid oauth state")

    token = _exchange_code(code, _redirect_uri(request))
    profile = _fetch_identity(token)
    user = _upsert_user(db, profile, token)

    response = RedirectResponse(url=f"{settings.web_base_url}/dashboard", status_code=302)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=create_session_token(user),
        httponly=True,
        samesite="lax",
        max_age=settings.session_ttl_hours * 3600,
        path="/",
    )
    return response


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)) -> UserOut:
    return _user_out(user)


@router.post("/logout")
def logout() -> JSONResponse:
    # JSON (not a redirect): the SPA clears its own client state on success.
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# --- GitHub calls --------------------------------------------------------


def _exchange_code(code: str, redirect_uri: str) -> str:
    settings = get_settings()
    with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        resp = client.post(
            _GITHUB_TOKEN,
            headers={"Accept": "application/json"},
            data={
                "client_id": settings.github_oauth_client_id,
                "client_secret": settings.github_oauth_client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
    if resp.is_error:
        logger.warning("oauth token exchange failed: %s %s", resp.status_code, resp.text[:200])
        raise HTTPException(status_code=502, detail="GitHub token exchange failed")
    body = resp.json()
    access_token = body.get("access_token")
    if not access_token:
        raise HTTPException(status_code=502, detail="GitHub did not return an access token")
    return access_token


def _fetch_identity(access_token: str) -> dict:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "axon-auth",
    }
    with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        user_resp = client.get(f"{_GITHUB_API}/user", headers=headers)
        if user_resp.is_error:
            raise HTTPException(status_code=502, detail="could not fetch GitHub profile")
        profile = user_resp.json()
        if not profile.get("email"):
            # Primary email may be private on /user; the emails endpoint has it.
            emails_resp = client.get(f"{_GITHUB_API}/user/emails", headers=headers)
            if emails_resp.status_code == 200:
                primary = next(
                    (e for e in emails_resp.json() if e.get("primary") and e.get("verified")),
                    None,
                )
                if primary:
                    profile["email"] = primary.get("email")
    return profile


def _upsert_user(db: Session, profile: dict, access_token: str) -> User:
    github_id = int(profile["id"])
    user = db.scalar(select(User).where(User.github_id == github_id))
    if user is None:
        user = User(github_id=github_id, login=profile["login"])
        db.add(user)
    user.login = profile["login"]
    user.name = profile.get("name")
    user.avatar_url = profile.get("avatar_url")
    user.email = profile.get("email")
    user.access_token = access_token
    db.commit()
    db.refresh(user)
    return user
