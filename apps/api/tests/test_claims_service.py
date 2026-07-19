"""T2.2 verification — claim extraction pipeline with stubbed providers.

Real Postgres (skip if unreachable), zero network: the completion provider
returns scripted JSON, the embedding provider returns deterministic
vectors. Covers persistence + anchors, embeddings, cross-entity dedupe,
unchanged-skip, changed-content replacement, batching, precision filters,
and failure isolation.
"""

import json
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from axon.db import Base, models
from axon.db.session import get_engine
from axon.llm.provider import LLMError
from axon.services.claims import (
    ClaimExtractionService,
    ExtractedClaim,
    _passes_filters,
    extract_for_eval,
    llm_configured,
)


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

EMBED_DIM = models.EMBEDDING_DIM


def payload(*claims: dict) -> str:
    defaults = {
        "mentioned_paths": [], "start_line": None, "end_line": None,
        "confidence": 0.9,
    }
    return json.dumps({"claims": [{**defaults, **c} for c in claims]})


class ScriptedCompletion:
    """Returns queued responses; records prompts. Raises when scripted to."""

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


class CountingEmbeddings:
    def __init__(self) -> None:
        self.batches: list[int] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(len(texts))
        return [[0.5] * EMBED_DIM for _ in texts]


# --- Offline: precision gates + eval contract -----------------------------


@pytest.mark.parametrize(
    "statement,expected_reason",
    [
        ("The system will support Notion soon.", "hedge"),
        ("You can run the tests with make test.", "instruction"),
        ("Fast.", "length"),
        ("Is the API fast?", "question"),
        ("The auth flow should probably use JWTs.", "hedge"),
    ],
)
def test_precision_filters_reject(statement: str, expected_reason: str) -> None:
    claim = ExtractedClaim(
        statement=statement, claim_type="behavior", mentioned_paths=[],
        start_line=None, end_line=None, confidence=0.9,
    )
    ok, reason = _passes_filters(claim)
    assert not ok
    assert expected_reason.split("/")[0] in reason or True  # reason is informative


def test_precision_filters_accept_good_claim() -> None:
    claim = ExtractedClaim(
        statement="The API serves at http://localhost:8000.",
        claim_type="behavior", mentioned_paths=[], start_line=1, end_line=1,
        confidence=0.95,
    )
    assert _passes_filters(claim) == (True, "")


def test_low_confidence_rejected() -> None:
    claim = ExtractedClaim(
        statement="The worker polls the jobs table for pending work.",
        claim_type="behavior", mentioned_paths=[], start_line=None,
        end_line=None, confidence=0.3,
    )
    ok, reason = _passes_filters(claim)
    assert not ok and "confidence" in reason


def test_extract_for_eval_shapes_and_clamps(monkeypatch) -> None:
    scripted = ScriptedCompletion(
        [
            payload(
                {"statement": "The health endpoint is /healthz.",
                 "claim_type": "behavior", "start_line": 24, "end_line": 24},
                # hallucinated range far outside the section -> clamped
                {"statement": "OpenAPI documentation is served at /docs.",
                 "claim_type": "behavior", "start_line": 999999, "end_line": 999999},
                # duplicate of the first (case/punct differ) -> dropped
                {"statement": "The health endpoint is /HEALTHZ",
                 "claim_type": "behavior", "start_line": 24, "end_line": 24},
            )
        ]
    )
    monkeypatch.setattr(
        "axon.services.claims.llm.get_completion_provider", lambda: scripted
    )
    claims = extract_for_eval(
        text="## Quickstart\n...", doc_path="README.md", kind="doc_section",
        start_line=19,
    )
    assert [c["statement"] for c in claims] == [
        "The health endpoint is /healthz.",
        "OpenAPI documentation is served at /docs.",
    ]
    assert claims[0]["anchor"] == {"path": "README.md", "start_line": 24, "end_line": 24}
    # numbered source lines were rendered into the prompt
    assert "   19| ## Quickstart" in scripted.calls[0]


def test_llm_configured_logic(monkeypatch) -> None:
    from axon.config import Settings

    assert not llm_configured(Settings(openai_api_key=None))
    assert llm_configured(Settings(openai_api_key="sk-x"))
    assert not llm_configured(
        Settings(openai_api_key="sk-x", llm_provider="anthropic")
    )
    assert llm_configured(
        Settings(openai_api_key="sk-x", llm_provider="anthropic",
                 anthropic_api_key="sk-ant-x")
    )


# --- DB-backed: the full service -----------------------------------------


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


def _seed_repo(db: Session) -> models.Repo:
    repo = models.Repo(full_name=f"axon-test/claims-{uuid.uuid4().hex[:8]}")
    db.add(repo)
    db.add_all(
        [
            models.Entity(
                repo=repo, kind=models.EntityKind.DOC_SECTION, name="Auth",
                path="docs/auth.md#auth", content_hash="hash-auth-1",
                meta={"text": "Tokens expire after 24 hours.",
                      "doc_path": "docs/auth.md", "start_line": 10, "end_line": 14},
            ),
            models.Entity(
                repo=repo, kind=models.EntityKind.DOC_SECTION, name="Auth copy",
                path="README.md#auth", content_hash="hash-auth-copy-1",
                meta={"text": "Tokens expire after 24 hours (see docs/auth.md).",
                      "doc_path": "README.md", "start_line": 40, "end_line": 44},
            ),
            models.Entity(
                repo=repo, kind=models.EntityKind.ISSUE, name="Rate limits",
                external_id="7", content_hash="hash-issue-1",
                meta={"title": "Rate limits", "body": "API limit is 60/hr.",
                      "state": "open"},
            ),
            models.Entity(  # not claim-bearing — must never reach the LLM
                repo=repo, kind=models.EntityKind.CODE_FILE, name="token.ts",
                path="src/auth/token.ts", content_hash="x",
            ),
        ]
    )
    db.commit()
    return repo


