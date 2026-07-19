"""T1.4 live smoke — POST /repos through the full pipeline.

Drives the real API (in-process TestClient), a real worker subprocess, and
real GitHub: connects a repo, observes ingest_status transitions
(pending → ingesting → ready), then lists ingested entities.

Usage (from apps/api/):
    python scripts/api_smoke.py [owner/repo]
"""

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from axon.db import Base, models  # noqa: E402
from axon.db.session import get_engine  # noqa: E402
from axon.main import create_app  # noqa: E402

WORKER_ENV = {**os.environ, "WORKER_POLL_INTERVAL_S": "0.3", "LOG_LEVEL": "WARNING"}


def main() -> None:
    full_name = sys.argv[1] if len(sys.argv) > 1 else "CodeVishal-17/Axon"
    engine = get_engine()
    Base.metadata.create_all(engine)

    # Clean slate so the pending → ingesting → ready transitions are real.
    with Session(engine) as db:
        for repo in db.scalars(
            select(models.Repo).where(models.Repo.full_name == full_name)
        ):
            db.delete(repo)
        db.commit()

    client = TestClient(create_app())
    worker = subprocess.Popen(
        [sys.executable, "-m", "axon.jobs.worker"],
        env=WORKER_ENV,
        cwd=Path(__file__).resolve().parents[1],
    )
    try:
        print(f"=== POST /api/repos {full_name} ===")
        created = client.post("/api/repos", json={"full_name": full_name})
        assert created.status_code == 200, created.text
        body = created.json()
        repo_id = body["id"]
        print(
            f"repo id={repo_id} status={body['ingest_status']} "
            f"job={body['latest_job']['status']}"
        )

        print("\n=== GET /api/repos/{id} — status transitions ===")
        observed: list[str] = [body["ingest_status"]]
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            detail = client.get(f"/api/repos/{repo_id}").json()
            status = detail["ingest_status"]
            if status != observed[-1]:
                observed.append(status)
                print(f"  -> {status}")
            if status in ("ready", "failed"):
                break
            time.sleep(0.15)

        assert observed[-1] == "ready", f"final status {observed[-1]}: {detail['latest_job']}"
        assert observed[0] == "pending" and "ingesting" in observed, observed
        print(f"transitions observed: {' -> '.join(observed)}")
        print(
            f"last_ingested_sha={detail['last_ingested_sha'][:12]}... "
            f"job={detail['latest_job']['status']} attempts={detail['latest_job']['attempts']}"
        )
        print(f"entity_counts={detail['entity_counts']}")

        print("\n=== GET /api/repos/{id}/entities ===")
        page = client.get(
            f"/api/repos/{repo_id}/entities?kind=doc_section&limit=5"
        ).json()
        print(f"doc_sections total={page['total']}, first {len(page['items'])}:")
        for item in page["items"]:
            print(f"  {item['path']}  ({item['name']})")
        assert page["total"] > 0

        search = client.get(f"/api/repos/{repo_id}/entities?q=ingestion").json()
        print(f"search q=ingestion -> {search['total']} hits")

        print("\nAPI SMOKE OK — POST enqueued, worker ingested, "
              "pending -> ingesting -> ready, entities served.")
    finally:
        worker.terminate()
        worker.wait(timeout=15)


if __name__ == "__main__":
    main()
