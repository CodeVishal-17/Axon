"""T3.1 verification — event ingestion + scoped verification.

Offline: webhook normalization (table-driven from the eval fixtures).
DB-backed: HMAC gate, dedupe, EventService persistence + enqueue, planner
scoping (code / doc / issue), the VERIFY handler end-to-end with a spied
verifier (scoped claim ids, event provenance, budget, idempotent re-run),
belief-change reingest routing, and finding auto-resolution.
"""

import hashlib
import hmac
import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from axon.adapters.github.adapter import GitHubAdapter
from axon.db import Base, models
from axon.db.session import get_engine
from axon.main import create_app
from axon.services.events import EventService, ScopedVerificationPlanner
from axon.services.verification import DriftVerifier

FIXTURES = json.loads(
    (Path(__file__).resolve().parents[1] / "axon" / "evals" / "events" / "fixtures.json")
    .read_text(encoding="utf-8")
)["cases"]


# --- Offline: normalization (evaluation fixtures) -------------------------


@pytest.mark.parametrize("case", FIXTURES, ids=[c["id"] for c in FIXTURES])
def test_webhook_normalization(case: dict) -> None:
    normalized = GitHubAdapter.normalize_webhook(
        case["event"], case["delivery"], case["payload"], default_branch="main"
    )
    expected = case["expected"]
    if expected["disposition"] == "ignored":
        assert normalized is None
        return
    assert normalized is not None
    if expected["disposition"] == "reingest":
        assert normalized.reingest_only
        assert normalized.issue_number == expected.get("issue_number")
        return
    assert not normalized.reingest_only
    assert normalized.kind == expected["kind"]
    assert normalized.external_id == case["delivery"]
    if "changed_paths" in expected:
        assert list(normalized.changed_paths) == expected["changed_paths"]
    if "pr_number" in expected:
        assert normalized.pr_number == expected["pr_number"]
    if "issue_number" in expected:
        assert normalized.issue_number == expected["issue_number"]
    if "head_sha" in expected:
        assert normalized.head_sha == expected["head_sha"]


# --- DB-backed ------------------------------------------------------------


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
        session.execute(text(
            "DELETE FROM jobs WHERE (payload->>'repo_id') NOT IN "
            "(SELECT id::text FROM repos) AND (payload->>'event_id') NOT IN "
            "(SELECT id::text FROM events)"
        ))
        session.commit()


@pytest.fixture()
def client():
    Base.metadata.create_all(get_engine())
    with TestClient(create_app()) as test_client:
        yield test_client


def _seed_repo(db: Session) -> models.Repo:
    repo = models.Repo(
        full_name=f"axon-test/ev-{uuid.uuid4().hex[:8]}", default_branch="main",
    )
    code = models.Entity(
        repo=repo, kind=models.EntityKind.CODE_FILE, name="token.ts",
        path="src/auth/token.ts",
    )
    other_code = models.Entity(
        repo=repo, kind=models.EntityKind.CODE_FILE, name="util.ts",
        path="src/util.ts",
    )
    doc = models.Entity(
        repo=repo, kind=models.EntityKind.DOC, name="auth.md", path="docs/auth.md",
    )
    section = models.Entity(
        repo=repo, kind=models.EntityKind.DOC_SECTION, name="Auth",
        path="docs/auth.md#auth",
        meta={"text": "Tokens expire.", "doc_path": "docs/auth.md", "start_line": 3},
    )
    issue = models.Entity(
        repo=repo, kind=models.EntityKind.ISSUE, name="Exporter bug",
        external_id="31", meta={"state": "open"},
    )
    linked_claim = models.Claim(
        repo=repo, source_entity=section,
        statement="Access tokens expire after 24 hours.",
        claim_type=models.ClaimType.BEHAVIOR, anchor={},
    )
    unrelated_claim = models.Claim(
        repo=repo, source_entity=section,
        statement="Totally unrelated claim.",
        claim_type=models.ClaimType.BEHAVIOR, anchor={},
    )
    issue_claim = models.Claim(
        repo=repo, source_entity=issue,
        statement="The exporter crashes on files over 2GB.",
        claim_type=models.ClaimType.STATUS, anchor={},
    )
    db.add_all([repo, code, other_code, doc, section, issue,
                linked_claim, unrelated_claim, issue_claim])
    db.flush()
    db.add_all([
        models.ClaimLink(claim_id=linked_claim.id, entity_id=code.id,
                         strength=0.9, method=models.LinkMethod.PATH_MATCH),
        models.ClaimLink(claim_id=unrelated_claim.id, entity_id=other_code.id,
                         strength=0.9, method=models.LinkMethod.PATH_MATCH),
    ])
    db.commit()
    repo._test_ids = {  # type: ignore[attr-defined]
        "linked": linked_claim.id, "unrelated": unrelated_claim.id,
        "issue_claim": issue_claim.id, "section": section.id,
    }
    return repo


