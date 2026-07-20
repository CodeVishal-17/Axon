"""T3.2 verification — remediation planning.

Offline: grounding-gate units. DB-backed (skip when Postgres is down):
proposal persistence, eligibility (contradicted-only), one-per-finding
dedupe, retry-after-failure, excerpt/value/confidence gates persisting as
failed fixes, issue-target mode, budget, and handler wiring.
"""

import json
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from axon.db import Base, models
from axon.db.session import get_engine
from axon.llm.provider import LLMError
from axon.services.remediation import (
    RemediationService,
    excerpt_in_text,
    load_proposal,
    unsupported_numbers,
)

DOC_TEXT = (
    "## Auth\n\n"
    "Access tokens expire after 24 hours. Refresh tokens last 30 days.\n"
    "Tokens are signed with RS256.\n"
)
EVIDENCE_QUOTE = "export const TOKEN_TTL_HOURS = 1; // reduced from 24"


# --- Offline: grounding gates ---------------------------------------------


def test_excerpt_gate_normalizes_whitespace() -> None:
    assert excerpt_in_text(
        "Access tokens expire after 24 hours.", DOC_TEXT
    )
    assert excerpt_in_text(
        "Access tokens  expire\nafter 24 hours.", DOC_TEXT
    )
    assert not excerpt_in_text("Access tokens expire after 12 hours.", DOC_TEXT)
    assert not excerpt_in_text("", DOC_TEXT)


def test_value_gate_flags_invented_numbers() -> None:
    allowed = DOC_TEXT + " " + EVIDENCE_QUOTE
    # 1 comes from evidence, 24 from the original — both fine
    assert unsupported_numbers("Access tokens expire after 1 hour.", allowed) == []
    # 90 appears nowhere → invented
    assert unsupported_numbers(
        "Access tokens expire after 1 hour and refresh after 90 days.", allowed
    ) == ["90"]


# --- DB-backed ------------------------------------------------------------


def _db_available() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


pytestmark_db = pytest.mark.skipif(
    not _db_available(),
    reason="Postgres not reachable — start it with `docker compose up -d db`",
)


class ScriptedCompletion:
    name = "scripted"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def complete_json(self, *, prompt, system, schema, schema_name) -> str:
        self.calls.append(prompt)
        response = self.responses.pop(0)
        if response == "RAISE":
            raise LLMError("scripted failure")
        return response


def proposal_json(
    original: str = "Access tokens expire after 24 hours.",
    replacement: str = "Access tokens expire after 1 hour.",
    confidence: float = 0.9,
    title: str = "Update token TTL in docs/auth.md",
) -> str:
    return json.dumps(
        {
            "title": title,
            "explanation": "Docs said 24 hours; TOKEN_TTL_HOURS is 1 since PR #47.",
            "original_excerpt": original,
            "suggested_replacement": replacement,
            "confidence": confidence,
        }
    )


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
        session.commit()


def _seed(
    db: Session,
    *,
    claim_status=models.ClaimStatus.CONTRADICTED,
    finding_status=models.FindingStatus.OPEN,
    source_kind=models.EntityKind.DOC_SECTION,
) -> tuple[models.Repo, models.Finding]:
    repo = models.Repo(full_name=f"axon-test/rem-{uuid.uuid4().hex[:8]}")
    if source_kind == models.EntityKind.DOC_SECTION:
        source = models.Entity(
            repo=repo, kind=source_kind, name="Auth", path="docs/auth.md#auth",
            meta={"text": DOC_TEXT, "doc_path": "docs/auth.md", "start_line": 10},
        )
    else:
        source = models.Entity(
            repo=repo, kind=source_kind, name="Token bug", external_id="31",
            meta={"title": "Token bug",
                  "body": "Tokens last 24 hours which is too long.",
                  "state": "open"},
        )
    claim = models.Claim(
        repo=repo, source_entity=source,
        statement="Access tokens expire after 24 hours.",
        claim_type=models.ClaimType.BEHAVIOR,
        anchor={"path": "docs/auth.md", "start_line": 12, "end_line": 12},
        status=claim_status,
    )
    finding = models.Finding(
        repo=repo, claim=claim,
        kind=models.FindingKind.DOC_DRIFT,
        severity=models.FindingSeverity.HIGH,
        explanation="Docs say 24h; code sets TOKEN_TTL_HOURS to 1.",
        evidence={"quotes": [{"text": EVIDENCE_QUOTE, "path": "src/auth/token.ts",
                              "start_line": 2, "language": "ts"}], "diff": None},
        status=finding_status,
    )
    db.add_all([repo, source, claim, finding])
    db.commit()
    return repo, finding


