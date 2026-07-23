"""Dashboard + auth-scoping API tests (need Postgres; skip without it).

Verifies the per-user rollup aggregates the existing findings/fixes history
correctly, and that repo ownership is enforced: connecting requires sign-in,
and one user cannot see another's repo.
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


def _make_user(db: Session, login: str) -> models.User:
    user = models.User(github_id=uuid.uuid4().int % 1_000_000_000, login=login)
    db.add(user)
    db.commit()
    return user


def _seed_repo_with_history(db: Session, owner: models.User) -> models.Repo:
    repo = models.Repo(
        full_name=f"axon-test/dash-{uuid.uuid4().hex[:8]}",
        owner_id=owner.id,
        ingest_status=models.IngestStatus.READY,
    )
    claim = models.Claim(
        repo=repo,
        statement="The service runs on port 5000.",
        claim_type=models.ClaimType.BEHAVIOR,
        status=models.ClaimStatus.CONTRADICTED,
        anchor={},
    )
    # Three findings in different states; two carry fixes (one PR, one blocked).
    f_open = models.Finding(
        repo=repo, claim=claim, kind=models.FindingKind.DOC_DRIFT,
        severity=models.FindingSeverity.HIGH, explanation="drift",
        evidence={}, status=models.FindingStatus.OPEN,
    )
    f_actioned = models.Finding(
        repo=repo, claim=claim, kind=models.FindingKind.DOC_DRIFT,
        severity=models.FindingSeverity.HIGH, explanation="drift2",
        evidence={}, status=models.FindingStatus.ACTIONED,
    )
    f_blocked = models.Finding(
        repo=repo, claim=claim, kind=models.FindingKind.DOC_DRIFT,
        severity=models.FindingSeverity.MEDIUM, explanation="drift3",
        evidence={}, status=models.FindingStatus.OPEN,
    )
    db.add_all([repo, claim, f_open, f_actioned, f_blocked])
    db.flush()
    db.add_all([
        models.Fix(
            finding_id=f_actioned.id, status=models.FixStatus.PR_OPENED,
            pr_url="https://github.com/axon-test/dash/pull/1", patch="{}",
        ),
        models.Fix(
            finding_id=f_blocked.id, status=models.FixStatus.FAILED,
            error="ungrounded values: ['6', '8']", patch="{}",
        ),
    ])
    db.commit()
    return repo


def _as_user(app, user: models.User) -> None:
    app.dependency_overrides[current_user] = lambda: user
    app.dependency_overrides[optional_user] = lambda: user


def test_dashboard_aggregates_history(db: Session, app) -> None:
    user = _make_user(db, "axon-test-owner")
    _seed_repo_with_history(db, user)
    _as_user(app, user)

    with TestClient(app) as client:
        body = client.get("/api/dashboard").json()

    totals = body["totals"]
    assert totals["findings_total"] == 3
    assert totals["findings_open"] == 2
    assert totals["findings_actioned"] == 1
    assert totals["prs_opened"] == 1
    assert totals["fixes_blocked"] == 1
    assert len(body["repos"]) == 1
    assert body["repos"][0]["prs_opened"] == 1
    # Activity surfaces the PR link and the block reason.
    kinds = {a["kind"] for a in body["recent_activity"]}
    assert {"pr_opened", "blocked"} <= kinds
    pr = next(a for a in body["recent_activity"] if a["kind"] == "pr_opened")
    assert pr["pr_url"].endswith("/pull/1")
    blocked = next(a for a in body["recent_activity"] if a["kind"] == "blocked")
    assert "ungrounded" in blocked["reason"]

    app.dependency_overrides.clear()


def test_dashboard_requires_auth(app) -> None:
    with TestClient(app) as client:
        assert client.get("/api/dashboard").status_code == 401


def test_connect_requires_auth(app) -> None:
    with TestClient(app) as client:
        resp = client.post("/api/repos", json={"full_name": "octocat/hello"})
    assert resp.status_code == 401


def test_repo_private_to_owner(db: Session, app) -> None:
    owner = _make_user(db, "axon-test-a")
    other = _make_user(db, "axon-test-b")
    repo = _seed_repo_with_history(db, owner)

    # Another signed-in user gets 404 (not 403 — don't leak existence).
    _as_user(app, other)
    with TestClient(app) as client:
        assert client.get(f"/api/repos/{repo.id}").status_code == 404
        assert client.get(f"/api/repos/{repo.id}/findings").status_code == 404
    app.dependency_overrides.clear()

    # The owner sees it.
    _as_user(app, owner)
    with TestClient(app) as client:
        assert client.get(f"/api/repos/{repo.id}").status_code == 200
    app.dependency_overrides.clear()
