"""T4.1 verification — deterministic PR generation.

Offline: pure patch-application units. DB-backed: the full flow against a
fake write adapter — happy path, idempotency (status gate + PR adoption +
commit skip), stale/ambiguous rejection, issue-target skip, action
endpoint, and the job handler. No LLM anywhere near this module.
"""

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from axon.db import Base, models
from axon.db.session import get_engine
from axon.main import create_app
from axon.services.pr import (
    GitHubPRService,
    apply_replacement,
    branch_name_for,
    render_pr_body,
)

DOC_ORIGINAL = (
    "# Auth\n\n"
    "Access tokens expire after 24 hours. Refresh tokens last 30 days.\n"
    "Tokens are signed with RS256.\n"
)
EXCERPT = "Access tokens expire after 24 hours."
REPLACEMENT = "Access tokens expire after 1 hour."


# --- Pure patch application -----------------------------------------------


def test_apply_replacement_exact_and_deterministic() -> None:
    out1, how1 = apply_replacement(DOC_ORIGINAL, EXCERPT, REPLACEMENT)
    out2, how2 = apply_replacement(DOC_ORIGINAL, EXCERPT, REPLACEMENT)
    assert out1 == out2 and how1 == how2 == "exact"          # deterministic
    assert REPLACEMENT in out1 and EXCERPT not in out1
    assert "Refresh tokens last 30 days." in out1            # rest untouched


def test_apply_replacement_whitespace_flexible() -> None:
    wrapped = DOC_ORIGINAL.replace(
        EXCERPT, "Access tokens expire\n   after 24 hours."
    )
    out, how = apply_replacement(wrapped, EXCERPT, REPLACEMENT)
    assert out is not None and how == "whitespace-flexible"
    assert REPLACEMENT in out


def test_apply_replacement_crlf_normalized() -> None:
    out, how = apply_replacement(
        DOC_ORIGINAL.replace("\n", "\r\n"), EXCERPT, REPLACEMENT
    )
    assert out is not None and how == "exact"


def test_apply_replacement_stale_and_ambiguous() -> None:
    out, reason = apply_replacement(
        DOC_ORIGINAL.replace("24", "12"), EXCERPT, REPLACEMENT
    )
    assert out is None and reason.startswith("stale")

    doubled = DOC_ORIGINAL + "\n" + EXCERPT + "\n"
    out, reason = apply_replacement(doubled, EXCERPT, REPLACEMENT)
    assert out is None and reason.startswith("ambiguous")


# --- DB-backed -------------------------------------------------------------


def _db_available() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(
    not _db_available(),
    reason="Postgres not reachable — start it with `docker compose up -d db`",
)


class FakeWriteAdapter:
    """In-memory GitHub write surface. Records every call."""

    def __init__(self, files: dict[str, str], default_branch: str = "main") -> None:
        self.branches: dict[str, dict[str, tuple[bytes, str]]] = {
            default_branch: {
                path: (content.encode(), f"sha-{path}-0")
                for path, content in files.items()
            }
        }
        self.default_branch = default_branch
        self.pulls: dict[str, str] = {}  # head branch -> url
        self.calls: list[tuple] = []

    def get_branch_head(self, branch: str) -> str:
        self.calls.append(("get_branch_head", branch))
        return f"head-{branch}"

    def branch_exists(self, branch: str) -> bool:
        self.calls.append(("branch_exists", branch))
        return branch in self.branches

    def create_branch(self, branch: str, from_sha: str) -> None:
        self.calls.append(("create_branch", branch, from_sha))
        self.branches[branch] = dict(self.branches[self.default_branch])

    def fetch_file_with_sha(self, path: str, ref: str | None = None):
        self.calls.append(("fetch_file_with_sha", path, ref))
        return self.branches.get(ref or self.default_branch, {}).get(path)

    def put_file(self, path, content, message, branch, sha) -> None:
        self.calls.append(("put_file", path, branch, message.splitlines()[0]))
        version = sum(1 for c in self.calls if c[0] == "put_file")
        self.branches[branch][path] = (content, f"sha-{path}-{version}")

    def find_pull(self, head_branch: str) -> str | None:
        self.calls.append(("find_pull", head_branch))
        return self.pulls.get(head_branch)

    def create_pull(self, title, body, head, base) -> str:
        self.calls.append(("create_pull", title, head, base))
        url = f"https://github.com/acme/app/pull/{len(self.pulls) + 100}"
        self.pulls[head] = url
        return url

    def count(self, name: str) -> int:
        return sum(1 for c in self.calls if c[0] == name)