def _push_payload(repo: models.Repo, paths: list[str]) -> dict:
    return {
        "ref": "refs/heads/main",
        "after": "abc123",
        "commits": [{"added": [], "modified": paths, "removed": []}],
        "repository": {"full_name": repo.full_name},
    }


# --- EventService: dedupe + persistence + enqueue -------------------------


@requires_db
def test_event_service_persists_and_enqueues_then_dedupes(db: Session) -> None:
    repo = _seed_repo(db)
    normalized = GitHubAdapter.normalize_webhook(
        "push", "delivery-1", _push_payload(repo, ["src/auth/token.ts"]), "main"
    )
    outcome = EventService(db).ingest(repo, normalized)
    assert outcome.status == "accepted"

    event = db.get(models.Event, uuid.UUID(outcome.event_id))
    assert event.kind == models.EventKind.PUSH
    assert event.external_id == "delivery-1"
    assert event.payload["changed_paths"] == ["src/auth/token.ts"]
    assert event.processed_at is None

    job = db.get(models.Job, uuid.UUID(outcome.job_id))
    assert job.kind == models.JobKind.VERIFY
    assert job.payload == {"event_id": outcome.event_id}

    # duplicate delivery: same external_id → no new event, no new job
    duplicate = EventService(db).ingest(repo, normalized)
    assert duplicate.status == "duplicate"
    assert duplicate.event_id == outcome.event_id
    events = db.scalars(
        select(models.Event).where(models.Event.repo_id == repo.id)
    ).all()
    assert len(events) == 1


@requires_db
def test_belief_change_routes_to_single_reingest(db: Session) -> None:
    repo = _seed_repo(db)
    edited = GitHubAdapter.normalize_webhook(
        "issues", "d-edit-1",
        {"action": "edited", "issue": {"number": 31, "title": "t"}}, "main",
    )
    first = EventService(db).ingest(repo, edited)
    assert first.status == "reingest"
    # no Event row for belief changes
    assert db.scalars(
        select(models.Event).where(models.Event.repo_id == repo.id)
    ).all() == []

    second = EventService(db).ingest(repo, edited)
    assert second.status == "reingest"
    assert second.job_id == first.job_id  # pending ingest deduped


# --- Planner --------------------------------------------------------------


@requires_db
def test_planner_scopes_code_change_to_linked_claims_only(db: Session) -> None:
    repo = _seed_repo(db)
    ids = repo._test_ids  # type: ignore[attr-defined]
    outcome = EventService(db).ingest(
        repo,
        GitHubAdapter.normalize_webhook(
            "push", "d-scope-1", _push_payload(repo, ["src/auth/token.ts"]), "main"
        ),
    )
    event = db.get(models.Event, uuid.UUID(outcome.event_id))
    plan = ScopedVerificationPlanner(db).plan(repo, event, ["src/auth/token.ts"])

    assert plan.impacted_claim_ids == [ids["linked"]]      # NOT unrelated
    assert plan.reingest_needed is False                   # code-only change


