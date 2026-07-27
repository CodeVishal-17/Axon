"""Pull-request review API tests (need Postgres; skip without it).

Covers the router's contract: ownership is enforced, requesting a review
enqueues exactly one job (repeat clicks dedupe), and the review endpoints
report honest states.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from axon.api.auth import current_user, optional_user
from axon.db import Base, models
from axon.db.session import get_engine
from axon.main import create_app


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
    Base.metadata.create_all(get_engine())
    with Session(get_engine(), expire_on_commit=False) as session:
        yield session
        session.rollback()
        for repo in session.scalars(
            select(models.Repo).where(models.Repo.full_name.like("axon-test/%"))
        ):
            session.delete(repo)
        for user in session.scalars(
            select(models.User).where(models.User.login.like("axon-test-%"))
        ):
            session.delete(user)
        session.commit()


@pytest.fixture()
def app():
    Base.metadata.create_all(get_engine())
    return create_app()


def _user(db: Session, login: str) -> models.User:
    user = models.User(github_id=uuid.uuid4().int % 1_000_000_000, login=login)
    db.add(user)
    db.commit()
    return user


def _repo(db: Session, owner: models.User) -> models.Repo:
    repo = models.Repo(
        full_name=f"axon-test/pulls-{uuid.uuid4().hex[:8]}",
        owner_id=owner.id,
        ingest_status=models.IngestStatus.READY,
    )
    db.add(repo)
    db.commit()
    return repo


def _as(app, user: models.User) -> None:
    app.dependency_overrides[current_user] = lambda: user
    app.dependency_overrides[optional_user] = lambda: user


def test_endpoints_require_repo_access(db: Session, app) -> None:
    owner = _user(db, "axon-test-owner")
    other = _user(db, "axon-test-other")
    repo = _repo(db, owner)

    _as(app, other)
    with TestClient(app) as client:
        # 404 (not 403) everywhere: never leak that the repo exists.
        assert client.get(f"/api/repos/{repo.id}/pulls").status_code == 404
        assert client.post(f"/api/repos/{repo.id}/pulls/1/review").status_code == 404
        assert client.get(f"/api/repos/{repo.id}/pulls/1/review").status_code == 404
        assert (
            client.post(f"/api/repos/{repo.id}/pulls/1/review/post").status_code == 404
        )
    app.dependency_overrides.clear()


def test_request_review_enqueues_exactly_one_job(db: Session, app) -> None:
    owner = _user(db, "axon-test-owner")
    repo = _repo(db, owner)
    _as(app, owner)

    with TestClient(app) as client:
        first = client.post(f"/api/repos/{repo.id}/pulls/7/review")
        second = client.post(f"/api/repos/{repo.id}/pulls/7/review")

    assert first.status_code == 200 and first.json()["status"] == "queued"
    # Repeat click returns the SAME job rather than queuing a duplicate.
    assert second.json()["job_id"] == first.json()["job_id"]

    jobs = db.scalars(
        select(models.Job)
        .where(models.Job.kind == models.JobKind.REVIEW_PR)
        .where(models.Job.payload["repo_id"].astext == str(repo.id))
    ).all()
    assert len(jobs) == 1
    assert jobs[0].payload["pr_number"] == 7
    app.dependency_overrides.clear()


def test_get_review_404_before_generation(db: Session, app) -> None:
    owner = _user(db, "axon-test-owner")
    repo = _repo(db, owner)
    _as(app, owner)
    with TestClient(app) as client:
        assert client.get(f"/api/repos/{repo.id}/pulls/7/review").status_code == 404
    app.dependency_overrides.clear()


def test_get_review_returns_stored_review(db: Session, app) -> None:
    owner = _user(db, "axon-test-owner")
    repo = _repo(db, owner)
    db.add(
        models.PullRequestReview(
            repo_id=repo.id, pr_number=7, head_sha="abc1234",
            pr_title="Serve on port 8080", summary="Switches the port.",
            comments=[
                {"path": "app/config.py", "line": 2, "lens": "code",
                 "severity": "high", "body": "Hard-coded.", "confidence": 0.9},
                {"path": "app/config.py", "line": 3, "lens": "truth",
                 "severity": "high", "body": "README says 5000.", "confidence": 0.95},
            ],
            status=models.ReviewStatus.GENERATED,
        )
    )
    db.commit()
    _as(app, owner)

    with TestClient(app) as client:
        body = client.get(f"/api/repos/{repo.id}/pulls/7/review").json()

    assert body["status"] == "generated"
    assert body["head_sha"] == "abc1234"
    assert sorted(c["lens"] for c in body["comments"]) == ["code", "truth"]
    app.dependency_overrides.clear()


def test_post_failed_review_is_rejected(db: Session, app) -> None:
    owner = _user(db, "axon-test-owner")
    repo = _repo(db, owner)
    db.add(
        models.PullRequestReview(
            repo_id=repo.id, pr_number=7, head_sha="abc1234",
            comments=[], status=models.ReviewStatus.FAILED,
            error="LLM failure: scripted",
        )
    )
    db.commit()
    _as(app, owner)

    with TestClient(app) as client:
        resp = client.post(f"/api/repos/{repo.id}/pulls/7/review/post")

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "review_failed"
    app.dependency_overrides.clear()
