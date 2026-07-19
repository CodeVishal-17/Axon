"""Ingest job handler — ingestion, then claim extraction.

Payload: {"repo_id": "<uuid>"}. The GitHub token comes from repo.settings
["token"] (set by the connect flow, T1.4) or falls back to the global
GITHUB_TOKEN — it is deliberately NOT carried in the job payload, so the
jobs table never stores credentials.

Claim extraction runs only when LLM credentials are configured; a keyless
environment still gets a full knowledge graph, just no beliefs yet.
Extraction is incremental (unchanged sections are skipped), so running it
on every ingest is cheap after the first pass.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from axon.adapters.github.adapter import GitHubAdapter
from axon.db.models import Repo
from axon.services.claims import ClaimExtractionService, llm_configured
from axon.services.ingestion import IngestionService
from axon.services.linking import EntityLinker

logger = logging.getLogger("axon.jobs.ingest")


def run(db: Session, payload: dict[str, Any]) -> None:
    repo_id = uuid.UUID(payload["repo_id"])
    repo = db.get(Repo, repo_id)
    if repo is None:
        raise ValueError(f"repo {repo_id} not found")

    adapter = GitHubAdapter(repo.full_name, token=repo.settings.get("token"))
    IngestionService(db, adapter).run(repo)

    if llm_configured():
        ClaimExtractionService(db).run(repo)
    else:
        logger.warning(
            "skipping claim extraction for %s: no LLM API key configured "
            "(set OPENAI_API_KEY — plus ANTHROPIC_API_KEY when LLM_PROVIDER=anthropic)",
            repo.full_name,
        )

    # Linking always runs: the path/symbol tiers are free and cover most
    # claims; embedding/LLM tiers self-disable without credentials.
    EntityLinker(db).run(repo)