@requires_db
def test_planner_doc_change_includes_sections_and_flags_reingest(db: Session) -> None:
    repo = _seed_repo(db)
    ids = repo._test_ids  # type: ignore[attr-defined]
    outcome = EventService(db).ingest(
        repo,
        GitHubAdapter.normalize_webhook(
            "push", "d-scope-2", _push_payload(repo, ["docs/auth.md"]), "main"
        ),
    )
    event = db.get(models.Event, uuid.UUID(outcome.event_id))
    plan = ScopedVerificationPlanner(db).plan(repo, event, ["docs/auth.md"])

    assert plan.reingest_needed is True
    # claims sourced from the doc's sections are impacted
    assert set(plan.impacted_claim_ids) == {ids["linked"], ids["unrelated"]}


@requires_db
def test_planner_issue_event_scopes_issue_sourced_claims(db: Session) -> None:
    repo = _seed_repo(db)
    ids = repo._test_ids  # type: ignore[attr-defined]
    outcome = EventService(db).ingest(
        repo,
        GitHubAdapter.normalize_webhook(
            "issues", "d-scope-3",
            {"action": "closed", "issue": {"number": 31, "title": "t"}}, "main",
        ),
    )
    event = db.get(models.Event, uuid.UUID(outcome.event_id))
    plan = ScopedVerificationPlanner(db).plan(repo, event, [])

    assert plan.impacted_claim_ids == [ids["issue_claim"]]
    assert plan.reingest_needed is True


# --- VERIFY handler end-to-end (spied verifier) ---------------------------


class VerifierSpy:
    calls: list[dict] = []

    def __init__(self, db, event=None, budget=None, **kwargs):
        self._db = db
        self._event = event
        self._budget = budget

    def run(self, repo, claim_ids=None):
        VerifierSpy.calls.append(
            {"event_id": self._event.id if self._event else None,
             "budget": self._budget, "claim_ids": sorted(claim_ids or [])}
        )


@requires_db
def test_verify_handler_scoped_provenance_budget_idempotent(
    db: Session, monkeypatch
) -> None:
    from axon.jobs.handlers import verify as verify_handler

    repo = _seed_repo(db)
    ids = repo._test_ids  # type: ignore[attr-defined]
    VerifierSpy.calls = []
    monkeypatch.setattr(verify_handler, "DriftVerifier", VerifierSpy)
    monkeypatch.setattr(verify_handler, "llm_configured", lambda: True)

    outcome = EventService(db).ingest(
        repo,
        GitHubAdapter.normalize_webhook(
            "push", "d-e2e-1", _push_payload(repo, ["src/auth/token.ts"]), "main"
        ),
    )
    verify_handler.run(db, {"event_id": outcome.event_id})

    assert len(VerifierSpy.calls) == 1
    call = VerifierSpy.calls[0]
    assert call["claim_ids"] == [ids["linked"]]            # scoped, not full repo
    assert str(call["event_id"]) == outcome.event_id       # provenance attached
    from axon.config import get_settings
    assert call["budget"] == get_settings().verify_event_budget

    db.expire_all()
    event = db.get(models.Event, uuid.UUID(outcome.event_id))
    assert event.processed_at is not None

    # idempotent re-run: same scope, still one event, re-stamped
    verify_handler.run(db, {"event_id": outcome.event_id})
    assert len(VerifierSpy.calls) == 2
    assert VerifierSpy.calls[1]["claim_ids"] == [ids["linked"]]
    assert len(db.scalars(
        select(models.Event).where(models.Event.repo_id == repo.id)
    ).all()) == 1


# --- Event-driven verifier: provenance + auto-resolve ---------------------


class ScriptedCompletion:
    name = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)

    def complete_json(self, *, prompt, system, schema, schema_name):
        return self.responses.pop(0)


def _verdict(verdict, quote, path="src/auth/token.ts"):
    return json.dumps({
        "verdict": verdict, "confidence": 0.95, "evidence_quote": quote,
        "evidence_path": path, "explanation": "docs say 24; code says 1",
    })


TOKEN_TS = b"export const TOKEN_TTL_HOURS = 1;\n"


