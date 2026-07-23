"""Dashboard — a per-user rollup of everything Axon has done.

Pure aggregation over existing tables: the history already lives in
``findings`` and ``fixes`` (status, timestamps, pr_url, error). This endpoint
scopes those to the signed-in user's repos and shapes them for the dashboard —
totals, per-repo breakdown, and a recent-activity stream derived from fix state
transitions. No new data capture.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from axon.api.auth import current_user
from axon.db.models import (
    Claim,
    Finding,
    FindingStatus,
    Fix,
    FixStatus,
    Repo,
    User,
)
from axon.db.session import get_db

router = APIRouter(prefix="/api", tags=["dashboard"])


# --- Schemas -------------------------------------------------------------


class Totals(BaseModel):
    findings_total: int = 0
    findings_open: int = 0
    findings_actioned: int = 0
    findings_dismissed: int = 0
    fixes_proposed: int = 0  # generated + pending (awaiting/queued)
    prs_opened: int = 0  # fixes that reached PR_OPENED
    fixes_blocked: int = 0  # FAILED (rejected by a safety gate)


class RepoSummary(BaseModel):
    id: uuid.UUID
    full_name: str
    ingest_status: str
    findings_open: int = 0
    findings_actioned: int = 0
    findings_dismissed: int = 0
    prs_opened: int = 0


class ActivityItem(BaseModel):
    kind: str  # "pr_opened" | "blocked" | "proposed"
    finding_id: uuid.UUID
    repo_full_name: str
    title: str
    pr_url: str | None = None
    reason: str | None = None
    at: str  # ISO timestamp of the fix's last transition


class DashboardOut(BaseModel):
    totals: Totals
    repos: list[RepoSummary]
    recent_activity: list[ActivityItem]


# --- Endpoint ------------------------------------------------------------


_ACTIVITY_LIMIT = 20


@router.get("/dashboard", response_model=DashboardOut)
def get_dashboard(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> DashboardOut:
    repos = db.scalars(
        select(Repo).where(Repo.owner_id == user.id).order_by(Repo.created_at)
    ).all()
    if not repos:
        return DashboardOut(totals=Totals(), repos=[], recent_activity=[])

    repo_ids = [r.id for r in repos]
    summaries = {
        r.id: RepoSummary(
            id=r.id, full_name=r.full_name, ingest_status=r.ingest_status.value
        )
        for r in repos
    }
    totals = Totals()

    # Findings by (repo, status) — one grouped scan.
    finding_rows = db.execute(
        select(Finding.repo_id, Finding.status, func.count())
        .where(Finding.repo_id.in_(repo_ids))
        .group_by(Finding.repo_id, Finding.status)
    ).all()
    for repo_id, status, count in finding_rows:
        summary = summaries[repo_id]
        totals.findings_total += count
        if status == FindingStatus.OPEN:
            summary.findings_open = count
            totals.findings_open += count
        elif status == FindingStatus.ACTIONED:
            summary.findings_actioned = count
            totals.findings_actioned += count
        elif status == FindingStatus.DISMISSED:
            summary.findings_dismissed = count
            totals.findings_dismissed += count

    # Fixes by (repo, status) — join through findings to scope by repo.
    fix_rows = db.execute(
        select(Finding.repo_id, Fix.status, func.count())
        .join(Finding, Fix.finding_id == Finding.id)
        .where(Finding.repo_id.in_(repo_ids))
        .group_by(Finding.repo_id, Fix.status)
    ).all()
    for repo_id, status, count in fix_rows:
        summary = summaries[repo_id]
        if status == FixStatus.PR_OPENED:
            summary.prs_opened = count
            totals.prs_opened += count
        elif status == FixStatus.FAILED:
            totals.fixes_blocked += count
        elif status in (FixStatus.GENERATED, FixStatus.PENDING):
            totals.fixes_proposed += count

    # Recent activity: latest fixes as state-transition events.
    fixes = db.scalars(
        select(Fix)
        .join(Finding, Fix.finding_id == Finding.id)
        .where(Finding.repo_id.in_(repo_ids))
        .options(
            joinedload(Fix.finding).joinedload(Finding.claim),
            joinedload(Fix.finding).joinedload(Finding.repo),
        )
        .order_by(Fix.updated_at.desc())
        .limit(_ACTIVITY_LIMIT)
    ).all()

    activity: list[ActivityItem] = []
    for fix in fixes:
        finding = fix.finding
        claim: Claim | None = finding.claim if finding else None
        title = claim.statement if claim else "(finding)"
        repo_full_name = finding.repo.full_name if finding and finding.repo else "?"
        if fix.status == FixStatus.PR_OPENED:
            kind, pr_url, reason = "pr_opened", fix.pr_url, None
        elif fix.status == FixStatus.FAILED:
            kind, pr_url, reason = "blocked", None, fix.error
        else:  # generated / pending
            kind, pr_url, reason = "proposed", None, None
        activity.append(
            ActivityItem(
                kind=kind,
                finding_id=finding.id,
                repo_full_name=repo_full_name,
                title=title,
                pr_url=pr_url,
                reason=reason,
                at=fix.updated_at.isoformat(),
            )
        )

    return DashboardOut(
        totals=totals, repos=list(summaries.values()), recent_activity=activity
    )
