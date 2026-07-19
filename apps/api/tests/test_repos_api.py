"""T1.4 endpoint tests — TestClient against real Postgres (skip if down).

The full pipeline drill (POST → worker subprocess → status transitions →
entities) is scripts/api_smoke.py; these tests cover endpoint logic:
validation, enqueue-on-create, idempotent reconnect, token secrecy,
pagination/filter/sort/search, and 404s.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from axon.db import Base, models
from axon.db.session import get_engine
from axon.main import create_app


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
def client():
    Base.metadata.create_all(get_engine())
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def db():
    with Session(get_engine(), expire_on_commit=False) as session:
        yield session
        session.rollback()
        for repo in session.scalars(
            select(models.Repo).where(models.Repo.full_name.like("axon-test/%"))
        ):
            session.delete(repo)
        session.execute(
            text("DELETE FROM jobs WHERE payload->>'repo_id' NOT IN (SELECT id::text FROM repos)")
        )
        session.commit()


def _unique_name() -> str:
    return f"axon-test/api-{uuid.uuid4().hex[:8]}"


# --- POST /api/repos -----------------------------------------------------


def test_post_creates_repo_and_enqueues_job(client: TestClient, db: Session) -> None:
    name = _unique_name()
    response = client.post(
        "/api/repos", json={"full_name": name, "token": "ghp_secret123"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["full_name"] == name
    assert body["ingest_status"] == "pending"
    assert body["latest_job"]["kind"] == "ingest"
    assert body["latest_job"]["status"] == "pending"
    assert body["entity_counts"] == {}

    # job really is in the queue
    job = db.scalars(
        select(models.Job).where(
            models.Job.payload["repo_id"].astext == body["id"]
        )
    ).one()
    assert job.kind == models.JobKind.INGEST

    # token persisted server-side...
    repo = db.get(models.Repo, uuid.UUID(body["id"]))
    assert repo.settings["token"] == "ghp_secret123"
    # ...and never present anywhere in any API response
    assert "ghp_secret123" not in response.text
    assert "settings" not in body
    detail = client.get(f"/api/repos/{body['id']}")
    assert "ghp_secret123" not in detail.text


def test_post_rejects_bad_full_name(client: TestClient) -> None:
    assert client.post("/api/repos", json={"full_name": "not-a-repo"}).status_code == 422
    assert client.post("/api/repos", json={"full_name": "a/b/c"}).status_code == 422


def test_post_is_idempotent_for_healthy_repo(client: TestClient, db: Session) -> None:
    name = _unique_name()
    first = client.post("/api/repos", json={"full_name": name}).json()
    second = client.post("/api/repos", json={"full_name": name}).json()
    assert first["id"] == second["id"]

    jobs = db.scalars(
        select(models.Job).where(models.Job.payload["repo_id"].astext == first["id"])
    ).all()
    assert len(jobs) == 1  # reconnect did not double-enqueue


def test_post_reenqueues_after_failure(client: TestClient, db: Session) -> None:
    name = _unique_name()
    created = client.post("/api/repos", json={"full_name": name}).json()
    repo = db.get(models.Repo, uuid.UUID(created["id"]))
    repo.ingest_status = models.IngestStatus.FAILED
    db.commit()

    retried = client.post("/api/repos", json={"full_name": name}).json()
    assert retried["ingest_status"] == "pending"
    jobs = db.scalars(
        select(models.Job).where(models.Job.payload["repo_id"].astext == created["id"])
    ).all()
    assert len(jobs) == 2  # failure → reconnect re-enqueues


# --- GET /api/repos/{id} -------------------------------------------------


def test_get_repo_404(client: TestClient) -> None:
    assert client.get(f"/api/repos/{uuid.uuid4()}").status_code == 404


# --- GET /api/repos/{id}/entities ----------------------------------------


@pytest.fixture()
def seeded_repo(client: TestClient, db: Session) -> str:
    repo = models.Repo(full_name=_unique_name(), ingest_status=models.IngestStatus.READY)
    db.add(repo)
    db.flush()
    db.add_all(
        [
            models.Entity(
                repo=repo, kind=models.EntityKind.CODE_FILE,
                name="token.ts", path="src/auth/token.ts",
            ),
            models.Entity(
                repo=repo, kind=models.EntityKind.CODE_FILE,
                name="main.ts", path="src/main.ts",
            ),
            models.Entity(
                repo=repo, kind=models.EntityKind.DOC, name="README.md",
                path="README.md",
            ),
            models.Entity(
                repo=repo, kind=models.EntityKind.DOC_SECTION, name="Auth",
                path="README.md#auth",
                meta={"text": "SECRET-BULKY-TEXT", "start_line": 3},
            ),
            models.Entity(
                repo=repo, kind=models.EntityKind.ISSUE, name="Token bug",
                external_id="1", meta={"body": "BULKY-BODY", "state": "open"},
            ),
        ]
    )
    db.commit()
    return str(repo.id)


def test_entities_pagination_and_counts(client: TestClient, seeded_repo: str) -> None:
    page = client.get(f"/api/repos/{seeded_repo}/entities?limit=2&offset=0").json()
    assert page["total"] == 5
    assert len(page["items"]) == 2
    page2 = client.get(f"/api/repos/{seeded_repo}/entities?limit=2&offset=4").json()
    assert len(page2["items"]) == 1

    detail = client.get(f"/api/repos/{seeded_repo}").json()
    assert detail["entity_counts"] == {
        "code_file": 2, "doc": 1, "doc_section": 1, "issue": 1,
    }


def test_entities_kind_filter_and_search(client: TestClient, seeded_repo: str) -> None:
    only_code = client.get(
        f"/api/repos/{seeded_repo}/entities?kind=code_file"
    ).json()
    assert only_code["total"] == 2
    assert all(item["kind"] == "code_file" for item in only_code["items"])

    search = client.get(f"/api/repos/{seeded_repo}/entities?q=auth").json()
    assert {item["path"] for item in search["items"]} == {
        "src/auth/token.ts", "README.md#auth",
    }


def test_entities_sorting(client: TestClient, seeded_repo: str) -> None:
    desc = client.get(
        f"/api/repos/{seeded_repo}/entities?kind=code_file&sort=path&order=desc"
    ).json()
    paths = [item["path"] for item in desc["items"]]
    assert paths == sorted(paths, reverse=True)


def test_entities_meta_strips_bulky_fields(client: TestClient, seeded_repo: str) -> None:
    page = client.get(f"/api/repos/{seeded_repo}/entities").json()
    assert "SECRET-BULKY-TEXT" not in str(page)
    assert "BULKY-BODY" not in str(page)
    section = next(i for i in page["items"] if i["kind"] == "doc_section")
    assert section["meta"].get("start_line") == 3  # small meta survives

    issue = next(i for i in page["items"] if i["kind"] == "issue")
    assert issue["meta"].get("state") == "open"


def test_entities_404_for_unknown_repo(client: TestClient) -> None:
    assert client.get(f"/api/repos/{uuid.uuid4()}/entities").status_code == 404
