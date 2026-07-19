"""T2.3 verification — entity linker.

Offline: pure matcher units (path/symbol tiers, ambiguity guards).
DB-backed (skip when Postgres is down): all four tiers with stubbed
providers, persistence into claim_links, duplicate prevention, incremental
skip, relink-on-change, and keyless degradation.
"""

import json
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from axon.db import Base, models
from axon.db.session import get_engine
from axon.services.linking import (
    EntityLinker,
    PathIndex,
    explain_link,
    extract_symbols,
    link_by_path,
    link_by_symbol,
)

EMBED_DIM = models.EMBEDDING_DIM

PATHS = [
    "docker-compose.yml",
    "apps/api/axon/jobs/queue.py",
    "apps/api/axon/jobs/worker.py",
    "apps/api/axon/services/ingestion.py",
    "apps/api/axon/config.py",
    "apps/api/requirements.txt",
    "apps/web/components/feed/finding-card.tsx",
    "apps/web/components/repo/repo-header.tsx",
    "docs/auth.md",
]


@pytest.fixture(scope="module")
def index() -> PathIndex:
    return PathIndex(PATHS)


# --- Pure tiers -----------------------------------------------------------


def test_path_tier_exact_suffix_basename(index: PathIndex) -> None:
    exact = link_by_path(["docker-compose.yml"], index)
    assert [(m.path, m.confidence) for m in exact] == [("docker-compose.yml", 0.95)]

    suffix = link_by_path(["axon/jobs/queue.py"], index)
    assert [(m.path, m.confidence) for m in suffix] == [
        ("apps/api/axon/jobs/queue.py", 0.90)
    ]

    # a bare filename is a unique path suffix → suffix rule (0.90)
    bare = link_by_path(["requirements.txt"], index)
    assert [(m.path, m.confidence) for m in bare] == [
        ("apps/api/requirements.txt", 0.90)
    ]
    # basename rule is the case-insensitive fallback
    basename = link_by_path(["REQUIREMENTS.TXT"], index)
    assert [(m.path, m.confidence) for m in basename] == [
        ("apps/api/requirements.txt", 0.80)
    ]

    assert link_by_path(["does/not/exist.py"], index) == []


def test_path_tier_ambiguous_basename_creates_nothing() -> None:
    ambiguous = PathIndex(["a/util.py", "b/util.py"])
    assert link_by_path(["util.py"], ambiguous) == []  # no speculative links


def test_symbol_extraction() -> None:
    symbols = extract_symbols(
        "The `IngestionService` in queue.py polls /healthz using snake_case_names."
    )
    assert "IngestionService" in symbols
    assert "queue.py" in symbols
    assert "snake_case_names" in symbols
    assert "/healthz" in symbols


def test_symbol_tier_filename_and_camelcase(index: PathIndex) -> None:
    filename = link_by_symbol("queue.py claims pending jobs with row locks.", index)
    assert [(m.path, m.confidence) for m in filename] == [
        ("apps/api/axon/jobs/queue.py", 0.85)
    ]

    camel_prefix = link_by_symbol(
        "IngestionService prunes entities absent from the snapshot.", index
    )
    assert [(m.path, m.confidence) for m in camel_prefix] == [
        ("apps/api/axon/services/ingestion.py", 0.70)
    ]

    dash_norm = link_by_symbol("The RepoHeader component polls status.", index)
    assert [(m.path, m.confidence) for m in dash_norm] == [
        ("apps/web/components/repo/repo-header.tsx", 0.75)
    ]

    assert link_by_symbol("Findings are ordered newest first.", index) == []


def test_symbol_tier_ambiguity_guard() -> None:
    many = PathIndex([f"pkg{i}/handler.py" for i in range(5)])
    assert link_by_symbol("handler.py does things.", many) == []  # >3 targets


# --- DB-backed service ----------------------------------------------------


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


class VectorEmbeddings:
    """Deterministic 'embeddings': orthogonal-ish vectors per keyword so
    similarity outcomes are designed, not lucky."""

    def __init__(self, vectors: dict[str, list[float]], default: list[float]) -> None:
        self.vectors = vectors
        self.default = default
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        out = []
        for text_ in texts:
            vector = next(
                (v for key, v in self.vectors.items() if key in text_), self.default
            )
            out.append(vector + [0.0] * (EMBED_DIM - len(vector)))
        return out


class ScriptedCompletion:
    name = "scripted"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def complete_json(self, *, prompt, system, schema, schema_name) -> str:
        self.calls.append(prompt)
        return self.responses.pop(0)


def _axis(i: int) -> list[float]:
    v = [0.0] * 8
    v[i] = 1.0
    return v


def _seed(db: Session, statements: list[dict]) -> models.Repo:
    repo = models.Repo(full_name=f"axon-test/link-{uuid.uuid4().hex[:8]}")
    db.add(repo)
    for path in PATHS:
        db.add(
            models.Entity(
                repo=repo,
                kind=models.EntityKind.DOC if path.endswith(".md") else models.EntityKind.CODE_FILE,
                name=path.rsplit("/", 1)[-1], path=path,
            )
        )
    source = models.Entity(
        repo=repo, kind=models.EntityKind.DOC_SECTION, name="src",
        path="docs/x.md#s", meta={},
    )
    db.add(source)
    for spec in statements:
        db.add(
            models.Claim(
                repo=repo, source_entity=source,
                statement=spec["statement"],
                claim_type=models.ClaimType.BEHAVIOR,
                anchor={"path": "docs/x.md", "mentioned_paths": spec.get("paths", [])},
                embedding=spec.get("embedding"),
            )
        )
    db.commit()
    return repo


