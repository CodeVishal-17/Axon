"""GitHub-facing helpers for the signed-in user — repo discovery.

Powers the multi-repo connect picker: lists the repositories the user can act
on through the Axon GitHub App (i.e. where the app is installed), marking which
are already connected, plus the install URL so they can grant more. Uses the
user's stored OAuth token to enumerate *their* installations of our app; the
app never sees repos the user hasn't granted.
"""

from __future__ import annotations

import uuid

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from axon.adapters.github.app_auth import app_slug
from axon.api.auth import current_user
from axon.db.models import Repo, User
from axon.db.session import get_db

router = APIRouter(prefix="/api/github", tags=["github"])

_API = "https://api.github.com"
_MAX_PAGES = 5  # cap: 500 repos per installation is plenty for a picker


class AvailableRepo(BaseModel):
    full_name: str
    private: bool
    description: str | None = None
    connected: bool = False
    repo_id: uuid.UUID | None = None


class AvailableReposOut(BaseModel):
    repos: list[AvailableRepo]
    install_url: str | None = None


def _install_url() -> str | None:
    slug = app_slug()
    return f"https://github.com/apps/{slug}/installations/new" if slug else None


@router.get("/available-repos", response_model=AvailableReposOut)
def available_repos(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> AvailableReposOut:
    install_url = _install_url()
    if not user.access_token:
        return AvailableReposOut(repos=[], install_url=install_url)

    headers = {
        "Authorization": f"Bearer {user.access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "axon-connect",
    }
    raw: list[dict] = []
    with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        installs = client.get(f"{_API}/user/installations", headers=headers)
        if installs.is_error:
            return AvailableReposOut(repos=[], install_url=install_url)
        for installation in installs.json().get("installations", []):
            iid = installation["id"]
            page = 1
            while page <= _MAX_PAGES:
                resp = client.get(
                    f"{_API}/user/installations/{iid}/repositories",
                    headers=headers,
                    params={"per_page": 100, "page": page},
                )
                if resp.is_error:
                    break
                batch = resp.json().get("repositories", [])
                raw.extend(batch)
                if len(batch) < 100:
                    break
                page += 1

    # Which of the user's repos are already connected (owned by them).
    owned = db.scalars(select(Repo).where(Repo.owner_id == user.id)).all()
    owned_by_name = {r.full_name.lower(): r.id for r in owned}

    seen: set[str] = set()
    repos: list[AvailableRepo] = []
    for item in raw:
        full_name = item.get("full_name")
        if not full_name or full_name.lower() in seen:
            continue
        seen.add(full_name.lower())
        repos.append(
            AvailableRepo(
                full_name=full_name,
                private=bool(item.get("private")),
                description=item.get("description"),
                connected=full_name.lower() in owned_by_name,
                repo_id=owned_by_name.get(full_name.lower()),
            )
        )
    repos.sort(key=lambda r: r.full_name.lower())
    return AvailableReposOut(repos=repos, install_url=install_url)
