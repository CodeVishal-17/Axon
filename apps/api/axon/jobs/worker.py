"""The worker process: poll → claim → dispatch → record outcome.

Run with:  python -m axon.jobs.worker

One codebase, two processes (architecture §2): this worker imports the same
services the API uses, but never touches HTTP. All LLM/network-heavy work
lives here so API requests stay fast.

Shutdown: SIGINT/SIGTERM set a stop flag; the in-flight job finishes, then
the loop exits. A hard kill instead leaves the job in ``running`` — the
stale-lock reaper (run each poll) returns it to ``pending`` after
``JOB_STALE_LOCK_SECONDS``, which is how a restarted worker resumes work.
"""

from __future__ import annotations

import logging
import signal
import time

from sqlalchemy.orm import Session

from axon.config import get_settings
from axon.db.models import Job
from axon.db.session import get_sessionmaker
from axon.jobs import queue
from axon.jobs.handlers import get_handler
from axon.adapters.base import RateLimitError, AuthenticationError

logger = logging.getLogger("axon.jobs.worker")


class Worker:
    def __init__(self, poll_interval_s: float | None = None) -> None:
        settings = get_settings()
        self.poll_interval_s = (
            poll_interval_s
            if poll_interval_s is not None
            else settings.worker_poll_interval_s
        )
        self._stop = False

    # -- lifecycle --------------------------------------------------------

    def install_signal_handlers(self) -> None:
        def request_stop(signum: int, _frame: object) -> None:
            logger.info("signal %s received — finishing current job, then exiting", signum)
            self._stop = True

        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)

    def run(self) -> None:
        logger.info(
            "worker started poll_interval=%.1fs max_attempts=%d stale_lock=%ds",
            self.poll_interval_s,
            get_settings().job_max_attempts,
            get_settings().job_stale_lock_seconds,
        )
        while not self._stop:
            try:
                worked = self.run_once()
            except Exception:  # noqa: BLE001 — a DB outage must not kill the worker
                logger.exception(
                    "poll cycle failed (database down?) — retrying in %.1fs",
                    self.poll_interval_s,
                )
                worked = False
            if not worked and not self._stop:
                time.sleep(self.poll_interval_s)
        logger.info("worker stopped")

    # -- one poll cycle ---------------------------------------------------

    def run_once(self) -> bool:
        """Reap stale jobs, then claim and process at most one job.
        Returns True if a job was processed (poll again immediately)."""
        session_factory = get_sessionmaker()
        with session_factory() as db:
            queue.requeue_stale(db)
            job = queue.claim_next(db)
            if job is None:
                return False
            self._process(db, job)
            return True

    def _process(self, db: Session, job: Job) -> None:
        job_id, kind, payload = job.id, job.kind, dict(job.payload)
        started = time.monotonic()
        try:
            handler = get_handler(kind)
            handler(db, payload)
            queue.mark_succeeded(db, job_id)
            logger.info(
                "job done id=%s kind=%s duration=%.1fs",
                job_id, kind.value, time.monotonic() - started,
            )
        except Exception as exc:  # noqa: BLE001 — worker must survive any handler error
            db.rollback()  # discard the handler's partial work
            logger.exception("job handler raised id=%s kind=%s", job_id, kind.value)
            
            run_at = None
            if isinstance(exc, RateLimitError) and exc.reset_at:
                run_at = exc.reset_at
            
            if isinstance(exc, AuthenticationError):
                # Expired token won't recover; fail permanently via existing mechanism
                job = db.get(Job, job_id)
                if job:
                    job.attempts = get_settings().job_max_attempts
                    db.commit()

            queue.mark_failed(db, job_id, f"{type(exc).__name__}: {exc}", run_at=run_at)


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    worker = Worker()
    worker.install_signal_handlers()
    worker.run()


if __name__ == "__main__":
    main()
