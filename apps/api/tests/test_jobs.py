"""T1.3 verification (deterministic half — real Postgres, no subprocesses).

Covers: enqueue/claim transitions, SKIP LOCKED under true concurrent
transactions, retry-with-backoff and permanent failure, error persistence,
stale-lock reclaim (crash recovery), and the worker loop end-to-end with a
stubbed handler registry. The live process-kill drill is
scripts/worker_smoke.py.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from axon.db import Base, models
from axon.db.session import get_engine
from axon.jobs import queue
from axon.jobs.handlers import get_handler
from axon.jobs.worker import Worker


@pytest.fixture()
def db():
    """A session whose queue is empty on ENTRY as well as on exit.

    These tests reason about the queue globally — "claim the next job",
    "nothing is claimable now" — so they are only meaningful starting from an
    empty table. Cleaning up on the way out was not enough: earlier modules
    leave their own jobs behind (measured: test_events, test_findings_api,
    test_pull_requests_api and test_remediation all do), and a due, PENDING
    row from one of them is indistinguishable from this module's own work.
    Draining on entry makes every test here independent of collection order.
    """
    engine = get_engine()
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    session.execute(text("DELETE FROM jobs"))
    session.commit()
    yield session
    session.rollback()
    session.execute(text("DELETE FROM jobs"))
    session.commit()
    session.close()


def _drain_jobs(db: Session) -> None:
    """Empty the queue, and PROVE it is empty.

    These tests reason about the whole queue ("claim the next job", "nothing
    is claimable now"), so a single row left behind by another module changes
    what they mean. A module once leaked a due, pending verify job here; the
    assertion turns that from an intermittent, far-away failure into an
    immediate one naming the culprit.
    """
    db.execute(text("DELETE FROM jobs"))
    db.commit()
    remaining = db.execute(text("SELECT count(*) FROM jobs")).scalar()
    assert remaining == 0, f"queue not empty after drain: {remaining} row(s) leaked"


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
        run_at=datetime.now(UTC) + timedelta(hours=1),
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
    from axon.config import get_settings  # noqa: PLC0415

    max_attempts = get_settings().job_max_attempts
    job = queue.enqueue(db, models.JobKind.INGEST, {"n": 1})

    for attempt in range(1, max_attempts + 1):
        # Make the previous failure's backoff due. Deliberately well in the
        # past rather than `now()`: the row would be stamped with POSTGRES's
        # clock while claim_next() filters on `run_at <= _now()` using
        # PYTHON's. Those clocks agree here to about 1.5 ms, which is inside
        # the host timer's granularity — a margin thin enough to lose. Backing
        # the timestamp off removes the dependency entirely without changing
        # what the test asserts.
        db.execute(
            text("UPDATE jobs SET run_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": job.id},
        )
        db.commit()
        claimed = queue.claim_next(db)
        assert claimed is not None, f"attempt {attempt} should be claimable"
        # Identity first: claim_next takes the next claimable job in the whole
        # queue, so a stray row from another module would otherwise surface as
        # a baffling attempts mismatch rather than "that isn't my job".
        assert claimed.id == job.id, "claimed a job this test did not enqueue"
        assert claimed.attempts == attempt
        queue.mark_failed(db, claimed.id, f"boom {attempt}")

        db.expire_all()
        fresh = db.get(models.Job, job.id)
        assert fresh.error == f"boom {attempt}"  # errors persisted
        if attempt < max_attempts:
            assert fresh.status == models.JobStatus.PENDING  # retry scheduled
            assert fresh.run_at > datetime.now(UTC)  # with backoff
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
