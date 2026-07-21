"""Findings endpoints — the Truth Feed's data source and action surface.

Added with T1.5 as the schema contract the frontend builds against
(fixtures must match these generated types exactly); the drift engine
(T2.4/T3.2) writes rows into it, T2.5 swaps the frontend from fixtures to
this endpoint. The evidence shape defined here is likewise the contract
the verifier's output must conform to.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

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
    Fix,
    FixStatus,
    JobKind,
    Repo,
)
from axon.db.session import get_db
from axon.jobs import queue

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
    pr_url: str | None = None


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
        pr_url=finding.fix.pr_url if finding.fix else None,
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
        .options(joinedload(Finding.claim), joinedload(Finding.event), joinedload(Finding.fix))
        .where(*conditions)
        .order_by(Finding.created_at.desc(), Finding.id)
        .limit(limit)
        .offset(offset)
    ).all()

    return FindingPage(
        total=total or 0, limit=limit, offset=offset,
        items=[_to_out(f) for f in rows],
    )


# --- Actions ---------------------------------------------------------------


class FindingActionRequest(BaseModel):
    action: Literal["generate_fix", "dismiss"]


class FindingActionResponse(BaseModel):
    status: str  # queued | dismissed | already_open
    finding_status: FindingStatus
    job_id: str | None = None
    pr_url: str | None = None


@router.post("/findings/{finding_id}/action", response_model=FindingActionResponse)
def finding_action(
    finding_id: uuid.UUID,
    body: FindingActionRequest,
    db: Session = Depends(get_db),
) -> FindingActionResponse:
    """Human-in-the-loop actions on a finding (architecture §12: writes to
    customer repos happen only behind an explicit click)."""
    finding = db.get(Finding, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="finding not found")

    if body.action == "dismiss":
        finding.status = FindingStatus.DISMISSED
        db.commit()
        return FindingActionResponse(
            status="dismissed", finding_status=finding.status
        )

    # generate_fix
    # Lock the fix row to prevent concurrent workers from enqueueing duplicate jobs
    fix = db.scalar(select(Fix).where(Fix.finding_id == finding_id).with_for_update())
    if fix is None:
        raise HTTPException(
            status_code=409,
            detail="no remediation proposal exists for this finding yet",
        )
    if fix.status == FixStatus.PR_OPENED:
        db.rollback()
        return FindingActionResponse(
            status="already_open", finding_status=finding.status, pr_url=fix.pr_url
        )
    if fix.status != FixStatus.GENERATED:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"remediation is not actionable (status: {fix.status.value})",
        )
    
    # Commit the PENDING state and the job enqueue atomically
    fix.status = FixStatus.PENDING
    job = queue.enqueue(db, JobKind.GENERATE_FIX, {"fix_id": str(fix.id)})
    
    return FindingActionResponse(
        status="queued", finding_status=finding.status, job_id=str(job.id)
    )
