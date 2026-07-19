"""T2.4 verification — DriftVerifier with stubbed providers and fetchers.

Covers: verified/contradicted/insufficient transitions, the mandatory-
evidence gate (hallucinated and missing quotes), finding persistence with
the FindingOut evidence contract, dedupe on re-verification, source
gathering for code (fetched) and doc (reassembled) targets, budget +
strongest-link ordering, and no-links skipping.
"""

import json
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from axon.db import Base, models
from axon.db.session import get_engine
from axon.services.verification import DriftVerifier


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

TOKEN_TS = (
    "import { sign } from './jwt';\n"
    "export const TOKEN_TTL_HOURS = 1; // reduced from 24\n"
    "export function issueToken(user) {\n"
    "  return sign(user, { expiresIn: `${TOKEN_TTL_HOURS}h` });\n"
    "}\n"
)

FILES = {"src/auth/token.ts": TOKEN_TS.encode()}


def fetcher(path: str) -> bytes | None:
    return FILES.get(path)


def verdict_json(
    verdict: str,
    quote: str | None,
    path: str | None = "src/auth/token.ts",
    confidence: float = 0.95,
    explanation: str = "docs say 24 hours; code sets TOKEN_TTL_HOURS to 1",
) -> str:
    return json.dumps(
        {
            "verdict": verdict, "confidence": confidence,
            "evidence_quote": quote, "evidence_path": path,
            "explanation": explanation,
        }
    )


class ScriptedCompletion:
    name = "scripted"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def complete_json(self, *, prompt, system, schema, schema_name) -> str:
        self.calls.append(prompt)
        return self.responses.pop(0)


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


def _seed(db: Session, *, source_kind=models.EntityKind.DOC_SECTION,
          link_strength: float = 0.9) -> tuple[models.Repo, models.Claim]:
    repo = models.Repo(full_name=f"axon-test/verify-{uuid.uuid4().hex[:8]}")
    code = models.Entity(
        repo=repo, kind=models.EntityKind.CODE_FILE, name="token.ts",
        path="src/auth/token.ts",
    )
    source = models.Entity(
        repo=repo, kind=source_kind,
        name="Auth" if source_kind == models.EntityKind.DOC_SECTION else "Token bug",
        path="docs/auth.md#auth" if source_kind == models.EntityKind.DOC_SECTION else None,
        external_id=None if source_kind == models.EntityKind.DOC_SECTION else "9",
        meta={},
    )
    claim = models.Claim(
        repo=repo, source_entity=source,
        statement="Access tokens expire after 24 hours.",
        claim_type=models.ClaimType.BEHAVIOR,
        anchor={"path": "docs/auth.md", "start_line": 12, "end_line": 14},
    )
    db.add_all([repo, code, source, claim])
    db.flush()
    db.add(models.ClaimLink(
        claim_id=claim.id, entity_id=code.id, strength=link_strength,
        method=models.LinkMethod.PATH_MATCH,
    ))
    db.commit()
    return repo, claim


def test_verified_flips_status_and_stamps_freshness(db: Session) -> None:
    repo, claim = _seed(db)
    quote = "export const TOKEN_TTL_HOURS = 1; // reduced from 24"
    report = DriftVerifier(
        db,
        completion_provider=ScriptedCompletion(
            [verdict_json("VERIFIED", quote, explanation="matches")]
        ),
        fetch_file=fetcher,
    ).run(repo)

    assert report.verified == 1
    db.expire_all()
    fresh = db.get(models.Claim, claim.id)
    assert fresh.status == models.ClaimStatus.VERIFIED
    assert fresh.last_verified_at is not None
    assert db.scalars(select(models.Finding)).all() == []