@pytest.fixture()
def db():
    engine = get_engine()
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
        session.rollback()
        for repo in session.scalars(
            select(models.Repo).where(models.Repo.full_name.like("axon-test/%"))
        ):
            session.delete(repo)
        session.execute(text(
            "DELETE FROM jobs WHERE payload->>'fix_id' IS NOT NULL "
            "AND payload->>'fix_id' NOT IN (SELECT id::text FROM fixes)"
        ))
        session.commit()


def _proposal(target_kind: str = "doc", excerpt: str = EXCERPT) -> dict:
    return {
        "prompt_version": "v1",
        "title": "Update token TTL in docs/auth.md",
        "explanation": "Docs said 24 hours; TOKEN_TTL_HOURS is 1 since PR #47.",
        "target_kind": target_kind,
        "target_path": "docs/auth.md" if target_kind == "doc" else "issue #31",
        "original_excerpt": excerpt,
        "suggested_replacement": REPLACEMENT,
        "confidence": 0.9,
        "claim_id": str(uuid.uuid4()),
        "claim_statement": "Access tokens expire after 24 hours.",
        "finding_id": "…",
        "evidence": [{"text": "export const TOKEN_TTL_HOURS = 1;",
                      "path": "src/auth/token.ts", "start_line": 2,
                      "language": "ts"}],
    }


def _seed(db: Session, *, fix_status=models.FixStatus.GENERATED,
          target_kind: str = "doc", excerpt: str = EXCERPT):
    repo = models.Repo(full_name=f"axon-test/pr-{uuid.uuid4().hex[:8]}")
    source = models.Entity(
        repo=repo, kind=models.EntityKind.DOC_SECTION, name="Auth",
        path="docs/auth.md#auth", meta={"text": DOC_ORIGINAL},
    )
    claim = models.Claim(
        repo=repo, source_entity=source,
        statement="Access tokens expire after 24 hours.",
        claim_type=models.ClaimType.BEHAVIOR,
        anchor={"path": "docs/auth.md"}, status=models.ClaimStatus.CONTRADICTED,
    )
    finding = models.Finding(
        repo=repo, claim=claim, kind=models.FindingKind.DOC_DRIFT,
        severity=models.FindingSeverity.HIGH, explanation="drift",
        evidence={"quotes": []},
    )
    db.add_all([repo, source, claim, finding])
    db.flush()
    proposal = _proposal(target_kind=target_kind, excerpt=excerpt)
    proposal["finding_id"] = str(finding.id)
    fix = models.Fix(
        finding_id=finding.id, status=fix_status,
        patch=json.dumps(proposal, sort_keys=True),
    )
    db.add(fix)
    db.commit()
    return repo, finding, fix


