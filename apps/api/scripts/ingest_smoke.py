"""T1.2 live smoke test — ingest a real GitHub repository, twice.

Prints entity counts by kind, verifies the second run skips unchanged
content, verifies ignored files are excluded, and checks the roadmap's
ingest-time target (< 2 minutes).

Usage (from apps/api/):
    python scripts/ingest_smoke.py [owner/repo]

Defaults to this project's own repository. Unauthenticated GitHub API calls
are rate-limited to 60/hr; set GITHUB_TOKEN in .env for headroom.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from axon.adapters.github.adapter import GitHubAdapter  # noqa: E402
from axon.db import Base, models  # noqa: E402
from axon.db.session import get_engine  # noqa: E402
from axon.services.ingestion import IngestionService  # noqa: E402

TIME_TARGET_S = 120.0


def entity_counts(db: Session, repo: models.Repo) -> dict[str, int]:
    rows = db.execute(
        select(models.Entity.kind, func.count())
        .where(models.Entity.repo_id == repo.id)
        .group_by(models.Entity.kind)
    ).all()
    return {kind.value: count for kind, count in rows}


def main() -> None:
    full_name = sys.argv[1] if len(sys.argv) > 1 else "CodeVishal-17/Axon"
    engine = get_engine()
    Base.metadata.create_all(engine)

    with Session(engine, expire_on_commit=False) as db:
        repo = db.scalars(
            select(models.Repo).where(models.Repo.full_name == full_name)
        ).first()
        if repo is None:
            repo = models.Repo(full_name=full_name)
            db.add(repo)
            db.commit()

        adapter = GitHubAdapter(full_name)

        print(f"=== First ingest of {full_name} ===")
        report1 = IngestionService(db, adapter).run(repo)
        print(report1.summary())
        counts = entity_counts(db, repo)
        print(f"\nEntity counts by kind: {counts}")

        print(f"\n=== Second ingest (idempotency) ===")
        report2 = IngestionService(db, adapter).run(repo)
        print(report2.summary())

        # --- assertions ------------------------------------------------
        failures = []
        if report2.created or report2.updated or report2.deleted:
            failures.append(
                "second run wrote changes: "
                f"created={dict(report2.created)} updated={dict(report2.updated)} "
                f"deleted={dict(report2.deleted)}"
            )
        if sum(report2.skipped.values()) == 0:
            failures.append("second run skipped nothing")

        bad_paths = [
            p
            for p in db.scalars(
                select(models.Entity.path).where(
                    models.Entity.repo_id == repo.id, models.Entity.path.is_not(None)
                )
            )
            if "node_modules" in p or p.endswith((".lock", "-lock.json", ".png"))
            or p.split("/")[0].startswith(".")
        ]
        if bad_paths:
            failures.append(f"ignored files leaked into DB: {bad_paths[:5]}")

        for label, report in (("first", report1), ("second", report2)):
            if report.duration_s > TIME_TARGET_S:
                failures.append(
                    f"{label} ingest took {report.duration_s:.0f}s "
                    f"(target < {TIME_TARGET_S:.0f}s)"
                )

        print()
        if failures:
            for failure in failures:
                print(f"FAIL: {failure}")
            sys.exit(1)
        print(
            f"SMOKE OK — second run skipped {sum(report2.skipped.values())} entities, "
            f"ignored files excluded, timings {report1.duration_s:.1f}s / "
            f"{report2.duration_s:.1f}s (target < {TIME_TARGET_S:.0f}s)"
        )


if __name__ == "__main__":
    main()
