"""Findings endpoints — the Truth Feed's data source.

Added with T1.5 as the schema contract the frontend builds against
(fixtures must match these generated types exactly); the drift engine
(T2.4/T3.2) writes rows into it, T2.5 swaps the frontend from fixtures to
this endpoint. The evidence shape defined here is likewise the contract
the verifier's output must conform to.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from axon.db.models import (
    Claim,
    ClaimStatus,
    ClaimType,
    EventKind,
    Finding,
    FindingKind,
    FindingSeverity,
    FindingStatus,
    Repo,
)
from axon.db.session import get_db

router = APIRouter(prefix="/api", tags=["findings"])


# --- Schemas -------------------------------------------------------------


class AnchorOut(BaseModel):
    """Source location of a claim (repo-relative)."""

    model_config = ConfigDict(extra="ignore")

    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None


class EvidenceQuote(BaseModel):
    """One quoted span of evidence, optionally anchored to code."""

    model_config = ConfigDict(extra="ignore")

    text: str
    path: str | None = None
    start_line: int | None = None
    language: str | None = None


class EvidenceOut(BaseModel):
    """Structured evidence: quoted spans plus an optional unified diff."""

    model_config = ConfigDict(extra="ignore")

    quotes: list[EvidenceQuote] = []
    diff: str | None = None


class ClaimBrief(BaseModel):
    id: uuid.UUID
    statement: str
    claim_type: ClaimType
    status: ClaimStatus
    anchor: AnchorOut


class EventBrief(BaseModel):
    """Provenance: the reality event that triggered this finding."""

    id: uuid.UUID
    kind: EventKind
    external_id: str | None
    created_at: datetime


class FindingOut(BaseModel):
    id: uuid.UUID
    kind: FindingKind
    severity: FindingSeverity
    status: FindingStatus
    explanation: str
    suggested_action: str | None
    evidence: EvidenceOut
    claim: ClaimBrief
    event: EventBrief | None
    created_at: datetime


class FindingPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[FindingOut]


# --- Endpoint ------------------------------------------------------------


def _to_out(finding: Finding) -> FindingOut:
    claim: Claim = finding.claim
    return FindingOut(
        id=finding.id,
        kind=finding.kind,
        severity=finding.severity,
        status=finding.status,
        explanation=finding.explanation,
        suggested_action=finding.suggested_action,
        evidence=EvidenceOut.model_validate(finding.evidence or {}),
        claim=ClaimBrief(
            id=claim.id,
            statement=claim.statement,
            claim_type=claim.claim_type,
            status=claim.status,
            anchor=AnchorOut.model_validate(claim.anchor or {}),
        ),
        event=(
            EventBrief(
                id=finding.event.id,
                kind=finding.event.kind,
                external_id=finding.event.external_id,
                created_at=finding.event.created_at,
            )
            if finding.event
            else None
        ),
        created_at=finding.created_at,
    )


@router.get("/repos/{repo_id}/findings", response_model=FindingPage)
def list_findings(
    repo_id: uuid.UUID,
    status: FindingStatus | None = FindingStatus.OPEN,
    severity: FindingSeverity | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> FindingPage:
    """Findings for the Truth Feed, newest first. ``status`` defaults to
    open (pass explicitly to see actioned/dismissed history)."""
    if db.get(Repo, repo_id) is None:
        raise HTTPException(status_code=404, detail="repository not found")

    conditions = [Finding.repo_id == repo_id]
    if status is not None:
        conditions.append(Finding.status == status)
    if severity is not None:
        conditions.append(Finding.severity == severity)

    total = db.scalar(select(func.count()).select_from(Finding).where(*conditions))
    rows = db.scalars(
        select(Finding)
        .options(joinedload(Finding.claim), joinedload(Finding.event))
        .where(*conditions)
        .order_by(Finding.created_at.desc(), Finding.id)
        .limit(limit)
        .offset(offset)
    ).all()

    return FindingPage(
        total=total or 0, limit=limit, offset=offset,
        items=[_to_out(f) for f in rows],
    )
