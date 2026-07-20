"""GENERATE_FIX job handler — one fix → one pull request.

Payload: {"fix_id": "<uuid>"}. The service handles terminal data outcomes
(stale/ambiguous/invalid) internally as fix states, so this job SUCCEEDS
on them — retrying would re-derive the same answer. Only transient GitHub
failures (AdapterError) escape, which the queue retries with backoff into
the service's idempotent flow.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from axon.services.pr import GitHubPRService

logger = logging.getLogger("axon.jobs.generate_fix")


def run(db: Session, payload: dict[str, Any]) -> None:
    outcome = GitHubPRService(db).open_pr_for_fix(uuid.UUID(payload["fix_id"]))
    logger.info(
        "generate_fix outcome=%s pr=%s %s",
        outcome.status, outcome.pr_url, outcome.reason,
    )