def test_contradiction_creates_finding_with_evidence(db: Session) -> None:
    repo, claim = _seed(db)
    quote = "export const TOKEN_TTL_HOURS = 1; // reduced from 24"
    report = DriftVerifier(
        db,
        completion_provider=ScriptedCompletion([verdict_json("CONTRADICTED", quote)]),
        fetch_file=fetcher,
    ).run(repo)

    assert report.contradicted == 1
    assert report.findings_created == 1
    db.expire_all()
    assert db.get(models.Claim, claim.id).status == models.ClaimStatus.CONTRADICTED

    finding = db.scalars(
        select(models.Finding).where(models.Finding.claim_id == claim.id)
    ).one()
    assert finding.kind == models.FindingKind.DOC_DRIFT   # doc-sourced claim
    assert finding.severity == models.FindingSeverity.HIGH  # conf 0.95
    assert finding.status == models.FindingStatus.OPEN
    assert finding.event_id is None                        # at-rest scan
    q = finding.evidence["quotes"][0]
    assert q["text"] == quote
    assert q["path"] == "src/auth/token.ts"
    assert q["start_line"] == 2                            # located in source
    assert "24" in finding.explanation and "1" in finding.explanation
    assert finding.suggested_action.startswith("Update docs/auth.md")


def test_issue_sourced_contradiction_is_stale_issue(db: Session) -> None:
    repo, claim = _seed(db, source_kind=models.EntityKind.ISSUE)
    quote = "export const TOKEN_TTL_HOURS = 1; // reduced from 24"
    DriftVerifier(
        db,
        completion_provider=ScriptedCompletion(
            [verdict_json("CONTRADICTED", quote, confidence=0.75)]
        ),
        fetch_file=fetcher,
    ).run(repo)
    finding = db.scalars(
        select(models.Finding).where(models.Finding.claim_id == claim.id)
    ).one()
    assert finding.kind == models.FindingKind.STALE_ISSUE
    assert finding.severity == models.FindingSeverity.MEDIUM  # conf 0.75


@pytest.mark.parametrize(
    "quote", [None, "", "this text appears nowhere in the source"],
    ids=["missing", "empty", "hallucinated"],
)
def test_evidence_gate_blocks_findings_without_real_quotes(
    db: Session, quote
) -> None:
    repo, claim = _seed(db)
    report = DriftVerifier(
        db,
        completion_provider=ScriptedCompletion([verdict_json("CONTRADICTED", quote)]),
        fetch_file=fetcher,
    ).run(repo)

    assert report.evidence_guard_downgrades == 1
    assert report.contradicted == 0
    assert report.insufficient == 1
    db.expire_all()
    assert db.get(models.Claim, claim.id).status == models.ClaimStatus.UNCHECKED
    assert db.scalars(select(models.Finding)).all() == []  # NEVER without evidence


def test_insufficient_changes_nothing(db: Session) -> None:
    repo, claim = _seed(db)
    report = DriftVerifier(
        db,
        completion_provider=ScriptedCompletion(
            [verdict_json("INSUFFICIENT_EVIDENCE", None, path=None)]
        ),
        fetch_file=fetcher,
    ).run(repo)
    assert report.insufficient == 1
    db.expire_all()
    assert db.get(models.Claim, claim.id).status == models.ClaimStatus.UNCHECKED
    assert db.scalars(select(models.Finding)).all() == []


def test_reverification_updates_single_open_finding(db: Session) -> None:
    repo, claim = _seed(db)
    quote = "export const TOKEN_TTL_HOURS = 1; // reduced from 24"
    DriftVerifier(
        db,
        completion_provider=ScriptedCompletion([verdict_json("CONTRADICTED", quote)]),
        fetch_file=fetcher,
    ).run(repo)

    # explicit re-verify of the same (now contradicted) claim
    report2 = DriftVerifier(
        db,
        completion_provider=ScriptedCompletion(
            [verdict_json("CONTRADICTED", quote, confidence=0.8,
                          explanation="still drifted")]
        ),
        fetch_file=fetcher,
    ).run(repo, claim_ids=[claim.id])

    assert report2.findings_created == 0
    assert report2.findings_updated == 1
    findings = db.scalars(
        select(models.Finding).where(models.Finding.claim_id == claim.id)
    ).all()
    assert len(findings) == 1                      # deduped
    assert findings[0].explanation == "still drifted"
    assert findings[0].severity == models.FindingSeverity.MEDIUM


