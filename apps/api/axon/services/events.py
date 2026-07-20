"""Event ingestion + scoped verification planning.

EventService — the single door reality changes walk through:
    NormalizedEvent → dedupe (repo_id, external_id) → persist Event with
    the normalized fields copied into payload (planner never reads
    provider-specific JSON) → enqueue a VERIFY job.
    ``reingest_only`` events (belief changes: issue edits, doc-platform
    page edits) write NO Event row — they enqueue an incremental INGEST,
    whose extraction stage re-processes exactly the changed beliefs.

ScopedVerificationPlanner — the "never re-verify the whole repository"
guarantee, built on T2.3's links:
    changed paths → affected entities (exact-path code/doc rows + a doc's
    sections + the issue entity for issue events) → impacted claims =
    claims LINKED to affected entities ∪ claims SOURCED from them.
    Two indexed queries; budget-capped downstream.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from axon.db.models import (
    Claim,
    ClaimLink,
    Entity,
    EntityKind,
    Event,
    EventKind,
    Job,
    JobKind,
    Repo,
)
from axon.adapters.base import NormalizedEvent
from axon.jobs import queue

logger = logging.getLogger("axon.services.events")

_KIND_MAP = {
    "push": EventKind.PUSH,
    "pr_merged": EventKind.PR_MERGED,
    "issue_closed": EventKind.ISSUE_CLOSED,
    "simulated": EventKind.SIMULATED,
}


@dataclass(frozen=True)
class IngestOutcome:
    status: Literal["accepted", "duplicate", "reingest", "ignored"]
    event_id: str | None = None
    job_id: str | None = None


class EventService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def ingest(
        self, repo: Repo, normalized: NormalizedEvent | None
    ) -> IngestOutcome:
        if normalized is None:
            return IngestOutcome(status="ignored")

        if normalized.reingest_only:
            job = self._enqueue_ingest_once(repo)
            return IngestOutcome(
                status="reingest", job_id=str(job.id) if job else None
            )

        existing = self.db.scalars(
            select(Event).where(
                Event.repo_id == repo.id,
                Event.external_id == normalized.external_id,
            )
        ).first()
        if existing is not None:
            logger.info(
                "duplicate delivery %s for %s — ignored",
                normalized.external_id, repo.full_name,
            )
            return IngestOutcome(status="duplicate", event_id=str(existing.id))

        event = Event(
            repo_id=repo.id,
            kind=_KIND_MAP[normalized.kind],
            external_id=normalized.external_id,
            payload={
                "provider": normalized.provider,
                "action": normalized.action,
                "changed_paths": list(normalized.changed_paths),
                "pr_number": normalized.pr_number,
                "issue_number": normalized.issue_number,
                "head_sha": normalized.head_sha,
                "title": normalized.title,
            },
        )
        self.db.add(event)
        
        from sqlalchemy.exc import IntegrityError
        try:
            self.db.flush()
        except IntegrityError:
            self.db.rollback()
            existing = self.db.scalars(
                select(Event).where(
                    Event.repo_id == repo.id,
                    Event.external_id == normalized.external_id,
                )
            ).first()
            logger.info(
                "concurrent duplicate delivery %s for %s — ignored",
                normalized.external_id, repo.full_name,
            )
            return IngestOutcome(status="duplicate", event_id=str(existing.id) if existing else None)

        job = queue.enqueue(
            self.db, JobKind.VERIFY, {"event_id": str(event.id)}
        )
        return IngestOutcome(
            status="accepted", event_id=str(event.id), job_id=str(job.id)
        )

    def _enqueue_ingest_once(self, repo: Repo) -> Job | None:
        """One pending ingest per repo is enough — dedupe belief refreshes."""
        from axon.db.models import JobStatus  # noqa: PLC0415

        pending = self.db.scalars(
            select(Job).where(
                Job.kind == JobKind.INGEST,
                Job.status == JobStatus.PENDING,
                Job.payload["repo_id"].astext == str(repo.id),
            )
        ).first()
        if pending is not None:
            return pending
        return queue.enqueue(self.db, JobKind.INGEST, {"repo_id": str(repo.id)})


# --- Scoped verification planning ----------------------------------------


@dataclass
class ScopePlan:
    affected_entity_ids: list = field(default_factory=list)
    impacted_claim_ids: list = field(default_factory=list)
    changed_paths: list[str] = field(default_factory=list)
    reingest_needed: bool = False

    def summary(self) -> str:
        return (
            f"changed_paths={len(self.changed_paths)} "
            f"affected_entities={len(self.affected_entity_ids)} "
            f"impacted_claims={len(self.impacted_claim_ids)} "
            f"reingest={self.reingest_needed}"
        )


class ScopedVerificationPlanner:
    """Event → the minimal set of claims whose truth the event could have
    changed. Provider-agnostic: reads only Event.payload's normalized
    fields."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def plan(self, repo: Repo, event: Event, changed_paths: list[str]) -> ScopePlan:
        plan = ScopePlan(changed_paths=sorted(changed_paths))
        issue_number = (event.payload or {}).get("issue_number")

        conditions = []
        if changed_paths:
            path_conditions = [Entity.path.in_(changed_paths)]
            for path in changed_paths:
                path_conditions.append(Entity.path.like(f"{path}#%"))
            conditions.append(or_(*path_conditions))
        if issue_number is not None:
            conditions.append(
                (Entity.kind == EntityKind.ISSUE)
                & (Entity.external_id == str(issue_number))
            )
        if not conditions:
            return plan

        affected = self.db.scalars(
            select(Entity).where(Entity.repo_id == repo.id, or_(*conditions))
        ).all()
        plan.affected_entity_ids = [e.id for e in affected]
        # Belief text may itself have changed (docs/sections/issues) —
        # re-ingest so extraction refreshes those claims before verifying.
        plan.reingest_needed = any(
            e.kind in (EntityKind.DOC, EntityKind.DOC_SECTION, EntityKind.ISSUE)
            for e in affected
        ) or any(path.lower().endswith((".md", ".mdx", ".markdown"))
                 for path in changed_paths)

        if not plan.affected_entity_ids:
            return plan

        linked = self.db.scalars(
            select(ClaimLink.claim_id).where(
                ClaimLink.entity_id.in_(plan.affected_entity_ids)
            )
        ).all()
        sourced = self.db.scalars(
            select(Claim.id).where(
                Claim.repo_id == repo.id,
                Claim.source_entity_id.in_(plan.affected_entity_ids),
            )
        ).all()
        plan.impacted_claim_ids = sorted(set(linked) | set(sourced))
        return plan


def mark_processed(db: Session, event: Event) -> None:
    event.processed_at = datetime.now(timezone.utc)
    db.commit()
