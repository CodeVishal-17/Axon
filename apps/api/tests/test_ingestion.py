"""T1.2 verification.

Unit tests (offline): ignore rules, binary sniffing, markdown sectioning.
Service tests (real Postgres, skip if unreachable): full ingest with a fake
adapter, idempotent re-run, change detection, pruning, exclusion of ignored
files. Network-free by design — the live path is scripts/ingest_smoke.py.
"""

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from axon.adapters.base import CommitInfo, KnowledgeDoc, RepoFile, RepoInfo, sha256_text
from axon.db import Base, models
from axon.db.session import get_engine
from axon.services import ingestion
from axon.services.ingestion import IngestionService, should_ignore, split_markdown

# --- Unit: ignore rules --------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "node_modules/react/index.js",
        "vendor/lib/util.go",
        "dist/bundle.js",
        ".github/workflows/ci.yml",
        ".env",
        "src/.hidden/file.py",
        "package-lock.json",
        "apps/web/yarn.lock",
        "logo.png",
        "docs/diagram.PNG",
        "app.min.js",
        "static/app.css.map",
        "__pycache__/mod.cpython-312.pyc",
    ],
)
def test_ignored_paths(path: str) -> None:
    assert should_ignore(path)


@pytest.mark.parametrize(
    "path",
    ["src/auth/token.ts", "README.md", "docs/setup.md", "Makefile", "go.mod"],
)
def test_kept_paths(path: str) -> None:
    assert not should_ignore(path)


def test_binary_sniff() -> None:
    assert ingestion.looks_binary(b"\x89PNG\x0d\x0a\x1a\x0a\x00\x00")
    assert not ingestion.looks_binary(b"plain old text\nwith lines\n")


# --- Unit: markdown sectioning -------------------------------------------


MARKDOWN = """Intro paragraph before any heading.

# Setup

Install things.

```bash
# this fenced comment is NOT a heading
make install
```

## Configuration

Set env vars.

# Setup

Duplicate heading title.
"""


def test_split_markdown_sections() -> None:
    sections = split_markdown(MARKDOWN)
    titles = [s.title for s in sections]
    anchors = [s.anchor for s in sections]

    assert titles == ["(overview)", "Setup", "Configuration", "Setup"]
    # fence content stayed inside its section, not split on
    assert "make install" in sections[1].text
    # duplicate anchors get suffixes
    assert anchors == ["overview", "setup", "configuration", "setup-2"]
    # line ranges are sane and ordered
    assert sections[0].start_line == 1
    assert all(s.start_line <= s.end_line for s in sections)


def test_split_markdown_no_headings() -> None:
    sections = split_markdown("just some prose\nwith no headings")
    assert len(sections) == 1
    assert sections[0].title == "(overview)"


# --- Service tests (real Postgres) ---------------------------------------


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


class FakeAdapter:
    """In-memory RealitySource + BeliefSource."""

    def __init__(self, files, docs=(), commits=(), head_sha="sha-1"):
        self.files = dict(files)
        self.docs = list(docs)
        self.commits = list(commits)
        self.head_sha = head_sha

    def fetch_repo_info(self) -> RepoInfo:
        return RepoInfo("1", "axon-test/fake", "main", self.head_sha)

    def iter_files(self, max_file_bytes: int):
        for path, content in self.files.items():
            if len(content) <= max_file_bytes:
                yield RepoFile(path=path, content=content)

    def iter_knowledge_docs(self, limit: int):
        yield from self.docs[:limit]

    def iter_commits(self, limit: int):
        yield from self.commits[:limit]


FILES = {
    "src/auth/token.ts": b"export const TOKEN_TTL_HOURS = 24;\n",
    "src/main.ts": b"console.log('hi');\n",
    "README.md": b"Intro.\n\n# Auth\n\nTokens expire after 24 hours.\n",
    # must all be excluded:
    "node_modules/x/index.js": b"junk",
    "package-lock.json": b"{}",
    "assets/logo.bin": b"\x00\x01\x02binary",
}

DOCS = [
    KnowledgeDoc(
        external_id="1", kind="issue", title="Token bug", body="It breaks.",
        url="https://example/1", author="alice", state="open",
        updated_at=None, content_hash=sha256_text("Token bug\nIt breaks.\nopen"),
    ),
    KnowledgeDoc(
        external_id="2", kind="pull_request", title="Fix tokens", body="Done.",
        url="https://example/2", author="bob", state="closed",
        updated_at=None, content_hash=sha256_text("Fix tokens\nDone.\nclosed"),
    ),
]

