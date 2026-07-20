"""All Axon database models — the nine tables from the architecture.

One module on purpose: the schema is the product's core asset and reads best
as a single page. Grouped in loop order:

    repos → entities/edges → claims/claim_links → events → findings → fixes
    (+ jobs, the standalone queue table)

Schema decisions
----------------
* **UUID primary keys**, generated client-side (``uuid4``) so objects have
  identity before flush and IDs can appear in logs/URLs without guessing.
* **Non-native enums** (VARCHAR + CHECK, ``native_enum=False``): adding a
  value is a one-line model change + cheap migration, versus ``ALTER TYPE``
  ceremony with native Postgres enums. Worth it for a fast-moving schema.
* **JSONB** for payload-shaped data (anchors, event payloads, evidence,
  settings, metadata): schema-on-read where the shape is owned by prompts
  and adapters, columns where the database enforces invariants.
* **Vector(1536)** on ``claims.embedding`` — dimension of OpenAI
  ``text-embedding-3-small``; the Anthropic-compatible path uses the same
  dim via the provider abstraction. Nullable: claims exist before embedding.
* **Timestamps are server-side** (``func.now()``) so API and worker clocks
  can never disagree with the database.
* ``claim_links`` uses a **composite PK** (claim_id, entity_id): a link is a
  fact, not an entity — the PK itself prevents duplicate links.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    DDL,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from axon.db.base import Base

# Dimension of claims.embedding; must match the provider layer's embed().
EMBEDDING_DIM = 1536

# pgvector must exist before any table with a Vector column is created.
# Hooked on metadata so plain Base.metadata.create_all() works on a fresh DB.
event.listen(
    Base.metadata,
    "before_create",
    DDL("CREATE EXTENSION IF NOT EXISTS vector"),
)


def _enum(enum_cls: type[enum.Enum], name: str) -> Enum:
    """Enum column storing the .value strings (VARCHAR + CHECK, not native)."""
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=32,
        values_callable=lambda e: [member.value for member in e],
    )


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    """created_at / updated_at maintained by the database."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# --- Enums ---------------------------------------------------------------


class IngestStatus(str, enum.Enum):
    PENDING = "pending"
    INGESTING = "ingesting"
    READY = "ready"
    FAILED = "failed"


class EntityKind(str, enum.Enum):
    CODE_FILE = "code_file"
    SYMBOL = "symbol"
    DOC = "doc"
    DOC_SECTION = "doc_section"
    ISSUE = "issue"
    PULL_REQUEST = "pull_request"
    PERSON = "person"


class EdgeKind(str, enum.Enum):
    CONTAINS = "contains"
    REFERENCES = "references"
    OWNS = "owns"
    MENTIONS = "mentions"


class ClaimType(str, enum.Enum):
    BEHAVIOR = "behavior"
    ARCHITECTURE = "architecture"
    PROCESS = "process"
    STATUS = "status"


class ClaimStatus(str, enum.Enum):
    UNCHECKED = "unchecked"
    VERIFIED = "verified"
    STALE = "stale"
    CONTRADICTED = "contradicted"


class LinkMethod(str, enum.Enum):
    PATH_MATCH = "path_match"
    SYMBOL_MATCH = "symbol_match"
    EMBEDDING = "embedding"
    LLM = "llm"


class EventKind(str, enum.Enum):
    PUSH = "push"
    PR_MERGED = "pr_merged"
    ISSUE_CLOSED = "issue_closed"
    SIMULATED = "simulated"


class FindingKind(str, enum.Enum):
    DOC_DRIFT = "doc_drift"
    STALE_ISSUE = "stale_issue"
    CONTRADICTION = "contradiction"
    SILO = "silo"