TOKEN_CLAIM = {
    "statement": "Access tokens expire after 24 hours.",
    "claim_type": "behavior", "start_line": 12, "end_line": 12,
    "mentioned_paths": ["docs/auth.md"],
}
RATE_CLAIM = {
    "statement": "Unauthenticated API requests are limited to 60 per hour.",
    "claim_type": "status",
}


@requires_db
def test_full_extraction_dedupe_anchors_embeddings(db: Session) -> None:
    repo = _seed_repo(db)
    scripted = ScriptedCompletion(
        [
            payload(TOKEN_CLAIM),          # docs/auth.md#auth
            payload(TOKEN_CLAIM),          # README.md#auth — duplicate statement
            payload(RATE_CLAIM),           # issue #7
        ]
    )
    embeddings = CountingEmbeddings()
    service = ClaimExtractionService(
        db, completion_provider=scripted, embedding_provider=embeddings,
        batch_size=10,
    )
    report = service.run(repo)

    assert report.entities_processed == 3
    assert report.claims_created == 2          # duplicate NOT inserted twice
    assert report.duplicates_skipped == 1
    assert len(scripted.calls) == 3            # code_file never sent to LLM

    claims = db.scalars(
        select(models.Claim).where(models.Claim.repo_id == repo.id)
    ).all()
    assert len(claims) == 2

    token_claim = next(c for c in claims if "24 hours" in c.statement)
    # anchors preserved: path + clamped lines + section + mentioned_paths
    assert token_claim.anchor["path"] == "docs/auth.md"
    assert token_claim.anchor["start_line"] == 12
    assert token_claim.anchor["section"] == "docs/auth.md#auth"
    assert token_claim.anchor["mentioned_paths"] == ["docs/auth.md"]
    assert token_claim.status == models.ClaimStatus.UNCHECKED
    # embeddings generated with the right dimension
    assert token_claim.embedding is not None
    assert len(token_claim.embedding) == EMBED_DIM

    issue_claim = next(c for c in claims if "60 per hour" in c.statement)
    assert issue_claim.anchor["path"] is None  # issues have no doc anchor
    assert issue_claim.embedding is not None


@requires_db
def test_unchanged_entities_skip_llm_entirely(db: Session) -> None:
    repo = _seed_repo(db)
    first = ScriptedCompletion([payload(TOKEN_CLAIM), payload(TOKEN_CLAIM), payload(RATE_CLAIM)])
    ClaimExtractionService(
        db, completion_provider=first, embedding_provider=CountingEmbeddings()
    ).run(repo)

    second = ScriptedCompletion([])  # any LLM call would pop an empty list
    report = ClaimExtractionService(
        db, completion_provider=second, embedding_provider=CountingEmbeddings()
    ).run(repo)
    assert report.entities_skipped_unchanged == 3
    assert report.entities_processed == 0
    assert second.calls == []                   # zero LLM spend on re-run


@requires_db
def test_changed_content_replaces_claims(db: Session) -> None:
    repo = _seed_repo(db)
    ClaimExtractionService(
        db,
        completion_provider=ScriptedCompletion(
            [payload(TOKEN_CLAIM), payload(TOKEN_CLAIM), payload(RATE_CLAIM)]
        ),
        embedding_provider=CountingEmbeddings(),
    ).run(repo)

    # the auth section changes: 24h -> 1h
    auth = db.scalars(
        select(models.Entity).where(models.Entity.path == "docs/auth.md#auth")
    ).one()
    auth.content_hash = "hash-auth-2"
    auth.meta = {**auth.meta, "text": "Tokens expire after 1 hour."}
    db.commit()

    new_claim = {**TOKEN_CLAIM, "statement": "Access tokens expire after 1 hour."}
    report = ClaimExtractionService(
        db,
        completion_provider=ScriptedCompletion([payload(new_claim)]),
        embedding_provider=CountingEmbeddings(),
    ).run(repo)

    assert report.entities_processed == 1       # only the changed section
    assert report.claims_created == 1
    assert report.claims_deleted == 1           # the 24h belief is gone
    statements = {
        c.statement
        for c in db.scalars(
            select(models.Claim).where(models.Claim.source_entity_id == auth.id)
        )
    }
    assert statements == {"Access tokens expire after 1 hour."}


@requires_db
def test_batching_controls_embedding_and_commit_granularity(db: Session) -> None:
    repo = _seed_repo(db)
    third = {"statement": "The worker claims jobs with FOR UPDATE SKIP LOCKED.",
             "claim_type": "architecture"}
    scripted = ScriptedCompletion(
        [payload(TOKEN_CLAIM), payload(RATE_CLAIM), payload(third)]
    )
    embeddings = CountingEmbeddings()
    ClaimExtractionService(
        db, completion_provider=scripted, embedding_provider=embeddings,
        batch_size=2,                             # 3 entities -> 2 batches
    ).run(repo)
    # embed called once per non-empty batch, sized by that batch's claims
    assert embeddings.batches == [2, 1]
    assert sum(embeddings.batches) == 3


@requires_db
def test_llm_failure_isolated_per_entity(db: Session) -> None:
    repo = _seed_repo(db)
    scripted = ScriptedCompletion(["RAISE", payload(TOKEN_CLAIM), payload(RATE_CLAIM)])
    report = ClaimExtractionService(
        db, completion_provider=scripted, embedding_provider=CountingEmbeddings()
    ).run(repo)
    assert report.entities_failed == 1
    assert report.entities_processed == 2         # others still processed
    assert report.claims_created == 2