COMMITS = [
    CommitInfo("c1", "alice", None, ("src/auth/token.ts", "README.md")),
    CommitInfo("c2", "alice", None, ("src/auth/token.ts",)),
    CommitInfo("c3", "bob", None, ("src/main.ts", "node_modules/x/index.js")),
]


@pytest.fixture()
def db() -> Session:
    engine = get_engine()
    Base.metadata.create_all(engine)
    session = Session(engine)
    yield session
    # cleanup: remove the repos this test created (cascades everything)
    session.rollback()
    for repo in session.scalars(
        select(models.Repo).where(models.Repo.full_name.like("axon-test/%"))
    ):
        session.delete(repo)
    session.commit()
    session.close()


def _make_repo(db: Session) -> models.Repo:
    repo = models.Repo(full_name=f"axon-test/fake-{uuid.uuid4().hex[:8]}")
    db.add(repo)
    db.commit()
    return repo


@requires_db
def test_full_ingest_and_idempotent_rerun(db: Session) -> None:
    repo = _make_repo(db)
    adapter = FakeAdapter(FILES, DOCS, COMMITS)

    # --- first run: everything created --------------------------------
    report1 = IngestionService(db, adapter).run(repo)
    assert report1.created["code_file"] == 2
    assert report1.created["doc"] == 1
    assert report1.created["doc_section"] == 2  # (overview) + Auth
    assert report1.created["issue"] == 1
    assert report1.created["pull_request"] == 1
    assert report1.created["person"] == 2
    assert report1.files_ignored == 3  # node_modules, lockfile, binary
    assert repo.ingest_status == models.IngestStatus.READY
    assert repo.last_ingested_sha == "sha-1"
    # contains: doc→2 sections; owns: alice→2 files, bob→1 file
    assert report1.edges_written == 5

    # ignored paths must not exist as entities
    paths = set(
        db.scalars(
            select(models.Entity.path).where(models.Entity.repo_id == repo.id)
        )
    )
    assert not any(p and ("node_modules" in p or "lock" in p or ".bin" in p) for p in paths)

    # --- second run: everything skipped, nothing written ----------------
    report2 = IngestionService(db, adapter).run(repo)
    assert not report2.created
    assert not report2.updated
    assert not report2.deleted
    assert report2.edges_written == 0
    assert sum(report2.skipped.values()) == 9  # 2 code + 1 doc + 2 sec + 2 kdocs + 2 ppl


@requires_db
def test_change_detection_and_pruning(db: Session) -> None:
    repo = _make_repo(db)
    IngestionService(db, FakeAdapter(FILES, DOCS, COMMITS)).run(repo)

    changed = dict(FILES)
    changed["src/auth/token.ts"] = b"export const TOKEN_TTL_HOURS = 1;\n"  # edited
    del changed["src/main.ts"]  # deleted from repo

    report = IngestionService(
        db, FakeAdapter(changed, DOCS, COMMITS, head_sha="sha-2")
    ).run(repo)

    assert report.updated["code_file"] == 1
    assert report.deleted["code_file"] == 1
    assert not report.created
    assert repo.last_ingested_sha == "sha-2"

    remaining = set(
        db.scalars(
            select(models.Entity.path).where(
                models.Entity.repo_id == repo.id,
                models.Entity.kind == models.EntityKind.CODE_FILE,
            )
        )
    )
    assert remaining == {"src/auth/token.ts"}


@requires_db
def test_doc_section_meta_supports_extraction(db: Session) -> None:
    """Claim extraction (T2.2) needs section text + line anchors — assert
    the contract now so it can't silently regress."""
    repo = _make_repo(db)
    IngestionService(db, FakeAdapter(FILES, DOCS, COMMITS)).run(repo)

    section = db.scalars(
        select(models.Entity).where(
            models.Entity.repo_id == repo.id,
            models.Entity.kind == models.EntityKind.DOC_SECTION,
            models.Entity.name == "Auth",
        )
    ).one()
    assert "24 hours" in section.meta["text"]
    assert section.meta["doc_path"] == "README.md"
    assert section.meta["start_line"] >= 1
    assert section.path == "README.md#auth"
