"""Repository ingestion — populates the knowledge graph. No LLM calls.

Belief → Verify loop position: this is the "gather reality + beliefs" stage.
It writes entities (code files, docs, doc sections, issues, PRs, people),
containment and ownership edges, and content hashes. Claim extraction (T2.2)
and verification (T2.4) build strictly on top of what's persisted here.

Idempotency model: entities are keyed by a natural key — (kind, path) for
file-derived entities, (kind, external_id) for provider-side ones. On
re-ingest, an unchanged ``content_hash`` skips the row entirely; changed
content updates in place; file-derived entities missing from the new
snapshot are pruned (cascading their claims — the file is gone, so beliefs
anchored to it are moot). The service is the single writer per repo (jobs
are serialized per repo by the worker), so app-level upserts are safe
without DB-level unique constraints on the natural keys.
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from axon.adapters.base import BeliefSource, RealitySource, sha256_bytes, sha256_text
from axon.db.models import (
    Edge,
    EdgeKind,
    Entity,
    EntityKind,
    IngestStatus,
    Repo,
)

logger = logging.getLogger("axon.services.ingestion")

# --- Caps (architecture §8: predictable cost and ingest time) ------------

MAX_FILES = 2000
MAX_FILE_BYTES = 500_000
KNOWLEDGE_DOC_LIMIT = 200
COMMIT_LIMIT = 100

# --- Ignore rules --------------------------------------------------------

IGNORED_DIRS = {
    "node_modules", "vendor", "vendors", "third_party", "dist", "build",
    "out", "target", "__pycache__", ".venv", "venv", "env", "coverage",
    ".next", ".nuxt", ".terraform",
}
IGNORED_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Cargo.lock", "Gemfile.lock", "composer.lock", "go.sum", "uv.lock",
}
BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".svg",
    ".pdf", ".zip", ".gz", ".tar", ".tgz", ".7z", ".rar",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".mp4", ".mov", ".avi", ".webm", ".wav",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".class", ".jar", ".pyc",
    ".db", ".sqlite", ".parquet", ".ipynb",
}
MARKDOWN_EXTENSIONS = {".md", ".mdx", ".markdown"}

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def should_ignore(path: str) -> bool:
    """Path-based exclusion: vendored, generated, hidden, lockfiles, binary
    extensions, minified assets."""
    parts = path.split("/")
    basename = parts[-1]
    dirs = parts[:-1]

    if any(d in IGNORED_DIRS for d in dirs):
        return True
    if any(part.startswith(".") for part in parts):  # hidden files AND dirs
        return True
    if basename in IGNORED_FILES:
        return True
    lower = basename.lower()
    if any(lower.endswith(ext) for ext in BINARY_EXTENSIONS):
        return True
    if lower.endswith((".min.js", ".min.css", ".map")):
        return True
    return False


def looks_binary(content: bytes) -> bool:
    """Content sniff for binaries hiding behind innocent extensions."""
    return b"\x00" in content[:8000]


def is_markdown(path: str) -> bool:
    return any(path.lower().endswith(ext) for ext in MARKDOWN_EXTENSIONS)


# --- Markdown sectioning -------------------------------------------------


@dataclass(frozen=True)
class MarkdownSection:
    title: str
    anchor: str
    text: str
    start_line: int  # 1-indexed, inclusive
    end_line: int


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "section"


def split_markdown(text: str) -> list[MarkdownSection]:
    """Split by ATX headings, ignoring headings inside code fences.

    Content before the first heading becomes an "(overview)" section, so no
    text is ever lost. Anchors are de-duplicated with numeric suffixes so
    section paths stay unique within a document.
    """
    lines = text.splitlines()
    boundaries: list[tuple[int, str]] = []  # (line index, title)
    in_fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING_RE.match(line)
        if match:
            boundaries.append((i, match.group(2).strip()))

    sections: list[MarkdownSection] = []
    used_anchors: Counter[str] = Counter()

    def add(title: str, start: int, end: int) -> None:
        body = "\n".join(lines[start:end]).strip()
        if not body:
            return
        anchor = _slugify(title)
        used_anchors[anchor] += 1
        if used_anchors[anchor] > 1:
            anchor = f"{anchor}-{used_anchors[anchor]}"
        sections.append(
            MarkdownSection(
                title=title, anchor=anchor, text=body,
                start_line=start + 1, end_line=end,
            )
        )

    first_heading = boundaries[0][0] if boundaries else len(lines)
    add("(overview)", 0, first_heading)
    for idx, (line_no, title) in enumerate(boundaries):
        end = boundaries[idx + 1][0] if idx + 1 < len(boundaries) else len(lines)
        add(title, line_no, end)
    return sections


# --- Ingestion service ---------------------------------------------------


@dataclass
class IngestReport:
    created: Counter = field(default_factory=Counter)
    updated: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)
    deleted: Counter = field(default_factory=Counter)
    files_ignored: int = 0
    edges_written: int = 0
    duration_s: float = 0.0

    def summary(self) -> str:
        def fmt(counter: Counter) -> str:
            return ", ".join(f"{k}={v}" for k, v in sorted(counter.items())) or "none"

        return (
            f"created: {fmt(self.created)}\n"
            f"updated: {fmt(self.updated)}\n"
            f"skipped (unchanged): {fmt(self.skipped)}\n"
            f"deleted (pruned): {fmt(self.deleted)}\n"
            f"files ignored: {self.files_ignored}\n"
            f"edges written: {self.edges_written}\n"
            f"duration: {self.duration_s:.1f}s"
        )


class IngestionService:
    """Orchestrates one full/incremental ingest of a repository.

    The adapter is anything satisfying both RealitySource and BeliefSource —
    GitHubAdapter in production, a fake in tests.
    """

    def __init__(self, db: Session, adapter: RealitySource | BeliefSource) -> None:
        self.db = db
        self.adapter = adapter
        self.report = IngestReport()
        self._existing: dict[tuple[EntityKind, str], Entity] = {}
        self._seen: set[tuple[EntityKind, str]] = set()

    # -- entity upsert machinery -----------------------------------------

    def _key(self, entity: Entity) -> tuple[EntityKind, str]:
        return (entity.kind, entity.path or entity.external_id or entity.name)

    def _load_existing(self, repo: Repo) -> None:
        from sqlalchemy.orm import load_only
        rows = self.db.scalars(
            select(Entity)
            .where(Entity.repo_id == repo.id)
            .options(load_only(Entity.kind, Entity.path, Entity.external_id, Entity.name, Entity.content_hash))
        ).all()
        self._existing = {self._key(e): e for e in rows}

    def _upsert(
        self,
        repo: Repo,
        *,
        kind: EntityKind,
        name: str,
        content_hash: str | None,
        path: str | None = None,
        external_id: str | None = None,
        meta: dict | None = None,
    ) -> Entity:
        key = (kind, path or external_id or name)
        self._seen.add(key)
        existing = self._existing.get(key)
        if existing is not None:
            if existing.content_hash == content_hash and content_hash is not None:
                self.report.skipped[kind.value] += 1
                return existing
            existing.name = name
            existing.content_hash = content_hash
            existing.meta = meta or {}
            self.report.updated[kind.value] += 1
            return existing

        entity = Entity(
            repo=repo, kind=kind, name=name, path=path,
            external_id=external_id, content_hash=content_hash, meta=meta or {},
        )
        self.db.add(entity)
        self._existing[key] = entity
        self.report.created[kind.value] += 1
        return entity

    # -- pipeline stages --------------------------------------------------

    def run(self, repo: Repo) -> IngestReport:
        started = time.monotonic()
        repo.ingest_status = IngestStatus.INGESTING
        self.db.commit()
        try:
            info = self.adapter.fetch_repo_info()
            repo.external_id = info.external_id
            repo.default_branch = info.default_branch

            self._load_existing(repo)
            self._ingest_files(repo)
            self._ingest_knowledge_docs(repo)
            self._ingest_ownership(repo)
            self._prune_missing_files()

            repo.last_ingested_sha = info.head_sha
            repo.ingest_status = IngestStatus.READY
            self.db.commit()
        except Exception:
            self.db.rollback()
            repo.ingest_status = IngestStatus.FAILED
            self.db.commit()
            raise
        self.report.duration_s = time.monotonic() - started
        logger.info("ingest %s finished\n%s", repo.full_name, self.report.summary())
        return self.report

    def _ingest_files(self, repo: Repo) -> None:
        count = 0
        for file in self.adapter.iter_files(MAX_FILE_BYTES):
            if should_ignore(file.path):
                self.report.files_ignored += 1
                continue
            if not is_markdown(file.path) and looks_binary(file.content):
                self.report.files_ignored += 1
                continue
            if count >= MAX_FILES:
                logger.warning("file cap %d reached; ignoring the rest", MAX_FILES)
                self.report.files_ignored += 1
                continue
            count += 1

            if is_markdown(file.path):
                self._ingest_markdown(repo, file.path, file.content)
            else:
                self._upsert(
                    repo,
                    kind=EntityKind.CODE_FILE,
                    name=file.path.rsplit("/", 1)[-1],
                    path=file.path,
                    content_hash=sha256_bytes(file.content),
                    meta={"size": len(file.content)},
                )

    def _ingest_markdown(self, repo: Repo, path: str, content: bytes) -> None:
        text = content.decode("utf-8", errors="replace")
        doc_hash = sha256_bytes(content)
        doc = self._upsert(
            repo,
            kind=EntityKind.DOC,
            name=path.rsplit("/", 1)[-1],
            path=path,
            content_hash=doc_hash,
            meta={"sections": 0},
        )
        sections = split_markdown(text)
        doc.meta = {**doc.meta, "sections": len(sections)}
        for section in sections:
            section_entity = self._upsert(
                repo,
                kind=EntityKind.DOC_SECTION,
                name=section.title,
                path=f"{path}#{section.anchor}",
                content_hash=sha256_text(section.text),
                meta={
                    "text": section.text,
                    "start_line": section.start_line,
                    "end_line": section.end_line,
                    "doc_path": path,
                },
            )
            self._link(repo, doc, section_entity, EdgeKind.CONTAINS, 1.0)

    def _ingest_knowledge_docs(self, repo: Repo) -> None:
        kind_map = {"issue": EntityKind.ISSUE, "pull_request": EntityKind.PULL_REQUEST}
        for kdoc in self.adapter.iter_knowledge_docs(KNOWLEDGE_DOC_LIMIT):
            self._upsert(
                repo,
                kind=kind_map[kdoc.kind],
                name=kdoc.title[:500] or f"#{kdoc.external_id}",
                external_id=kdoc.external_id,
                content_hash=kdoc.content_hash,
                meta={
                    "title": kdoc.title,
                    "body": kdoc.body,
                    "url": kdoc.url,
                    "state": kdoc.state,
                    "author": kdoc.author,
                    "updated_at": kdoc.updated_at.isoformat()
                    if kdoc.updated_at
                    else None,
                },
            )

    def _ingest_ownership(self, repo: Repo) -> None:
        """People + `owns` edges. Weight = commits touching the file within
        the recent-commit window — the bus-factor raw signal, no git blame
        required (architecture §8)."""
        counts: dict[tuple[str, str], int] = defaultdict(int)
        for commit in self.adapter.iter_commits(COMMIT_LIMIT):
            if not commit.author_login:
                continue
            for path in commit.files:
                if not should_ignore(path):
                    counts[(commit.author_login, path)] += 1

        persons: dict[str, Entity] = {}
        for (login, _), _ in counts.items():
            if login not in persons:
                persons[login] = self._upsert(
                    repo,
                    kind=EntityKind.PERSON,
                    name=login,
                    external_id=login,
                    content_hash=sha256_text(login),
                )

        for (login, path), weight in counts.items():
            target = self._existing.get(
                (EntityKind.DOC, path)
            ) or self._existing.get((EntityKind.CODE_FILE, path))
            if target is None:
                continue  # file no longer exists or is ignored
            self._link(repo, persons[login], target, EdgeKind.OWNS, float(weight))

    def _link(
        self, repo: Repo, src: Entity, dst: Entity, kind: EdgeKind, weight: float
    ) -> None:
        """Edge upsert against the (src, dst, kind) natural key."""
        self.db.flush()  # ensure entity ids exist
        edge = self.db.scalars(
            select(Edge).where(
                Edge.src_entity_id == src.id,
                Edge.dst_entity_id == dst.id,
                Edge.kind == kind,
            )
        ).first()
        if edge is None:
            self.db.add(
                Edge(
                    repo_id=repo.id, src_entity_id=src.id, dst_entity_id=dst.id,
                    kind=kind, weight=weight,
                )
            )
            self.report.edges_written += 1
        elif edge.weight != weight:
            edge.weight = weight
            self.report.edges_written += 1

    def _prune_missing_files(self) -> None:
        """Delete file-derived entities absent from this snapshot. Their
        claims cascade — a belief anchored to a deleted file is moot."""
        file_kinds = {EntityKind.CODE_FILE, EntityKind.DOC, EntityKind.DOC_SECTION}
        for key, entity in list(self._existing.items()):
            if key[0] in file_kinds and key not in self._seen:
                self.report.deleted[key[0].value] += 1
                self.db.delete(entity)
                del self._existing[key]
