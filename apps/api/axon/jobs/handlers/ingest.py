"""Ingest job handler — runs IngestionService for one repository.

Payload: {"repo_id": "<uuid>"}. The GitHub token comes from repo.settings
["token"] (set by the connect flow, T1.4) or falls back to the global
GITHUB_TOKEN — it is deliberately NOT carried in the job payload, so the
jobs table never stores credentials.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from axon.adapters.github.adapter import GitHubAdapter
from axon.db.models import Repo
from axon.services.ingestion import IngestionService


def run(db: Session, payload: dict[str, Any]) -> None:
    repo_id = uuid.UUID(payload["repo_id"])
    repo = db.get(Repo, repo_id)
    if repo is None:
        raise ValueError(f"repo {repo_id} not found")

    adapter = GitHubAdapter(repo.full_name, token=repo.settings.get("token"))
    IngestionService(db, adapter).run(repo)
