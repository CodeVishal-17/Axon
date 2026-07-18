"""T0.4 smoke test — the roadmap's acceptance drill:

Insert Repository → Entity → Claim → Finding (plus a ClaimLink, since the
link table is on the drift engine's hot path), then read the full
relationship chain back through the ORM and verify it.

Runs against real Postgres (docker compose up -d db). Skips cleanly when no
database is reachable so `pytest` stays green on a checkout without Docker.
All writes happen inside a connection-level transaction that is rolled back
— repeated runs leave no residue.
"""

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from axon.db import Base, models
from axon.db.session import get_engine


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
def engine():
    engine = get_engine()
    # Idempotent bootstrap; also creates the pgvector extension via the
    # before_create hook in models.py.
    Base.metadata.create_all(engine)
    return engine


def test_relationship_chain_roundtrip(engine) -> None:
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        # --- Insert the chain -----------------------------------------
        repo = models.Repo(
            full_name=f"axon-test/smoke-{uuid.uuid4().hex[:8]}",
            ingest_status=models.IngestStatus.READY,
        )
        doc_section = models.Entity(
            repo=repo,
            kind=models.EntityKind.DOC_SECTION,
            path="docs/auth.md",
            name="Token lifetimes",
            content_hash="abc123",
        )
        code_file = models.Entity(
            repo=repo,
            kind=models.EntityKind.CODE_FILE,
            path="src/auth/token.ts",
            name="token.ts",
        )
        claim = models.Claim(
            repo=repo,
            source_entity=doc_section,
            statement="Access tokens expire after 24 hours.",
            claim_type=models.ClaimType.BEHAVIOR,
            anchor={"path": "docs/auth.md", "start_line": 12, "end_line": 14},
            status=models.ClaimStatus.CONTRADICTED,
            confidence=0.92,
            embedding=[0.0] * models.EMBEDDING_DIM,
        )
        link = models.ClaimLink(
            claim=claim,
            entity=code_file,
            strength=0.9,
            method=models.LinkMethod.PATH_MATCH,
        )
        event = models.Event(
            repo=repo,
            kind=models.EventKind.PR_MERGED,
            external_id="pr-47",
            payload={"number": 47},
        )
        finding = models.Finding(
            repo=repo,
            claim=claim,
            event=event,
            kind=models.FindingKind.DOC_DRIFT,
            severity=models.FindingSeverity.HIGH,
            explanation="Docs say 24h token expiry; code sets 1h since PR #47.",
            evidence={"quotes": ["const TOKEN_TTL_HOURS = 1"]},
        )
        session.add_all([repo, doc_section, code_file, claim, link, event, finding])
        session.flush()

        # --- Read the chain back cold (expire ORM state, requery) -----
        session.expire_all()
        fetched_repo = session.scalars(
            select(models.Repo).where(models.Repo.id == repo.id)
        ).one()

        # Repo → Entities
        kinds = {e.kind for e in fetched_repo.entities}
        assert kinds == {models.EntityKind.DOC_SECTION, models.EntityKind.CODE_FILE}

        # Entity (doc) → Claim
        fetched_doc = next(
            e for e in fetched_repo.entities if e.kind == models.EntityKind.DOC_SECTION
        )
        assert len(fetched_doc.claims) == 1
        fetched_claim = fetched_doc.claims[0]
        assert fetched_claim.statement == "Access tokens expire after 24 hours."
        assert fetched_claim.status == models.ClaimStatus.CONTRADICTED
        assert fetched_claim.anchor["start_line"] == 12
        assert fetched_claim.embedding is not None
        assert len(fetched_claim.embedding) == models.EMBEDDING_DIM

        # Claim → ClaimLink → code entity (the drift engine's hot path)
        assert len(fetched_claim.links) == 1
        assert fetched_claim.links[0].entity.path == "src/auth/token.ts"
        assert fetched_claim.links[0].method == models.LinkMethod.PATH_MATCH

        # Claim → Finding → Event provenance
        assert len(fetched_claim.findings) == 1
        fetched_finding = fetched_claim.findings[0]
        assert fetched_finding.kind == models.FindingKind.DOC_DRIFT
        assert fetched_finding.severity == models.FindingSeverity.HIGH
        assert fetched_finding.event is not None
        assert fetched_finding.event.external_id == "pr-47"
        assert fetched_finding.repo.id == fetched_repo.id

        # Server-side timestamps arrived
        assert fetched_claim.created_at is not None
        assert fetched_finding.updated_at is not None
    finally:
        session.close()
        transaction.rollback()
        connection.close()
