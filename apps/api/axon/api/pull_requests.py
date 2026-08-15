"""Pull-request review endpoints — list PRs, request a review, publish it.

Thin router (architecture §2): validate, authorize, call the service or the
queue, shape the response. Review GENERATION never happens in a request
handler — POST .../review only enqueues; the worker does the work. Publishing
to GitHub is the one synchronous action, because it is the explicit
human-in-the-loop click that must report its result immediately.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from axon.adapters.base import AdapterError
from axon.api.auth import authorize_repo, optional_user
from axon.db.models import (
    Job,
    JobKind,
    JobStatus,
    PullRequestReview,
    Repo,
    ReviewStatus,
    User,
)
from axon.db.session import get_db
from axon.jobs import queue
from axon.services.pr_review import PRReviewService

router = APIRouter(prefix="/api", tags=["pull-requests"])


# --- Schemas -------------------------------------------------------------


class ReviewCommentOut(BaseModel):
    path: str
    line: int
    lens: Literal["code", "truth"]
    severity: str
    body: str
    confidence: float | None = None


class ReviewOut(BaseModel):
    id: uuid.UUID
    pr_number: int
    head_sha: str
    pr_title: str | None
    summary: str | None
    comments: list[ReviewCommentOut]
    status: ReviewStatus
    review_url: str | None
    error: str | None
    created_at: datetime
    updated_at: datetime


class PullOut(BaseModel):
    number: int
    title: str
    author: str | None
    head_sha: str
    draft: bool
    url: str
    updated_at: datetime | None
    # Review state for THIS revision (a new push resets it to none).
    review_status: ReviewStatus | None = None
    review_url: str | None = None
    review_comment_count: int = 0
    review_pending: bool = False


class PullListOut(BaseModel):
    items: list[PullOut]


class ReviewRequestResponse(BaseModel):
    status: str  # queued | already_reviewed
    job_id: str | None = None
    review: ReviewOut | None = None


# --- Helpers -------------------------------------------------------------


def _repo_for(
    db: Session, repo_id: uuid.UUID, user: User | None
) -> Repo:
    repo = db.get(Repo, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="repository not found")
    authorize_repo(repo, user)
    return repo


def _review_out(review: PullRequestReview) -> ReviewOut:
    return ReviewOut(
        id=review.id,
        pr_number=review.pr_number,
        head_sha=review.head_sha,
        pr_title=review.pr_title,
        summary=review.summary,
        comments=[
            ReviewCommentOut.model_validate(c) for c in (review.comments or [])
        ],
        status=review.status,
        review_url=review.review_url,
        error=review.error,
        created_at=review.created_at,
        updated_at=review.updated_at,
    )


def _find_review(
    db: Session, repo: Repo, pr_number: int, head_sha: str | None = None
) -> PullRequestReview | None:
    conditions = [
        PullRequestReview.repo_id == repo.id,
        PullRequestReview.pr_number == pr_number,
    ]
    if head_sha:
        conditions.append(PullRequestReview.head_sha == head_sha)
    return db.scalars(
        select(PullRequestReview)
        .where(*conditions)
        .order_by(PullRequestReview.created_at.desc())
        .limit(1)
    ).first()


def _in_flight_review_job(db: Session, repo_id: uuid.UUID, pr_number: int) -> Job | None:
    """A queued/running REVIEW_PR job for this PR — drives the "reviewing…"
    state in the UI and dedupes repeat clicks."""
    return db.scalars(
        select(Job).where(
            Job.kind == JobKind.REVIEW_PR,
            Job.status.in_((JobStatus.PENDING, JobStatus.RUNNING)),
            Job.payload["repo_id"].astext == str(repo_id),
            Job.payload["pr_number"].astext == str(pr_number),
        )
    ).first()


# --- Endpoints -----------------------------------------------------------


@router.get("/repos/{repo_id}/pulls", response_model=PullListOut)
def list_pulls(
    repo_id: uuid.UUID,
    limit: int = Query(default=30, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User | None = Depends(optional_user),
) -> PullListOut:
    """Open pull requests, each annotated with the review state of its
    CURRENT revision (head_sha) so a new push visibly clears the review."""
    repo = _repo_for(db, repo_id, user)
    from axon.adapters.github.adapter import GitHubAdapter  # noqa: PLC0415
    from axon.adapters.github.app_auth import token_for_repo  # noqa: PLC0415

    adapter = GitHubAdapter(repo.full_name, token=token_for_repo(repo))
    try:
        pulls = list(adapter.iter_open_pulls(limit=limit))
    except AdapterError as exc:
        raise HTTPException(
            status_code=502, detail=f"GitHub unavailable: {exc}"
        ) from exc

    items: list[PullOut] = []
    for pull in pulls:
        review = _find_review(db, repo, pull.number, pull.head_sha)
        pending = _in_flight_review_job(db, repo.id, pull.number)
        items.append(
            PullOut(
                number=pull.number,
                title=pull.title,
                author=pull.author,
                head_sha=pull.head_sha,
                draft=pull.draft,
                url=pull.url,
                updated_at=pull.updated_at,
                review_status=review.status if review else None,
                review_url=review.review_url if review else None,
                review_comment_count=len(review.comments or []) if review else 0,
                review_pending=pending is not None,
            )
        )
    return PullListOut(items=items)


@router.post(
    "/repos/{repo_id}/pulls/{pr_number}/review",
    response_model=ReviewRequestResponse,
)
def request_review(
    repo_id: uuid.UUID,
    pr_number: int,
    db: Session = Depends(get_db),
    user: User | None = Depends(optional_user),
) -> ReviewRequestResponse:
    """Enqueue an AI review of this pull request. Deduped: an in-flight job
    for the same PR is not enqueued twice."""
    repo = _repo_for(db, repo_id, user)

    existing_job = _in_flight_review_job(db, repo.id, pr_number)
    if existing_job is not None:
        return ReviewRequestResponse(status="queued", job_id=str(existing_job.id))

    job = queue.enqueue(
        db, JobKind.REVIEW_PR, {"repo_id": str(repo.id), "pr_number": pr_number}
    )
    return ReviewRequestResponse(status="queued", job_id=str(job.id))


@router.get(
    "/repos/{repo_id}/pulls/{pr_number}/review", response_model=ReviewOut
)
def get_review(
    repo_id: uuid.UUID,
    pr_number: int,
    db: Session = Depends(get_db),
    user: User | None = Depends(optional_user),
) -> ReviewOut:
    """The most recent stored review for this pull request."""
    repo = _repo_for(db, repo_id, user)
    review = _find_review(db, repo, pr_number)
    if review is None:
        raise HTTPException(
            status_code=404, detail="no review has been generated for this pull request"
        )
    return _review_out(review)


@router.post(
    "/repos/{repo_id}/pulls/{pr_number}/review/post", response_model=ReviewOut
)
def post_review(
    repo_id: uuid.UUID,
    pr_number: int,
    db: Session = Depends(get_db),
    user: User | None = Depends(optional_user),
) -> ReviewOut:
    """Publish the stored review to GitHub as the Axon app's bot identity.
    The explicit human-in-the-loop click — Axon never posts on its own."""
    repo = _repo_for(db, repo_id, user)
    review = _find_review(db, repo, pr_number)
    if review is None:
        raise HTTPException(
            status_code=404, detail="no review has been generated for this pull request"
        )
    if review.status == ReviewStatus.FAILED:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "review_failed",
                "message": "This review couldn't be generated, so there's nothing to post.",
                "reason": review.error,
            },
        )
    try:
        posted = PRReviewService(db).post_review(review)
    except AdapterError as exc:
        raise HTTPException(
            status_code=502, detail=f"GitHub rejected the review: {exc}"
        ) from exc
    return _review_out(posted)
