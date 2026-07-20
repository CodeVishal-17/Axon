"""T1.3 verification (deterministic half — real Postgres, no subprocesses).

Covers: enqueue/claim transitions, SKIP LOCKED under true concurrent
transactions, retry-with-backoff and permanent failure, error persistence,
stale-lock reclaim (crash recovery), and the worker loop end-to-end with a
stubbed handler registry. The live process-kill drill is
scripts/worker_smoke.py.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from axon.db import Base, models
from axon.db.session import get_engine
from axon.jobs import queue
from axon.jobs.handlers import UnknownJobKind, get_handler
from axon.jobs.worker import Worker


def _db_available() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_available(),
    reason="Postgres not reachable — start it with `docker compose up -d db`",
)


@pytest.fixture()
def db():
    engine = get_engine()
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    yield session
    session.rollback()
    session.execute(text("DELETE FROM jobs"))
    session.commit()
    session.close()


def _drain_jobs(db: Session) -> None:
    db.execute(text("DELETE FROM jobs"))
    db.commit()


# --- enqueue + claim transitions -----------------------------------------


def test_enqueue_claim_succeed_transitions(db: Session) -> None:
    _drain_jobs(db)
    job = queue.enqueue(db, models.JobKind.INGEST, {"repo_id": "x"})
    assert job.status == models.JobStatus.PENDING
    assert job.attempts == 0

    claimed = queue.claim_next(db)
    assert claimed is not None and claimed.id == job.id
    assert claimed.status == models.JobStatus.RUNNING
    assert claimed.attempts == 1
    assert claimed.locked_at is not None

    queue.mark_succeeded(db, claimed.id)
    db.expire_all()
    assert db.get(models.Job, job.id).status == models.JobStatus.SUCCEEDED


def test_future_jobs_not_claimed(db: Session) -> None:
    _drain_jobs(db)
    queue.enqueue(
        db,
        models.JobKind.INGEST,
        run_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert queue.claim_next(db) is None


# --- SKIP LOCKED: duplicate processing is impossible ----------------------


def test_skip_locked_prevents_duplicate_claim(db: Session) -> None:
    _drain_jobs(db)
    queue.enqueue(db, models.JobKind.INGEST, {"n": 1})

    engine = get_engine()
    # Two independent transactions, as two workers would hold them.
    with Session(engine) as worker_a, Session(engine) as worker_b:
        row_a = worker_a.execute(
            select(models.Job)
            .where(models.Job.status == models.JobStatus.PENDING)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()
        assert row_a is not None  # worker A holds the row lock now

        row_b = worker_b.execute(
            select(models.Job)
            .where(models.Job.status == models.JobStatus.PENDING)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()
        # SKIP LOCKED: B skips A's locked row instead of blocking or duping.
        assert row_b is None

        worker_a.rollback()
        worker_b.rollback()


# --- retry, backoff, permanent failure, error persistence -----------------


def test_retry_then_permanent_failure(db: Session) -> None:
    _drain_jobs(db)
    from axon.config import get_settings

    max_attempts = get_settings().job_max_attempts
    job = queue.enqueue(db, models.JobKind.INGEST, {"n": 1})

    for attempt in range(1, max_attempts + 1):
        # make any backoff from the previous failure due now
        db.execute(
            text("UPDATE jobs SET run_at = now() WHERE id = :id"), {"id": job.id}
        )
        db.commit()
        claimed = queue.claim_next(db)
        assert claimed is not None, f"attempt {attempt} should be claimable"
        assert claimed.attempts == attempt
        queue.mark_failed(db, claimed.id, f"boom {attempt}")

        db.expire_all()
        fresh = db.get(models.Job, job.id)
        assert fresh.error == f"boom {attempt}"  # errors persisted
        if attempt < max_attempts:
            assert fresh.status == models.JobStatus.PENDING  # retry scheduled
            assert fresh.run_at > datetime.now(timezone.utc)  # with backoff
        else:
            assert fresh.status == models.JobStatus.FAILED  # budget exhausted

    assert queue.claim_next(db) is None  # failed jobs are never re-claimed


# --- crash recovery: stale-lock reclaim -----------------------------------


def test_stale_running_job_is_requeued(db: Session) -> None:
    _drain_jobs(db)
    job = queue.enqueue(db, models.JobKind.INGEST, {"n": 1})
    claimed = queue.claim_next(db)
    assert claimed.status == models.JobStatus.RUNNING

    # Simulate a dead worker: running, locked long ago.
    db.execute(
        text("UPDATE jobs SET locked_at = now() - interval '1 hour' WHERE id = :id"),
        {"id": job.id},
    )
    db.commit()

    assert queue.requeue_stale(db, older_than_s=60) == 1
    db.expire_all()
    fresh = db.get(models.Job, job.id)
    assert fresh.status == models.JobStatus.PENDING
    assert "worker died" in fresh.error
    assert fresh.attempts == 1  # attempts survive the reclaim

    # A live running job (recent locked_at) is NOT reclaimed.
    reclaimed_again = queue.claim_next(db)
    assert reclaimed_again is not None
    assert queue.requeue_stale(db, older_than_s=60) == 0


# --- worker loop end-to-end ----------------------------------------------


def test_worker_run_once_dispatch(db: Session, monkeypatch) -> None:
    _drain_jobs(db)
    processed: list[dict] = []

    def fake_handler(session: Session, payload: dict) -> None:
        processed.append(payload)

    monkeypatch.setattr(
        "axon.jobs.worker.get_handler", lambda kind: fake_handler
    )
    job = queue.enqueue(db, models.JobKind.INGEST, {"repo_id": "abc"})

    worker = Worker(poll_interval_s=0.01)
    assert worker.run_once() is True  # processed one job
    assert worker.run_once() is False  # queue empty

    assert processed == [{"repo_id": "abc"}]
    db.expire_all()
    assert db.get(models.Job, job.id).status == models.JobStatus.SUCCEEDED


def test_worker_records_handler_failure(db: Session, monkeypatch) -> None:
    _drain_jobs(db)

    def exploding_handler(session: Session, payload: dict) -> None:
        raise RuntimeError("handler exploded")

    monkeypatch.setattr(
        "axon.jobs.worker.get_handler", lambda kind: exploding_handler
    )
    job = queue.enqueue(db, models.JobKind.INGEST, {})

    Worker(poll_interval_s=0.01).run_once()
    db.expire_all()
    fresh = db.get(models.Job, job.id)
    assert fresh.status == models.JobStatus.PENDING  # first failure → retry
    assert "handler exploded" in fresh.error
    assert fresh.attempts == 1


def test_every_job_kind_has_a_handler() -> None:
    """The registry is complete as of T4.1 — a new JobKind without a
    handler should fail here before it fails in production."""
    for kind in models.JobKind:
        assert callable(get_handler(kind))
