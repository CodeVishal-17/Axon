"""Postgres-backed job queue.

Why Postgres and not Celery/Redis (architecture §2): transactional enqueue
(a job commits atomically with the data that spawned it), visibility via
plain SQL, and one fewer infrastructure dependency. ``FOR UPDATE SKIP
LOCKED`` gives safe concurrent claiming — two workers can poll the same
table and never receive the same job.

State machine:

    pending ──claim──▶ running ──▶ succeeded
        ▲                 │
        │   retry (attempts < max, backoff)
        └─────────────────┤
                          └──▶ failed (attempts exhausted)

Crash recovery: a claim COMMITS the ``running`` state, so a worker that
dies mid-job leaves the row in ``running`` with a frozen ``locked_at``.
:func:`requeue_stale` returns such rows to ``pending``; the stale threshold
must exceed the longest legitimate job duration, since a running job holds
no row lock while its handler executes.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from axon.config import get_settings
from axon.db.models import Job, JobKind, JobStatus

logger = logging.getLogger("axon.jobs.queue")

_ERROR_MAX_CHARS = 2000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def enqueue(
    db: Session,
    kind: JobKind,
    payload: dict[str, Any] | None = None,
    run_at: datetime | None = None,
) -> Job:
    """Insert a pending job. Commits — callers enqueue as a final act of
    their own unit of work."""
    job = Job(kind=kind, payload=payload or {}, run_at=run_at or _now())
    db.add(job)
    db.commit()
    logger.info("job enqueued id=%s kind=%s", job.id, kind.value)
    return job


def claim_next(db: Session) -> Job | None:
    """Atomically claim the oldest due pending job, or None.

    SELECT ... FOR UPDATE SKIP LOCKED: concurrent claimers skip rows locked
    by each other instead of blocking, so no two workers ever get the same
    job. The claim (status=running, attempts+1, locked_at) is committed
    immediately — from then on the job is owned by this worker until it
    finishes or is reaped as stale.
    """
    job = db.execute(
        select(Job)
        .where(Job.status == JobStatus.PENDING, Job.run_at <= _now())
        .order_by(Job.run_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if job is None:
        db.rollback()  # end the read transaction promptly
        return None

    job.status = JobStatus.RUNNING
    job.locked_at = _now()
    job.attempts += 1
    db.commit()
    logger.info(
        "job claimed id=%s kind=%s attempt=%d", job.id, job.kind.value, job.attempts
    )
    return job


def mark_succeeded(db: Session, job_id: uuid.UUID) -> None:
    job = db.get(Job, job_id)
    if job is None:
        return
    job.status = JobStatus.SUCCEEDED
    job.error = None
    db.commit()
    logger.info("job succeeded id=%s kind=%s", job.id, job.kind.value)


def mark_failed(db: Session, job_id: uuid.UUID, error: str, run_at: datetime | None = None) -> None:
    """Record the failure; requeue with linear backoff while attempts
    remain, otherwise fail permanently."""
    settings = get_settings()
    job = db.get(Job, job_id)
    if job is None:
        return
    job.error = error[:_ERROR_MAX_CHARS]

    if job.attempts < settings.job_max_attempts:
        if run_at is not None:
            job.run_at = run_at
        else:
            backoff = timedelta(seconds=settings.job_retry_backoff_s * job.attempts)
            job.run_at = _now() + backoff
        job.status = JobStatus.PENDING
        db.commit()
        logger.warning(
            "job retry id=%s kind=%s attempt=%d/%d scheduled_for=%s error=%s",
            job.id, job.kind.value, job.attempts,
            settings.job_max_attempts, job.run_at.isoformat(), error[:200],
        )
    else:
        job.status = JobStatus.FAILED
        db.commit()
        logger.error(
            "job failed permanently id=%s kind=%s attempts=%d error=%s",
            job.id, job.kind.value, job.attempts, error[:200],
        )


def requeue_stale(db: Session, older_than_s: float | None = None) -> int:
    """Return crashed-worker jobs (running, stale locked_at) to pending.

    Attempts are NOT reset — a job that crashes its worker on every attempt
    still exhausts its retry budget and lands in ``failed`` instead of
    crash-looping forever.
    """
    settings = get_settings()
    threshold = older_than_s if older_than_s is not None else settings.job_stale_lock_seconds
    cutoff = _now() - timedelta(seconds=threshold)

    stale_jobs = db.scalars(
        select(Job)
        .where(Job.status == JobStatus.RUNNING, Job.locked_at < cutoff)
        .with_for_update(skip_locked=True)
    ).all()
    for job in stale_jobs:
        was_last_attempt = job.attempts >= settings.job_max_attempts
        job.status = JobStatus.FAILED if was_last_attempt else JobStatus.PENDING
        job.run_at = _now()
        job.error = (
            f"worker died mid-job (locked_at={job.locked_at}, attempt={job.attempts})"
        )
        logger.warning(
            "stale job reclaimed id=%s kind=%s attempt=%d -> %s",
            job.id, job.kind.value, job.attempts, job.status.value,
        )
    if stale_jobs:
        db.commit()
    else:
        db.rollback()
    return len(stale_jobs)
