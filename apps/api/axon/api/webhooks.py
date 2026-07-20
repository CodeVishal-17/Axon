"""Event ingress: GitHub webhooks + the demo-day simulate endpoint.

Both routes run the IDENTICAL downstream path (normalize → EventService →
VERIFY job). The simulate endpoint exists because conference Wi-Fi and
GitHub delivery are outside our control at the one moment that matters —
it is a delivery bypass, not a fake (architecture §6).

Security: webhook deliveries are HMAC-verified against
GITHUB_WEBHOOK_SECRET (constant-time compare). An unset secret accepts
unsigned deliveries with a loud warning — dev convenience only. The
simulate endpoint requires the X-Axon-Simulate-Secret header whenever
SIMULATE_SHARED_SECRET is configured.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from axon.adapters.github.adapter import GitHubAdapter
from axon.config import get_settings
from axon.db.models import Repo
from axon.db.session import get_db
from axon.services.events import EventService, IngestOutcome

logger = logging.getLogger("axon.api.webhooks")

router = APIRouter(prefix="/api", tags=["events"])


class EventAccepted(BaseModel):
    status: str  # accepted | duplicate | reingest | ignored
    event_id: str | None = None
    job_id: str | None = None


def _verify_signature(secret: str | None, body: bytes, signature: str | None) -> None:
    if not secret:
        logger.warning(
            "GITHUB_WEBHOOK_SECRET is not set — accepting UNSIGNED webhook "
            "delivery (dev only; configure the secret in production)"
        )
        return
    if not signature or not signature.startswith("sha256="):
        raise HTTPException(status_code=401, detail="missing webhook signature")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature.removeprefix("sha256="), expected):
        raise HTTPException(status_code=401, detail="invalid webhook signature")


def _outcome_response(outcome: IngestOutcome) -> EventAccepted:
    return EventAccepted(
        status=outcome.status, event_id=outcome.event_id, job_id=outcome.job_id
    )


@router.post("/webhooks/github", response_model=EventAccepted)
async def github_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_github_event: str = Header(default=""),
    x_github_delivery: str = Header(default=""),
    x_hub_signature_256: str | None = Header(default=None),
) -> EventAccepted:
    body = await request.body()
    _verify_signature(
        get_settings().github_webhook_secret, body, x_hub_signature_256
    )

    import json  # noqa: PLC0415

    try:
        payload: dict[str, Any] = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="invalid JSON body") from exc

    full_name = (payload.get("repository") or {}).get("full_name")
    if not full_name:
        return EventAccepted(status="ignored")
    repo = db.scalars(
        select(Repo).where(Repo.provider == "github", Repo.full_name == full_name)
    ).first()
    if repo is None:
        # Not connected — acknowledge so GitHub doesn't retry forever.
        return EventAccepted(status="ignored")

    normalized = GitHubAdapter.normalize_webhook(
        x_github_event,
        x_github_delivery or f"nodelivery-{uuid.uuid4().hex[:12]}",
        payload,
        repo.default_branch,
    )
    return _outcome_response(EventService(db).ingest(repo, normalized))


class SimulateRequest(BaseModel):
    """A canned GitHub-style webhook: same event names, same payload
    shapes, run through the identical normalization + pipeline."""

    event: str = Field(examples=["push", "pull_request", "issues"])
    payload: dict[str, Any]
    external_id: str | None = None


@router.post("/repos/{repo_id}/simulate-event", response_model=EventAccepted)
def simulate_event(
    repo_id: uuid.UUID,
    body: SimulateRequest,
    db: Session = Depends(get_db),
    x_axon_simulate_secret: str | None = Header(default=None),
) -> EventAccepted:
    settings = get_settings()
    if settings.simulate_shared_secret and not hmac.compare_digest(
        x_axon_simulate_secret or "", settings.simulate_shared_secret
    ):
        raise HTTPException(status_code=403, detail="invalid simulate secret")

    repo = db.get(Repo, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="repository not found")

    normalized = GitHubAdapter.normalize_webhook(
        body.event,
        body.external_id or f"sim-{uuid.uuid4().hex[:12]}",
        body.payload,
        repo.default_branch,
    )
    # Honest provenance: simulated deliveries are marked as such, but the
    # planner reads only the normalized payload, so behavior is identical.
    if normalized is not None and not normalized.reingest_only:
        normalized = dataclasses.replace(normalized, kind="simulated")
    return _outcome_response(EventService(db).ingest(repo, normalized))