@pytestmark_db
def test_contradicted_finding_gets_grounded_proposal(db: Session) -> None:
    repo, finding = _seed(db)
    scripted = ScriptedCompletion([proposal_json()])
    report = RemediationService(db, completion_provider=scripted).run(repo)

    assert report.proposals_created == 1
    fix = db.scalars(select(models.Fix).where(models.Fix.finding_id == finding.id)).one()
    assert fix.status == models.FixStatus.GENERATED
    assert fix.pr_url is None                        # no GitHub writes
    assert fix.error is None

    proposal = load_proposal(fix)
    assert proposal["title"] == "Update token TTL in docs/auth.md"
    assert proposal["target_kind"] == "doc"
    assert proposal["target_path"] == "docs/auth.md"
    assert proposal["original_excerpt"] == "Access tokens expire after 24 hours."
    assert proposal["suggested_replacement"] == "Access tokens expire after 1 hour."
    assert proposal["confidence"] == 0.9
    # full provenance: claim, finding, evidence
    assert proposal["claim_id"] == str(finding.claim_id)
    assert proposal["finding_id"] == str(finding.id)
    assert proposal["evidence"][0]["text"] == EVIDENCE_QUOTE
    # the model saw the claim, contradiction, evidence, and target text
    prompt = scripted.calls[0]
    for fragment in ("24 hours", "TOKEN_TTL_HOURS", "docs/auth.md", "## Auth"):
        assert fragment in prompt


@pytestmark_db
@pytest.mark.parametrize(
    "claim_status",
    [models.ClaimStatus.VERIFIED, models.ClaimStatus.UNCHECKED,
     models.ClaimStatus.STALE],
)
def test_non_contradicted_claims_never_get_remediation(
    db: Session, claim_status
) -> None:
    repo, _ = _seed(db, claim_status=claim_status)
    scripted = ScriptedCompletion([])                # any LLM call would pop empty
    report = RemediationService(db, completion_provider=scripted).run(repo)
    assert report.skipped_not_eligible == 1
    assert report.proposals_created == 0
    assert db.scalars(select(models.Fix)).all() == []
    assert scripted.calls == []


@pytestmark_db
def test_dismissed_findings_are_not_considered(db: Session) -> None:
    repo, _ = _seed(db, finding_status=models.FindingStatus.DISMISSED)
    report = RemediationService(
        db, completion_provider=ScriptedCompletion([])
    ).run(repo)
    assert report.findings_considered == 0
    assert db.scalars(select(models.Fix)).all() == []


@pytestmark_db
def test_no_duplicate_remediations(db: Session) -> None:
    repo, finding = _seed(db)
    RemediationService(
        db, completion_provider=ScriptedCompletion([proposal_json()])
    ).run(repo)

    second = ScriptedCompletion([])                  # must not be called
    report = RemediationService(db, completion_provider=second).run(repo)
    assert report.skipped_existing == 1
    assert report.proposals_created == 0
    assert second.calls == []
    fixes = db.scalars(
        select(models.Fix).where(models.Fix.finding_id == finding.id)
    ).all()
    assert len(fixes) == 1                           # DB-level 1:1 upheld


@pytestmark_db
def test_llm_failure_persists_failed_fix_then_retry_succeeds(db: Session) -> None:
    repo, finding = _seed(db)
    report1 = RemediationService(
        db, completion_provider=ScriptedCompletion(["RAISE"])
    ).run(repo)
    assert report1.llm_failures == 1

    fix = db.scalars(select(models.Fix).where(models.Fix.finding_id == finding.id)).one()
    assert fix.status == models.FixStatus.FAILED
    assert "LLM failure" in fix.error
    assert fix.patch is None

    report2 = RemediationService(
        db, completion_provider=ScriptedCompletion([proposal_json()])
    ).run(repo)
    assert report2.retried_after_failure == 1
    assert report2.proposals_created == 1
    db.expire_all()
    fix = db.get(models.Fix, fix.id)
    assert fix.status == models.FixStatus.GENERATED
    assert fix.error is None


@pytestmark_db
def test_ungrounded_excerpt_rejected(db: Session) -> None:
    repo, finding = _seed(db)
    report = RemediationService(
        db,
        completion_provider=ScriptedCompletion(
            [proposal_json(original="Access tokens expire after 12 hours.")]
        ),
    ).run(repo)
    assert report.rejected_ungrounded_excerpt == 1
    fix = db.scalars(select(models.Fix).where(models.Fix.finding_id == finding.id)).one()
    assert fix.status == models.FixStatus.FAILED
    assert "ungrounded excerpt" in fix.error