@requires_db
def test_happy_path_one_commit_one_pr(db: Session) -> None:
    repo, finding, fix = _seed(db)
    adapter = FakeWriteAdapter({"docs/auth.md": DOC_ORIGINAL})
    outcome = GitHubPRService(db, adapter=adapter).open_pr_for_fix(fix.id)

    assert outcome.status == "opened"
    assert outcome.pr_url.startswith("https://github.com/")
    assert adapter.count("put_file") == 1                    # one commit
    assert adapter.count("create_pull") == 1                 # one PR
    branch = branch_name_for(fix)
    assert branch.startswith("axon/fix-")
    patched = adapter.branches[branch]["docs/auth.md"][0].decode()
    assert REPLACEMENT in patched and EXCERPT not in patched

    db.expire_all()
    fix = db.get(models.Fix, fix.id)
    assert fix.status == models.FixStatus.PR_OPENED
    assert fix.pr_url == outcome.pr_url                      # URL persisted
    assert db.get(models.Finding, finding.id).status == models.FindingStatus.ACTIONED

    # PR body is a deterministic render of the record
    body = render_pr_body(json.loads(fix.patch))
    assert "TOKEN_TTL_HOURS = 1" in body                     # evidence
    assert "Access tokens expire after 24 hours." in body    # claim
    assert str(finding.id) in body                           # provenance
    assert "docs/auth.md" in body                            # target


@requires_db
def test_identical_fix_never_opens_duplicate_pr(db: Session) -> None:
    repo, finding, fix = _seed(db)
    adapter = FakeWriteAdapter({"docs/auth.md": DOC_ORIGINAL})
    service = GitHubPRService(db, adapter=adapter)
    first = service.open_pr_for_fix(fix.id)

    second = service.open_pr_for_fix(fix.id)                 # status gate
    assert second.status == "already_open"
    assert second.pr_url == first.pr_url
    assert adapter.count("create_pull") == 1
    assert adapter.count("put_file") == 1


@requires_db
def test_lost_url_recovery_adopts_existing_pr(db: Session) -> None:
    """Crash after PR creation but before persistence: the recovery probe
    finds the PR by the deterministic branch and adopts it."""
    repo, finding, fix = _seed(db)
    adapter = FakeWriteAdapter({"docs/auth.md": DOC_ORIGINAL})
    branch = branch_name_for(fix)
    adapter.pulls[branch] = "https://github.com/acme/app/pull/7"

    outcome = GitHubPRService(db, adapter=adapter).open_pr_for_fix(fix.id)
    assert outcome.status == "already_open"
    assert outcome.pr_url == "https://github.com/acme/app/pull/7"
    assert adapter.count("create_pull") == 0                 # no duplicate
    assert adapter.count("put_file") == 0
    db.expire_all()
    assert db.get(models.Fix, fix.id).pr_url == outcome.pr_url


@requires_db
def test_crash_resume_skips_recommit(db: Session) -> None:
    """Crash between commit and PR: branch exists with the patch applied —
    resume must not commit again, only open the PR."""
    repo, finding, fix = _seed(db)
    adapter = FakeWriteAdapter({"docs/auth.md": DOC_ORIGINAL})
    branch = branch_name_for(fix)
    patched, _ = apply_replacement(DOC_ORIGINAL, EXCERPT, REPLACEMENT)
    adapter.branches[branch] = {
        "docs/auth.md": (patched.encode(), "sha-prior")
    }

    outcome = GitHubPRService(db, adapter=adapter).open_pr_for_fix(fix.id)
    assert outcome.status == "opened"
    assert adapter.count("put_file") == 0                    # one commit ever
    assert adapter.count("create_pull") == 1
    assert adapter.count("create_branch") == 0


@requires_db
def test_stale_excerpt_rejected_without_github_writes(db: Session) -> None:
    repo, finding, fix = _seed(db)
    drifted_again = DOC_ORIGINAL.replace("24 hours", "12 hours")
    adapter = FakeWriteAdapter({"docs/auth.md": drifted_again})

    outcome = GitHubPRService(db, adapter=adapter).open_pr_for_fix(fix.id)
    assert outcome.status == "stale"
    assert adapter.count("create_branch") == 0
    assert adapter.count("put_file") == 0
    assert adapter.count("create_pull") == 0
    db.expire_all()
    fix = db.get(models.Fix, fix.id)
    assert fix.status == models.FixStatus.FAILED
    assert fix.error.startswith("stale")
    assert db.get(models.Finding, finding.id).status == models.FindingStatus.OPEN


