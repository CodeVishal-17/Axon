"""Repository management endpoints — the frontend's door into the pipeline.

Routers stay thin (architecture §2): validate, call services/queue, shape
the response. Ingestion NEVER happens in a request handler — POST /repos
only enqueues; the worker does the work.

Credential handling: the GitHub token is stored in ``repo.settings`` (JSONB,
server-side only). Every response model here enumerates its fields
explicitly and none includes ``settings`` — the token cannot leak through
this API, and it is never logged.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from axon.db.models import (
    Entity,
    EntityKind,
    IngestStatus,
    Job,
    JobKind,
    JobStatus,
    Repo,
)
from axon.db.session import get_db
from axon.jobs import queue

router = APIRouter(prefix="/api", tags=["repos"])

_FULL_NAME_RE = re.compile(r"^[\w.-]+/[\w.-]+$")

# --- Schemas -------------------------------------------------------------


class RepoCreate(BaseModel):
    """Connect a repository. ``token`` is a fine-grained PAT scoped to the
    repo; it is persisted server-side and never returned by any endpoint."""

    full_name: str = Field(examples=["owner/repository"])
    token: str | None = None


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: JobKind
    status: JobStatus
    attempts: int
    error: str | None
    run_at: datetime
    created_at: datetime


class RepoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str
    provider: str
    default_branch: str
    ingest_status: IngestStatus
    last_ingested_sha: str | None
    created_at: datetime
    updated_at: datetime


class RepoDetail(RepoOut):
    """Repo + pipeline visibility: latest job (progress/errors) and entity
    counts by kind (empty until the first ingest lands)."""

    entity_counts: dict[str, int]
    latest_job: JobOut | None


class EntityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: EntityKind
    name: str
    path: str | None
    external_id: str | None
    content_hash: str | None
    meta: dict
    updated_at: datetime


class EntityPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[EntityOut]


# --- Helpers -------------------------------------------------------------


def _get_repo(db: Session, repo_id: uuid.UUID) -> Repo:
    repo = db.get(Repo, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="repository not found")
    return repo


def _entity_counts(db: Session, repo: Repo) -> dict[str, int]:
    rows = db.execute(
        select(Entity.kind, func.count())
        .where(Entity.repo_id == repo.id)
        .group_by(Entity.kind)
    ).all()
    return {kind.value: count for kind, count in rows}


def _latest_job(db: Session, repo: Repo) -> Job | None:
    return db.scalars(
        select(Job)
        .where(Job.payload["repo_id"].astext == str(repo.id))
        .order_by(Job.created_at.desc())
        .limit(1)
    ).first()


def _detail(db: Session, repo: Repo) -> RepoDetail:
    job = _latest_job(db, repo)
    return RepoDetail(
        **RepoOut.model_validate(repo).model_dump(),
        entity_counts=_entity_counts(db, repo),
        latest_job=JobOut.model_validate(job) if job else None,
    )


# --- Endpoints -----------------------------------------------------------


@router.post("/repos", response_model=RepoDetail)
def connect_repo(body: RepoCreate, db: Session = Depends(get_db)) -> RepoDetail:
    """Connect a repository and enqueue its first ingest.

    Idempotent on ``full_name``: reconnecting an existing repo updates the
    stored token (if provided) and re-enqueues ingestion only when the
    previous one failed — a healthy repo is not re-ingested by accident.
    """
    if not _FULL_NAME_RE.match(body.full_name):
        raise HTTPException(
            status_code=422, detail="full_name must look like 'owner/repository'"
        )

    repo = db.scalars(
        select(Repo).where(
            Repo.provider == "github", Repo.full_name == body.full_name
        )
    ).first()

    if repo is None:
        repo = Repo(
            full_name=body.full_name,
            settings={"token": body.token} if body.token else {},
        )
        db.add(repo)
        db.commit()
        queue.enqueue(db, JobKind.INGEST, {"repo_id": str(repo.id)})
    else:
        if body.token:
            repo.settings = {**repo.settings, "token": body.token}
            db.commit()
        if repo.ingest_status == IngestStatus.FAILED:
            repo.ingest_status = IngestStatus.PENDING
            db.commit()
            queue.enqueue(db, JobKind.INGEST, {"repo_id": str(repo.id)})

    return _detail(db, repo)


@router.get("/repos/{repo_id}", response_model=RepoDetail)
def get_repo(repo_id: uuid.UUID, db: Session = Depends(get_db)) -> RepoDetail:
    """Repository metadata + ingest status + latest job + entity counts."""
    return _detail(db, _get_repo(db, repo_id))


@router.get("/repos/{repo_id}/entities", response_model=EntityPage)
def list_entities(
    repo_id: uuid.UUID,
    kind: EntityKind | None = None,
    q: str | None = Query(default=None, max_length=200, description="search name/path"),
    sort: Literal["name", "path", "kind", "updated_at"] = "path",
    order: Literal["asc", "desc"] = "asc",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> EntityPage:
    """Paginated entity listing with kind filter, sort, and name/path search.

    Bulky meta payloads (doc/section text, issue bodies) are stripped from
    list responses — detail-level content ships with the feed/claims APIs.
    """
    repo = _get_repo(db, repo_id)

    conditions = [Entity.repo_id == repo.id]
    if kind is not None:
        conditions.append(Entity.kind == kind)
    if q:
        pattern = f"%{q}%"
        conditions.append(or_(Entity.name.ilike(pattern), Entity.path.ilike(pattern)))

    total = db.scalar(select(func.count()).select_from(Entity).where(*conditions))

    sort_column = {
        "name": Entity.name,
        "path": Entity.path,
        "kind": Entity.kind,
        "updated_at": Entity.updated_at,
    }[sort]
    sort_expr = sort_column.desc() if order == "desc" else sort_column.asc()

    rows = db.scalars(
        select(Entity)
        .where(*conditions)
        .order_by(sort_expr, Entity.id)  # id tiebreak: stable pagination
        .limit(limit)
        .offset(offset)
    ).all()

    items = []
    for entity in rows:
        out = EntityOut.model_validate(entity)
        out.meta = {
            k: v for k, v in out.meta.items() if k not in ("text", "body")
        }
        items.append(out)

    return EntityPage(total=total or 0, limit=limit, offset=offset, items=items)
