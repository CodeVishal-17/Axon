"""VERIFY job handler — the scoped, event-driven verification pass.

Payload: {"event_id": "<uuid>"}. Steps:

1. Plan the scope (changed paths from the event; merged PRs fetch their
   file list here — worker time, never the webhook request path).
2. If beliefs may have changed (docs/issues affected): incremental
   re-ingest + re-extraction + re-linking. Every one of those services
   skips unchanged content by hash/fingerprint, so this is cheap and the
   whole handler stays idempotent.
3. Re-plan (re-extraction may have replaced claim rows), then verify ONLY
   the impacted claims, with the event attached as finding provenance.
4. Stamp event.processed_at.

The at-rest full pass never runs here — "never re-verify the whole
repository if the change scope is known" is enforced by construction:
verification receives an explicit claim_ids list, always.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from axon.adapters.github.adapter import GitHubAdapter
from axon.adapters.github.app_auth import token_for_repo
from axon.config import get_settings
from axon.db.models import Event, Repo
from axon.services.claims import ClaimExtractionService, llm_configured
from axon.services.events import ScopedVerificationPlanner, mark_processed
from axon.services.ingestion import IngestionService
from axon.services.linking import EntityLinker
from axon.services.remediation import RemediationService
from axon.services.verification import DriftVerifier

logger = logging.getLogger("axon.jobs.verify")


def run(db: Session, payload: dict[str, Any]) -> None:
    event = db.get(Event, uuid.UUID(payload["event_id"]))
    if event is None:
        raise ValueError(f"event {payload['event_id']} not found")
    repo = db.get(Repo, event.repo_id)
    if repo is None:
        raise ValueError(f"repo {event.repo_id} not found")

    adapter = GitHubAdapter(repo.full_name, token=token_for_repo(repo))
    changed_paths = list((event.payload or {}).get("changed_paths") or [])

    # Merged-PR payloads carry no file list — resolve it now and persist
    # into the event payload so re-runs skip the API call (idempotency).
    pr_number = (event.payload or {}).get("pr_number")
    if pr_number is not None and not changed_paths:
        changed_paths = list(adapter.fetch_pr_files(pr_number))
        event.payload = {**event.payload, "changed_paths": changed_paths}
        db.commit()

    planner = ScopedVerificationPlanner(db)
    plan = planner.plan(repo, event, changed_paths)
    logger.info("event %s scope: %s", event.id, plan.summary())

    if plan.reingest_needed:
        IngestionService(db, adapter).run(repo)
        if llm_configured():
            ClaimExtractionService(db).run(repo)
        EntityLinker(db).run(repo)
        # claim rows may have been replaced — recompute the scope
        plan = planner.plan(repo, event, changed_paths)
        logger.info("event %s post-reingest scope: %s", event.id, plan.summary())

    if plan.impacted_claim_ids and llm_configured():
        DriftVerifier(
            db, event=event, budget=get_settings().verify_event_budget
        ).run(repo, claim_ids=plan.impacted_claim_ids)
        # Act stage: contradictions found by this pass get grounded
        # remediation proposals (persisted only — no GitHub writes).
        RemediationService(db).run(repo)
    elif plan.impacted_claim_ids:
        logger.warning(
            "event %s: %d impacted claims but no LLM key configured — "
            "verification skipped", event.id, len(plan.impacted_claim_ids),
        )

    mark_processed(db, event)