def test_no_links_skipped_and_budget_orders_by_strength(db: Session) -> None:
    repo = models.Repo(full_name=f"axon-test/verify-{uuid.uuid4().hex[:8]}")
    code = models.Entity(
        repo=repo, kind=models.EntityKind.CODE_FILE, name="token.ts",
        path="src/auth/token.ts",
    )
    source = models.Entity(
        repo=repo, kind=models.EntityKind.DOC_SECTION, name="S",
        path="docs/x.md#s", meta={},
    )
    db.add_all([repo, code, source])
    weak = models.Claim(
        repo=repo, source_entity=source, statement="Weak-linked claim.",
        claim_type=models.ClaimType.BEHAVIOR, anchor={},
    )
    strong = models.Claim(
        repo=repo, source_entity=source, statement="Strong-linked claim.",
        claim_type=models.ClaimType.BEHAVIOR, anchor={},
    )
    orphan = models.Claim(
        repo=repo, source_entity=source, statement="Orphan claim.",
        claim_type=models.ClaimType.BEHAVIOR, anchor={},
    )
    db.add_all([weak, strong, orphan])
    db.flush()
    db.add_all([
        models.ClaimLink(claim_id=weak.id, entity_id=code.id, strength=0.6,
                         method=models.LinkMethod.EMBEDDING),
        models.ClaimLink(claim_id=strong.id, entity_id=code.id, strength=0.95,
                         method=models.LinkMethod.PATH_MATCH),
    ])
    db.commit()

    scripted = ScriptedCompletion(
        [verdict_json("VERIFIED", "export const TOKEN_TTL_HOURS = 1; // reduced from 24")]
    )
    report = DriftVerifier(
        db, completion_provider=scripted, fetch_file=fetcher, budget=1,
    ).run(repo)

    assert report.claims_considered == 3
    assert report.claims_checked == 1              # budget respected
    assert "Strong-linked claim." in scripted.calls[0]  # strongest first
    # orphan wasn't within budget window; verify skip counting separately
    report_full = DriftVerifier(
        db,
        completion_provider=ScriptedCompletion(
            [verdict_json("INSUFFICIENT_EVIDENCE", None, path=None)]
        ),
        fetch_file=fetcher,
    ).run(repo)
    assert report_full.skipped_no_links == 1       # the orphan


def test_doc_target_reassembles_sections_without_fetching(db: Session) -> None:
    repo = models.Repo(full_name=f"axon-test/verify-{uuid.uuid4().hex[:8]}")
    doc = models.Entity(
        repo=repo, kind=models.EntityKind.DOC, name="ops.md", path="docs/ops.md",
    )
    section = models.Entity(
        repo=repo, kind=models.EntityKind.DOC_SECTION, name="Tuning",
        path="docs/ops.md#tuning",
        meta={"text": "The worker polls every 2 seconds.", "start_line": 5,
              "doc_path": "docs/ops.md"},
    )
    src = models.Entity(
        repo=repo, kind=models.EntityKind.DOC_SECTION, name="S",
        path="README.md#s", meta={},
    )
    claim = models.Claim(
        repo=repo, source_entity=src,
        statement="The worker polls every 2 seconds.",
        claim_type=models.ClaimType.BEHAVIOR, anchor={},
    )
    db.add_all([repo, doc, section, src, claim])
    db.flush()
    db.add(models.ClaimLink(
        claim_id=claim.id, entity_id=doc.id, strength=0.9,
        method=models.LinkMethod.PATH_MATCH,
    ))
    db.commit()

    calls: list[str] = []

    def spy_fetcher(path: str) -> bytes | None:
        calls.append(path)
        return None

    scripted = ScriptedCompletion(
        [verdict_json("VERIFIED", "The worker polls every 2 seconds.",
                      path="docs/ops.md")]
    )
    report = DriftVerifier(
        db, completion_provider=scripted, fetch_file=spy_fetcher,
    ).run(repo)

    assert report.verified == 1
    assert calls == []                             # doc text came from the DB
    assert "docs/ops.md" in scripted.calls[0]
    assert "The worker polls every 2 seconds." in scripted.calls[0]