@requires_db
def test_tiers_persist_with_method_and_confidence(db: Session) -> None:
    embedding_axis = _axis(0) + [0.0] * (EMBED_DIM - 8)
    repo = _seed(
        db,
        [
            {"statement": "Compose publishes Postgres on 5434.",
             "paths": ["docker-compose.yml"]},
            {"statement": "queue.py claims jobs with row locks.", "paths": []},
            {"statement": "Vague statement about worker pacing.", "paths": [],
             "embedding": embedding_axis},
        ],
    )
    embeddings = VectorEmbeddings(
        {"worker.py": _axis(0)},  # entity path text containing 'worker.py'
        default=_axis(7),
    )
    report = EntityLinker(
        db, embedding_provider=embeddings,
        completion_provider=ScriptedCompletion([]),
        similarity_threshold=0.6,
    ).run(repo)

    assert report.claims_by_method == {
        "path_match": 1, "symbol_match": 1, "embedding": 1,
    }
    assert report.claims_unresolved == 0
    assert report.llm_calls == 0

    links = db.scalars(
        select(models.ClaimLink)
        .join(models.Claim, models.Claim.id == models.ClaimLink.claim_id)
        .where(models.Claim.repo_id == repo.id)
    ).all()
    assert len(links) == 3
    by_method = {link.method: link for link in links}
    assert by_method[models.LinkMethod.PATH_MATCH].strength == 0.95
    assert by_method[models.LinkMethod.SYMBOL_MATCH].strength == 0.85
    assert by_method[models.LinkMethod.EMBEDDING].strength >= 0.6
    # every link is explainable
    for link in links:
        why = explain_link(db, link)
        assert "→" in why and f"{link.strength:.2f}" in why


@requires_db
def test_llm_fallback_and_null_answer(db: Session) -> None:
    axis = _axis(1) + [0.0] * (EMBED_DIM - 8)
    repo = _seed(
        db,
        [
            {"statement": "Something only the model can place.", "paths": [],
             "embedding": axis},
            {"statement": "Totally unrelatable external fact.", "paths": [],
             "embedding": axis},
        ],
    )
    embeddings = VectorEmbeddings({}, default=_axis(2))  # below threshold for all
    completion = ScriptedCompletion(
        [
            json.dumps({"entity_path": "apps/api/axon/jobs/worker.py",
                        "confidence": 0.9}),
            json.dumps({"entity_path": None, "confidence": 0.2}),
        ]
    )
    report = EntityLinker(
        db, embedding_provider=embeddings, completion_provider=completion,
        similarity_threshold=0.6,
    ).run(repo)

    assert report.llm_calls == 2
    assert report.claims_by_method.get("llm") == 1
    assert report.claims_unresolved == 1  # null answer → no speculative link

    llm_link = db.scalars(
        select(models.ClaimLink).where(
            models.ClaimLink.method == models.LinkMethod.LLM
        )
    ).one()
    assert llm_link.strength == 0.70  # capped below deterministic tiers


@requires_db
def test_incremental_skip_and_no_duplicates(db: Session) -> None:
    repo = _seed(
        db, [{"statement": "Compose publishes Postgres on 5434.",
              "paths": ["docker-compose.yml"]}],
    )
    EntityLinker(db, completion_provider=ScriptedCompletion([])).run(repo)

    # second run: fingerprint unchanged → nothing recomputed, no dup rows
    report2 = EntityLinker(db, completion_provider=ScriptedCompletion([])).run(repo)
    assert report2.claims_skipped_unchanged == 1
    assert report2.links_created == 0
    links = db.scalars(
        select(models.ClaimLink)
        .join(models.Claim, models.Claim.id == models.ClaimLink.claim_id)
        .where(models.Claim.repo_id == repo.id)
    ).all()
    assert len(links) == 1


@requires_db
def test_relink_when_inventory_changes(db: Session) -> None:
    repo = _seed(
        db, [{"statement": "linking.py resolves claims to files.", "paths": []}],
    )
    report1 = EntityLinker(db).run(repo)
    assert report1.claims_unresolved == 1  # linking.py not in inventory yet

    db.add(
        models.Entity(
            repo=repo, kind=models.EntityKind.CODE_FILE, name="linking.py",
            path="apps/api/axon/services/linking.py",
        )
    )
    db.commit()

    report2 = EntityLinker(db).run(repo)  # inventory hash changed → relink
    assert report2.claims_skipped_unchanged == 0
    assert report2.claims_by_method == {"symbol_match": 1}


@requires_db
def test_keyless_runs_deterministic_tiers_only(db: Session) -> None:
    axis = _axis(3) + [0.0] * (EMBED_DIM - 8)
    repo = _seed(
        db,
        [
            {"statement": "Compose publishes Postgres on 5434.",
             "paths": ["docker-compose.yml"]},
            {"statement": "Unplaceable vague statement.", "paths": [],
             "embedding": axis},
        ],
    )
    # no providers injected + no keys configured → tiers 3/4 skipped
    report = EntityLinker(db).run(repo)
    assert report.claims_by_method == {"path_match": 1}
    assert report.claims_unresolved == 1
    assert report.llm_calls == 0
    assert report.embedding_calls == 0
