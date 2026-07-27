"""REVIEW_PR job handler — one pull request → one stored AI review.

Payload: {"repo_id": "<uuid>", "pr_number": <int>}. The service handles
terminal data outcomes (LLM failure, nothing reviewable) internally as review
states, so this job SUCCEEDS on them — a retry would re-derive the same
answer. Only transient GitHub failures (AdapterError) escape, which the queue
retries with backoff into the service's idempotent, head_sha-keyed flow.

Reviews are only GENERATED here. Publishing to GitHub is a separate,
explicitly clicked action (architecture §12) — never a background job.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from axon.db.models import Repo
from axon.services.pr_review import PRReviewService

logger = logging.getLogger("axon.jobs.review_pr")


def run(db: Session, payload: dict[str, Any]) -> None:
    repo_id = uuid.UUID(payload["repo_id"])
    pr_number = int(payload["pr_number"])
    repo = db.get(Repo, repo_id)
    if repo is None:
        raise ValueError(f"repo {repo_id} not found")

    service = PRReviewService(db)
    review = service.review(repo, pr_number)
    logger.info(
        "review_pr repo=%s pr=#%s status=%s comments=%d",
        repo.full_name, pr_number, review.status.value, len(review.comments or []),
    )
