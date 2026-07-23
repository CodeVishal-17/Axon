"""Findings endpoint tests (schema contract for the Truth Feed)."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

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


@pytest.fixture(scope="module")
def client():
    Base.metadata.create_all(get_engine())
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture()
def db():
    with Session(get_engine(), expire_on_commit=False) as session:
        yield session
        session.rollback()
        for repo in session.scalars(
            select(models.Repo).where(models.Repo.full_name.like("axon-test/%"))
        ):
            session.delete(repo)
        session.commit()


@pytest.fixture()
def seeded(db: Session) -> str:
    repo = models.Repo(full_name=f"axon-test/findings-{uuid.uuid4().hex[:8]}")
    doc = models.Entity(
        repo=repo, kind=models.EntityKind.DOC_SECTION, name="Auth",
        path="docs/auth.md#auth",
    )
    claim = models.Claim(
        repo=repo, source_entity=doc,
        statement="Access tokens expire after 24 hours.",
        claim_type=models.ClaimType.BEHAVIOR,
        status=models.ClaimStatus.CONTRADICTED,
        anchor={"path": "docs/auth.md", "start_line": 12, "end_line": 14},
    )
    event = models.Event(
        repo=repo, kind=models.EventKind.PR_MERGED, external_id="47",
    )
    open_finding = models.Finding(
        repo=repo, claim=claim, event=event,
        kind=models.FindingKind.DOC_DRIFT,
        severity=models.FindingSeverity.HIGH,
        explanation="Docs say 24h; code says 1h since PR #47.",
        evidence={
            "quotes": [
                {"text": "const TOKEN_TTL_HOURS = 1", "path": "src/auth/token.ts",
                 "start_line": 12, "extra_provider_key": "ignored"},
            ],
            "diff": "-24\n+1",
        },
        suggested_action="Update docs/auth.md.",
    )
    dismissed = models.Finding(
        repo=repo, claim=claim, kind=models.FindingKind.STALE_ISSUE,
        severity=models.FindingSeverity.LOW, explanation="old",
        evidence={}, status=models.FindingStatus.DISMISSED,
    )
    db.add_all([repo, doc, claim, event, open_finding, dismissed])
    db.commit()
    return str(repo.id)


def test_findings_default_open_only(client: TestClient, seeded: str) -> None:
    page = client.get(f"/api/repos/{seeded}/findings").json()
    assert page["total"] == 1
    item = page["items"][0]
    assert item["kind"] == "doc_drift"
    assert item["severity"] == "high"
    assert item["claim"]["statement"].startswith("Access tokens")
    assert item["claim"]["anchor"]["start_line"] == 12
    assert item["event"]["external_id"] == "47"
    assert item["evidence"]["diff"] == "-24\n+1"
    # unknown provider keys in evidence are dropped by the schema, not 500s
    assert "extra_provider_key" not in str(item["evidence"])


def test_findings_status_filter(client: TestClient, seeded: str) -> None:
    dismissed = client.get(f"/api/repos/{seeded}/findings?status=dismissed").json()
    assert dismissed["total"] == 1
    assert dismissed["items"][0]["status"] == "dismissed"
    assert dismissed["items"][0]["event"] is None


def test_findings_404(client: TestClient) -> None:
    assert client.get(f"/api/repos/{uuid.uuid4()}/findings").status_code == 404

def test_findings_action_generate_fix_duplicate(client: TestClient, db: Session, seeded: str) -> None:
    repo_id = seeded
    finding = db.scalars(select(models.Finding).where(models.Finding.repo_id == uuid.UUID(repo_id))).first()
    assert finding is not None
    
    # 1. Seed a finding with a fix in GENERATED state
    fix = models.Fix(
        finding=finding,
        status=models.FixStatus.GENERATED,
        patch="-- patch",
    )
    db.add(fix)
    db.commit()

    # 2. Call the endpoint once
    resp1 = client.post(f"/api/findings/{finding.id}/action", json={"action": "generate_fix"})
    assert resp1.status_code == 200
    assert resp1.json()["status"] == "queued"

    # 3. Call the endpoint a second time (simulating a duplicate click/race).
    #    The first click moved the fix GENERATED -> PENDING, so the dedup guard
    #    now reports it as already queued (structured detail: code + message).
    resp2 = client.post(f"/api/findings/{finding.id}/action", json={"action": "generate_fix"})
    assert resp2.status_code == 409
    detail = resp2.json()["detail"]
    assert detail["code"] == "already_queued"
    assert "already queued" in detail["message"]

    # 4. Ensure only ONE job was queued
    jobs = db.scalars(
        select(models.Job)
        .where(models.Job.kind == models.JobKind.GENERATE_FIX)
        .where(models.Job.payload["fix_id"].astext == str(fix.id))
    ).all()
    assert len(jobs) == 1


def test_findings_action_no_proposal(client: TestClient, db: Session, seeded: str) -> None:
    """A finding with no Fix row reports code=no_proposal (not a bare 409)."""
    finding = db.scalars(
        select(models.Finding)
        .where(models.Finding.repo_id == uuid.UUID(seeded))
        .where(models.Finding.status == models.FindingStatus.OPEN)
    ).first()
    assert finding is not None
    assert finding.fix is None  # seeded finding has no remediation proposal

    resp = client.post(f"/api/findings/{finding.id}/action", json={"action": "generate_fix"})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "no_proposal"
    assert "hasn't drafted" in detail["message"]


def test_findings_action_fix_blocked_surfaces_reason(
    client: TestClient, db: Session, seeded: str
) -> None:
    """A FAILED fix (rejected by a safety gate) reports code=fix_blocked and
    surfaces the rejection reason instead of 'no remediation drafted'."""
    finding = db.scalars(
        select(models.Finding)
        .where(models.Finding.repo_id == uuid.UUID(seeded))
        .where(models.Finding.status == models.FindingStatus.OPEN)
    ).first()
    assert finding is not None

    reason = "ungrounded values: replacement contains numbers not present in evidence"
    fix = models.Fix(
        finding=finding,
        status=models.FixStatus.FAILED,
        patch="-- patch",
        error=reason,
    )
    db.add(fix)
    db.commit()

    resp = client.post(f"/api/findings/{finding.id}/action", json={"action": "generate_fix"})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "fix_blocked"
    assert "grounding check" in detail["message"]
    assert detail["reason"] == reason

    # No job was enqueued for a blocked fix.
    jobs = db.scalars(
        select(models.Job)
        .where(models.Job.kind == models.JobKind.GENERATE_FIX)
        .where(models.Job.payload["fix_id"].astext == str(fix.id))
    ).all()
    assert len(jobs) == 0