@requires_db
def test_missing_file_and_ambiguous_excerpt_are_stale(db: Session) -> None:
    repo, finding, fix = _seed(db)
    adapter = FakeWriteAdapter({})                           # file gone
    outcome = GitHubPRService(db, adapter=adapter).open_pr_for_fix(fix.id)
    assert outcome.status == "stale"
    assert "no longer exists" in outcome.reason

    repo2, finding2, fix2 = _seed(db)
    doubled = DOC_ORIGINAL + "\nSee above: " + EXCERPT + "\n"
    adapter2 = FakeWriteAdapter({"docs/auth.md": doubled})
    outcome2 = GitHubPRService(db, adapter=adapter2).open_pr_for_fix(fix2.id)
    assert outcome2.status == "stale"
    db.expire_all()
    assert "ambiguous" in db.get(models.Fix, fix2.id).error


@requires_db
def test_issue_target_and_failed_fix_are_not_prable(db: Session) -> None:
    repo, finding, fix = _seed(db, target_kind="issue")
    adapter = FakeWriteAdapter({"docs/auth.md": DOC_ORIGINAL})
    outcome = GitHubPRService(db, adapter=adapter).open_pr_for_fix(fix.id)
    assert outcome.status == "skipped_issue_target"
    assert adapter.calls == []                               # zero GitHub calls
    db.expire_all()
    assert db.get(models.Fix, fix.id).status == models.FixStatus.GENERATED

    repo2, finding2, fix2 = _seed(db, fix_status=models.FixStatus.FAILED)
    outcome2 = GitHubPRService(
        db, adapter=FakeWriteAdapter({"docs/auth.md": DOC_ORIGINAL})
    ).open_pr_for_fix(fix2.id)
    assert outcome2.status == "invalid_state"


@requires_db
def test_action_endpoint_enqueues_and_guards(db: Session) -> None:
    Base.metadata.create_all(get_engine())
    with TestClient(create_app()) as client:
        repo, finding, fix = _seed(db)
        response = client.post(
            f"/api/findings/{finding.id}/action", json={"action": "generate_fix"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "queued"
        job = db.get(models.Job, uuid.UUID(body["job_id"]))
        assert job.kind == models.JobKind.GENERATE_FIX
        assert job.payload == {"fix_id": str(fix.id)}

        # PR already open → no re-queue, URL returned
        fix.status = models.FixStatus.PR_OPENED
        fix.pr_url = "https://github.com/acme/app/pull/9"
        db.commit()
        again = client.post(
            f"/api/findings/{finding.id}/action", json={"action": "generate_fix"}
        ).json()
        assert again["status"] == "already_open"
        assert again["pr_url"] == fix.pr_url

        # finding without a proposal → 409
        repo2, finding2, fix2 = _seed(db)
        db.delete(fix2)
        db.commit()
        assert client.post(
            f"/api/findings/{finding2.id}/action", json={"action": "generate_fix"}
        ).status_code == 409

        # dismiss
        dismissed = client.post(
            f"/api/findings/{finding2.id}/action", json={"action": "dismiss"}
        ).json()
        assert dismissed["finding_status"] == "dismissed"


@requires_db
def test_generate_fix_job_handler_runs_service(db: Session, monkeypatch) -> None:
    from axon.jobs.handlers import generate_fix as handler

    seen: list[uuid.UUID] = []

    class ServiceSpy:
        def __init__(self, db_, adapter=None):
            pass

        def open_pr_for_fix(self, fix_id):
            seen.append(fix_id)
            from axon.services.pr import PROutcome
            return PROutcome(status="opened", pr_url="https://x/pr/1")

    monkeypatch.setattr(handler, "GitHubPRService", ServiceSpy)
    fix_id = uuid.uuid4()
    handler.run(db, {"fix_id": str(fix_id)})
    assert seen == [fix_id]