@requires_db
def test_findings_carry_event_provenance_and_resolve_on_verified(db: Session) -> None:
    repo = _seed_repo(db)
    ids = repo._test_ids  # type: ignore[attr-defined]
    outcome = EventService(db).ingest(
        repo,
        GitHubAdapter.normalize_webhook(
            "push", "d-prov-1", _push_payload(repo, ["src/auth/token.ts"]), "main"
        ),
    )
    event = db.get(models.Event, uuid.UUID(outcome.event_id))

    quote = "export const TOKEN_TTL_HOURS = 1;"
    DriftVerifier(
        db, event=event,
        completion_provider=ScriptedCompletion([_verdict("CONTRADICTED", quote)]),
        fetch_file=lambda p: TOKEN_TS,
    ).run(repo, claim_ids=[ids["linked"]])

    finding = db.scalars(
        select(models.Finding).where(models.Finding.claim_id == ids["linked"])
    ).one()
    assert finding.event_id == event.id                    # feed provenance
    assert finding.status == models.FindingStatus.OPEN

    # the doc gets fixed; the next event re-verifies → finding auto-resolves
    report = DriftVerifier(
        db, event=event,
        completion_provider=ScriptedCompletion([_verdict("VERIFIED", quote)]),
        fetch_file=lambda p: TOKEN_TS,
    ).run(repo, claim_ids=[ids["linked"]])

    assert report.findings_resolved == 1
    db.expire_all()
    assert (
        db.get(models.Finding, finding.id).status == models.FindingStatus.DISMISSED
    )
    assert db.get(models.Claim, ids["linked"]).status == models.ClaimStatus.VERIFIED


# --- HTTP layer -----------------------------------------------------------


def _signed_headers(secret: str, body: bytes, event: str, delivery: str) -> dict:
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": f"sha256={signature}",
        "Content-Type": "application/json",
    }


@requires_db
def test_webhook_endpoint_hmac_and_flow(client, db: Session, monkeypatch) -> None:
    from axon.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "github_webhook_secret", "hook-secret")

    repo = _seed_repo(db)
    body = json.dumps(_push_payload(repo, ["src/auth/token.ts"])).encode()

    # bad signature → 401
    bad = client.post(
        "/api/webhooks/github", content=body,
        headers={**_signed_headers("wrong", body, "push", "d-http-1")},
    )
    assert bad.status_code == 401

    # good signature → accepted
    ok = client.post(
        "/api/webhooks/github", content=body,
        headers=_signed_headers("hook-secret", body, "push", "d-http-1"),
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "accepted"

    # duplicate delivery id → safely ignored
    dup = client.post(
        "/api/webhooks/github", content=body,
        headers=_signed_headers("hook-secret", body, "push", "d-http-1"),
    )
    assert dup.json()["status"] == "duplicate"

    # unknown repo → acknowledged as ignored (GitHub must not retry forever)
    unknown_body = json.dumps(
        {"ref": "refs/heads/main", "commits": [],
         "repository": {"full_name": "nobody/nothing"}}
    ).encode()
    unknown = client.post(
        "/api/webhooks/github", content=unknown_body,
        headers=_signed_headers("hook-secret", unknown_body, "push", "d-http-2"),
    )
    assert unknown.json()["status"] == "ignored"


@requires_db
def test_simulate_endpoint_secret_and_identical_path(
    client, db: Session, monkeypatch
) -> None:
    from axon.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "simulate_shared_secret", "sim-secret")

    repo = _seed_repo(db)
    request = {
        "event": "push",
        "payload": _push_payload(repo, ["src/auth/token.ts"]),
        "external_id": "sim-demo-1",
    }

    denied = client.post(f"/api/repos/{repo.id}/simulate-event", json=request)
    assert denied.status_code == 403

    ok = client.post(
        f"/api/repos/{repo.id}/simulate-event", json=request,
        headers={"X-Axon-Simulate-Secret": "sim-secret"},
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "accepted"

    event = db.get(models.Event, uuid.UUID(ok.json()["event_id"]))
    assert event.kind == models.EventKind.SIMULATED        # honest provenance
    assert event.payload["changed_paths"] == ["src/auth/token.ts"]
    job = db.get(models.Job, uuid.UUID(ok.json()["job_id"]))
    assert job.kind == models.JobKind.VERIFY               # identical pipeline
