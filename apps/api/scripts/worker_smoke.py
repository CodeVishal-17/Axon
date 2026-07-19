"""T1.3 live smoke drill — real worker processes, real kill, real GitHub.

Sequence:
  1. Enqueue an ingest job; start a worker subprocess; job succeeds.
  2. Enqueue another; hard-kill the worker the moment the job is claimed.
  3. Restart the worker (short stale-lock threshold); the job is reaped,
     retried, and completes — attempts == 2 proves the retry happened.

Usage (from apps/api/):
    python scripts/worker_smoke.py [owner/repo]
"""

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from axon.db import Base, models  # noqa: E402
from axon.db.session import get_engine  # noqa: E402
from axon.jobs import queue  # noqa: E402

WORKER_ENV = {
    **os.environ,
    "WORKER_POLL_INTERVAL_S": "0.3",
    "JOB_STALE_LOCK_SECONDS": "2",
    "LOG_LEVEL": "INFO",
}


def start_worker() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "axon.jobs.worker"],
        env=WORKER_ENV,
        cwd=Path(__file__).resolve().parents[1],
    )


def wait_for(db: Session, job_id: uuid.UUID, statuses: set, timeout_s: float) -> models.Job:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        db.expire_all()
        job = db.get(models.Job, job_id)
        if job.status in statuses:
            return job
        time.sleep(0.2)
    raise TimeoutError(
        f"job {job_id} did not reach {[s.value for s in statuses]} in {timeout_s}s "
        f"(current: {job.status.value}, error: {job.error})"
    )


def main() -> None:
    full_name = sys.argv[1] if len(sys.argv) > 1 else "CodeVishal-17/Axon"
    engine = get_engine()
    Base.metadata.create_all(engine)

    with Session(engine, expire_on_commit=False) as db:
        db.execute(text("DELETE FROM jobs"))
        db.commit()
        repo = db.scalars(
            select(models.Repo).where(models.Repo.full_name == full_name)
        ).first()
        if repo is None:
            repo = models.Repo(full_name=full_name)
            db.add(repo)
            db.commit()

        # --- 1. happy path ---------------------------------------------
        print("=== 1. enqueue + worker processes successfully ===")
        job1 = queue.enqueue(db, models.JobKind.INGEST, {"repo_id": str(repo.id)})
        worker = start_worker()
        done = wait_for(
            db, job1.id,
            {models.JobStatus.SUCCEEDED, models.JobStatus.FAILED}, 90,
        )
        assert done.status == models.JobStatus.SUCCEEDED, done.error
        print(f"job1 succeeded (attempts={done.attempts})")

        # --- 2. kill mid-job -------------------------------------------
        print("\n=== 2. kill worker mid-job ===")
        job2 = queue.enqueue(db, models.JobKind.INGEST, {"repo_id": str(repo.id)})
        running = wait_for(db, job2.id, {models.JobStatus.RUNNING}, 30)
        worker.kill()  # hard kill — no graceful shutdown, job stays RUNNING
        worker.wait(timeout=10)
        print(f"worker killed while job2 running (attempts={running.attempts})")

        time.sleep(3)  # let the lock go stale (threshold 2s)
        db.expire_all()
        stuck = db.get(models.Job, job2.id)
        assert stuck.status == models.JobStatus.RUNNING, (
            f"expected job2 stuck in running, got {stuck.status.value}"
        )
        print("job2 confirmed stuck in 'running' — orphaned by the dead worker")

        # --- 3. restart → reap → retry → complete ----------------------
        print("\n=== 3. restart worker: stale job reaped and retried ===")
        worker = start_worker()
        try:
            done2 = wait_for(
                db, job2.id,
                {models.JobStatus.SUCCEEDED, models.JobStatus.FAILED}, 90,
            )
        finally:
            worker.terminate()
            worker.wait(timeout=15)

        assert done2.status == models.JobStatus.SUCCEEDED, done2.error
        assert done2.attempts == 2, f"expected 2 attempts, got {done2.attempts}"
        print(
            f"job2 recovered and succeeded after restart "
            f"(attempts={done2.attempts}, prior error recorded: "
            f"{'worker died' in (done2.error or '') or done2.error is None})"
        )

        print("\nWORKER SMOKE OK — enqueue, process, crash, reap, retry, complete.")


if __name__ == "__main__":
    main()