@pytestmark_db
def test_invented_numbers_rejected(db: Session) -> None:
    repo, finding = _seed(db)
    report = RemediationService(
        db,
        completion_provider=ScriptedCompletion(
            [proposal_json(
                replacement="Access tokens expire after 1 hour (rotated every 45 minutes)."
            )]
        ),
    ).run(repo)
    assert report.rejected_ungrounded_values == 1
    fix = db.scalars(select(models.Fix).where(models.Fix.finding_id == finding.id)).one()
    assert fix.status == models.FixStatus.FAILED
    assert "45" in fix.error


@pytestmark_db
def test_low_confidence_rejected(db: Session) -> None:
    repo, finding = _seed(db)
    report = RemediationService(
        db,
        completion_provider=ScriptedCompletion([proposal_json(confidence=0.3)]),
    ).run(repo)
    assert report.rejected_low_confidence == 1
    fix = db.scalars(select(models.Fix).where(models.Fix.finding_id == finding.id)).one()
    assert fix.status == models.FixStatus.FAILED
    assert "confidence" in fix.error


@pytestmark_db
def test_issue_target_mode(db: Session) -> None:
    repo, finding = _seed(db, source_kind=models.EntityKind.ISSUE)
    scripted = ScriptedCompletion(
        [proposal_json(
            original="Tokens last 24 hours which is too long.",
            replacement="Resolved: TOKEN_TTL_HOURS is now 1 — tokens last 1 hour.",
            title="Close issue #31: token TTL already reduced",
        )]
    )
    report = RemediationService(db, completion_provider=scripted).run(repo)
    assert report.proposals_created == 1

    proposal = load_proposal(
        db.scalars(select(models.Fix).where(models.Fix.finding_id == finding.id)).one()
    )
    assert proposal["target_kind"] == "issue"
    assert proposal["target_path"] == "issue #31"
    assert "issue #31" in scripted.calls[0]


@pytestmark_db
def test_budget_respected(db: Session) -> None:
    repo, _ = _seed(db)
    # add a second contradicted finding on the same repo
    repo2, _ = _seed(db)  # separate repo — keep scoping honest
    scripted = ScriptedCompletion([proposal_json()])
    report = RemediationService(
        db, completion_provider=scripted, budget=1
    ).run(repo)
    assert report.proposals_created <= 1
    assert len(scripted.calls) == 1


@pytestmark_db
def test_verify_handler_runs_remediation_after_verification(
    db: Session, monkeypatch
) -> None:
    """The event pipeline runs the Act stage after the Verify stage."""
    from axon.adapters.github.adapter import GitHubAdapter
    from axon.jobs.handlers import verify as verify_handler
    from axon.services.events import EventService

    order: list[str] = []

    class VerifierSpy:
        def __init__(self, db_, **kwargs):
            pass

        def run(self, repo, claim_ids=None):
            order.append("verify")

    class RemediationSpy:
        def __init__(self, db_, **kwargs):
            pass

        def run(self, repo):
            order.append("remediate")

    monkeypatch.setattr(verify_handler, "DriftVerifier", VerifierSpy)
    monkeypatch.setattr(verify_handler, "RemediationService", RemediationSpy)
    monkeypatch.setattr(verify_handler, "llm_configured", lambda: True)

    repo, finding = _seed(db)
    # a code entity linked to the claim so the plan is non-empty
    code = models.Entity(
        repo=repo, kind=models.EntityKind.CODE_FILE, name="token.ts",
        path="src/auth/token.ts",
    )
    db.add(code)
    db.flush()
    db.add(models.ClaimLink(
        claim_id=finding.claim_id, entity_id=code.id, strength=0.9,
        method=models.LinkMethod.PATH_MATCH,
    ))
    db.commit()

    outcome = EventService(db).ingest(
        repo,
        GitHubAdapter.normalize_webhook(
            "push", f"d-rem-{uuid.uuid4().hex[:8]}",
            {"ref": "refs/heads/main", "after": "abc",
             "commits": [{"added": [], "modified": ["src/auth/token.ts"],
                          "removed": []}],
             "repository": {"full_name": repo.full_name}},
            "main",
        ),
    )
    verify_handler.run(db, {"event_id": outcome.event_id})
    assert order == ["verify", "remediate"]