class FindingSeverity(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FindingStatus(str, enum.Enum):
    OPEN = "open"
    ACTIONED = "actioned"
    DISMISSED = "dismissed"


class FixStatus(str, enum.Enum):
    PENDING = "pending"
    GENERATED = "generated"
    PR_OPENED = "pr_opened"
    FAILED = "failed"


class JobKind(str, enum.Enum):
    INGEST = "ingest"
    VERIFY = "verify"
    GENERATE_FIX = "generate_fix"


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


# --- Models --------------------------------------------------------------


class Repo(TimestampMixin, Base):
    """A connected repository — the tenant boundary; every hot query is
    scoped by repo_id."""

    __tablename__ = "repos"
    __table_args__ = (
        UniqueConstraint("provider", "full_name", name="provider_full_name"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    provider: Mapped[str] = mapped_column(String(32), default="github", nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    default_branch: Mapped[str] = mapped_column(String(255), default="main", nullable=False)
    ingest_status: Mapped[IngestStatus] = mapped_column(
        _enum(IngestStatus, "ingest_status"), default=IngestStatus.PENDING, nullable=False
    )
    last_ingested_sha: Mapped[str | None] = mapped_column(String(64))
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    entities: Mapped[list[Entity]] = relationship(
        back_populates="repo", cascade="all, delete-orphan", passive_deletes=True
    )
    claims: Mapped[list[Claim]] = relationship(
        back_populates="repo", cascade="all, delete-orphan", passive_deletes=True
    )
    events: Mapped[list[Event]] = relationship(
        back_populates="repo", cascade="all, delete-orphan", passive_deletes=True
    )
    findings: Mapped[list[Finding]] = relationship(
        back_populates="repo", cascade="all, delete-orphan", passive_deletes=True
    )


class Entity(TimestampMixin, Base):
    """Unified node table: code files, symbols, docs, doc sections, issues,
    PRs, people. Uniformity is what keeps the graph and adapters cheap."""

    __tablename__ = "entities"
    __table_args__ = (
        Index("ix_entities_repo_kind", "repo_id", "kind"),
        Index("ix_entities_repo_path", "repo_id", "path"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    repo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[EntityKind] = mapped_column(_enum(EntityKind, "entity_kind"), nullable=False)
    # Repo-relative path for code/doc entities; None for issues/PRs/people.
    path: Mapped[str | None] = mapped_column(String(1024))
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    # Provider-side identifier (issue number, login, ...) for sync/dedup.
    external_id: Mapped[str | None] = mapped_column(String(255), index=True)
    # Hash of source content; unchanged hash => skip re-ingest/re-extract.
    content_hash: Mapped[str | None] = mapped_column(String(64))
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    repo: Mapped[Repo] = relationship(back_populates="entities")
    claims: Mapped[list[Claim]] = relationship(
        back_populates="source_entity",
        foreign_keys="Claim.source_entity_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    claim_links: Mapped[list[ClaimLink]] = relationship(
        back_populates="entity", cascade="all, delete-orphan", passive_deletes=True
    )


class Edge(TimestampMixin, Base):
    """Graph edges between entities (contains / references / owns /
    mentions). Ownership weight from commit history makes bus factor a
    query, not a subsystem."""

    __tablename__ = "edges"
    __table_args__ = (
        UniqueConstraint("src_entity_id", "dst_entity_id", "kind", name="src_dst_kind"),
        Index("ix_edges_repo_kind", "repo_id", "kind"),
        Index("ix_edges_dst", "dst_entity_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    repo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE"), nullable=False
    )
    src_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    dst_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[EdgeKind] = mapped_column(_enum(EdgeKind, "edge_kind"), nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)

    src_entity: Mapped[Entity] = relationship(foreign_keys=[src_entity_id])
    dst_entity: Mapped[Entity] = relationship(foreign_keys=[dst_entity_id])


class Claim(TimestampMixin, Base):
    """An atomic, verifiable belief extracted from a doc/issue — the core
    asset. A chunk can't be true or false; a claim can."""

    __tablename__ = "claims"
    __table_args__ = (
        Index("ix_claims_repo_status", "repo_id", "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    repo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE"), nullable=False
    )
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    claim_type: Mapped[ClaimType] = mapped_column(_enum(ClaimType, "claim_type"), nullable=False)
    # The doc_section/issue entity this claim was extracted from.
    source_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Source location: {"path": ..., "start_line": ..., "end_line": ...}.
    # Mandatory in practice — a finding without a quotable location is an
    # assertion, not evidence. JSONB because belief sources beyond GitHub
    # (Notion blocks, Slack threads) anchor differently.
    anchor: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[ClaimStatus] = mapped_column(
        _enum(ClaimStatus, "claim_status"), default=ClaimStatus.UNCHECKED, nullable=False
    )
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))

    repo: Mapped[Repo] = relationship(back_populates="claims")
    source_entity: Mapped[Entity] = relationship(
        back_populates="claims", foreign_keys=[source_entity_id]
    )
    links: Mapped[list[ClaimLink]] = relationship(
        back_populates="claim", cascade="all, delete-orphan", passive_deletes=True
    )
    findings: Mapped[list[Finding]] = relationship(
        back_populates="claim", cascade="all, delete-orphan", passive_deletes=True
    )


class ClaimLink(TimestampMixin, Base):
    """Claim ↔ code-entity binding. The drift engine's hottest query is
    'claims linked to these changed entities' — hence the entity_id index.
    Composite PK: one link per (claim, entity), enforced by the schema."""

    __tablename__ = "claim_links"
    __table_args__ = (
        Index("ix_claim_links_entity", "entity_id"),
    )

    claim_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("claims.id", ondelete="CASCADE"), primary_key=True
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True
    )
    strength: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    method: Mapped[LinkMethod] = mapped_column(_enum(LinkMethod, "link_method"), nullable=False)

    claim: Mapped[Claim] = relationship(back_populates="links")
    entity: Mapped[Entity] = relationship(back_populates="claim_links")


class Event(TimestampMixin, Base):
    """Append-only reality log: pushes, merged PRs, closed issues — real or
    simulated (identical downstream path)."""

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("repo_id", "external_id", name="uq_events_repo_external"),
        Index("ix_events_repo_kind", "repo_id", "kind"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    repo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[EventKind] = mapped_column(_enum(EventKind, "event_kind"), nullable=False)
    # Provider delivery id (webhook GUID / commit sha) for idempotency.
    external_id: Mapped[str | None] = mapped_column(String(255), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    repo: Mapped[Repo] = relationship(back_populates="events")
    findings: Mapped[list[Finding]] = relationship(back_populates="event")


class Finding(TimestampMixin, Base):
    """A detected contradiction/drift — what the Truth Feed renders.
    event_id is provenance ('this finding exists because of that PR');
    SET NULL on event deletion because a finding outlives its trigger."""

    __tablename__ = "findings"
    __table_args__ = (
        Index("ix_findings_repo_status", "repo_id", "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    repo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE"), nullable=False
    )
    claim_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("claims.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"), index=True
    )
    kind: Mapped[FindingKind] = mapped_column(_enum(FindingKind, "finding_kind"), nullable=False)
    severity: Mapped[FindingSeverity] = mapped_column(
        _enum(FindingSeverity, "finding_severity"), nullable=False
    )
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    # Quoted evidence spans + diff hunks: {"quotes": [...], "diff": ...}.
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    suggested_action: Mapped[str | None] = mapped_column(Text)
    status: Mapped[FindingStatus] = mapped_column(
        _enum(FindingStatus, "finding_status"), default=FindingStatus.OPEN, nullable=False
    )

    repo: Mapped[Repo] = relationship(back_populates="findings")
    claim: Mapped[Claim] = relationship(back_populates="findings")
    event: Mapped[Event | None] = relationship(back_populates="findings")
    fix: Mapped[Fix | None] = relationship(
        back_populates="finding",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Fix(TimestampMixin, Base):
    """Audit trail of generated corrective PRs. 1:1 with finding (unique FK)."""

    __tablename__ = "fixes"

    id: Mapped[uuid.UUID] = _uuid_pk()
    finding_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("findings.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    patch: Mapped[str | None] = mapped_column(Text)
    pr_url: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[FixStatus] = mapped_column(
        _enum(FixStatus, "fix_status"), default=FixStatus.PENDING, nullable=False
    )
    error: Mapped[str | None] = mapped_column(Text)

    finding: Mapped[Finding] = relationship(back_populates="fix")


class Job(TimestampMixin, Base):
    """Postgres-backed job queue. Standalone (no FKs): jobs may reference
    rows in payload, but a queue must be able to outlive/precede its data.
    The (status, run_at) index serves the worker's SKIP LOCKED poll."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_status_run_at", "status", "run_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[JobKind] = mapped_column(_enum(JobKind, "job_kind"), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        _enum(JobStatus, "job_status"), default=JobStatus.PENDING, nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
