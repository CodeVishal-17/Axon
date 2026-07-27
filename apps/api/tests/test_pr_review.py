"""Pull-request review tests.

The offline half (no DB, no network) covers the grounding contract, which is
the part that must never regress: a comment may only cite a line that really
exists in the PR diff, and low-confidence comments are dropped — the review
equivalent of remediation's never-invent-facts gate.

The DB half exercises the real service against Postgres with a scripted LLM
and a stubbed adapter: both lenses survive, status transitions are correct,
re-review of the same head_sha is idempotent, and posting is a single
GitHub call that never repeats.
"""

import json
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from axon.adapters.base import PullRequestFile, PullRequestInfo
from axon.db import Base, models
from axon.db.session import get_engine
from axon.llm.provider import LLMError
from axon.services.pr_review import (
    PRReviewService,
    ReviewComment,
    ReviewReport,
    commentable_lines,
    diff_line_numbers,
)

# --- Fixtures / doubles ---------------------------------------------------

PATCH = """@@ -1,4 +1,6 @@
 import os
-PORT = 5000
+PORT = 8080
+DEBUG = True

 def main():"""


class ScriptedCompletion:
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


class StubAdapter:
    """The four adapter methods the review service touches."""

    def __init__(self, head_sha: str = "abc1234", files=None):
        self.pull = PullRequestInfo(
            number=7, title="Serve on port 8080", body="Switch the port.",
            author="octocat", head_sha=head_sha, base_branch="main",
            draft=False, url="https://github.com/acme/widgets/pull/7",
            updated_at=None,
        )
        self.files = files if files is not None else (
            PullRequestFile(
                path="app/config.py", status="modified",
                additions=2, deletions=1, patch=PATCH,
            ),
        )
        self.created_reviews: list[dict] = []

    def fetch_pull(self, number): return self.pull
    def fetch_pr_diff(self, number, limit=300): return self.files

    def create_review(self, number, body, comments, commit_id=None, event="COMMENT"):
        self.created_reviews.append(
            {"number": number, "body": body, "comments": comments,
             "commit_id": commit_id, "event": event}
        )
        return "https://github.com/acme/widgets/pull/7#pullrequestreview-1"


def review_json(comments: list[dict], summary: str = "Switches the port.") -> str:
    return json.dumps({"summary": summary, "comments": comments})


def comment(
    path="app/config.py", line=2, lens="code", severity="high",
    body="Looks wrong.", confidence=0.9,
) -> dict:
    return {
        "path": path, "line": line, "lens": lens,
        "severity": severity, "body": body, "confidence": confidence,
    }


# --- Offline: diff parsing ------------------------------------------------


def test_diff_line_numbers_maps_new_side():
    # new side: 1 context, 2-3 added, 4 blank context, 5 context
    assert diff_line_numbers(PATCH) == {1, 2, 3, 4, 5}


def test_diff_line_numbers_handles_missing_patch():
    assert diff_line_numbers(None) == set()
    assert diff_line_numbers("") == set()


def test_diff_line_numbers_ignores_removed_lines():
    patch = "@@ -1,3 +1,1 @@\n-gone one\n-gone two\n+kept"
    # only the added line has a new-side number
    assert diff_line_numbers(patch) == {1}


def test_render_files_never_sends_an_empty_diff():
    """A single file larger than the budget is truncated, not dropped —
    otherwise the model would review nothing at all."""
    big = PullRequestFile("big.py", "modified", 1, 0, "x" * 100)
    rendered = PRReviewService._render_files((big,), 50)
    assert len(rendered) == 1
    assert "truncated" in rendered[0]["patch"]


def test_render_files_stops_at_the_char_budget():
    small = PullRequestFile("small.py", "modified", 1, 0, "y" * 10)
    big = PullRequestFile("big.py", "modified", 1, 0, "x" * 100)
    rendered = PRReviewService._render_files((small, big), 20)
    assert [f["path"] for f in rendered] == ["small.py"]


def test_commentable_lines_keys_by_path():
    files = (
        PullRequestFile("a.py", "modified", 1, 0, PATCH),
        PullRequestFile("bin.png", "added", 0, 0, None),
    )
    mapping = commentable_lines(files)
    assert mapping["a.py"] == {1, 2, 3, 4, 5}
    assert mapping["bin.png"] == set()  # binary: nothing commentable


# --- Offline: the grounding gate -----------------------------------------


def _gate(comments: list[ReviewComment], min_confidence: float = 0.6):
    service = PRReviewService.__new__(PRReviewService)
    service.report = ReviewReport()
    service.min_confidence = min_confidence
    kept = service._gate(comments, {"app/config.py": {1, 2, 3}})
    return kept, service.report


def test_gate_keeps_grounded_comments():
    kept, report = _gate([ReviewComment(**comment(line=2))])
    assert len(kept) == 1
    assert report.dropped_ungrounded == 0


def test_gate_drops_hallucinated_line_numbers():
    kept, report = _gate([ReviewComment(**comment(line=999))])
    assert kept == []
    assert report.dropped_ungrounded == 1
    assert "app/config.py:999" in report.dropped_paths


def test_gate_drops_comments_on_files_not_in_the_diff():
    kept, report = _gate([ReviewComment(**comment(path="other/file.py", line=1))])
    assert kept == []
    assert report.dropped_ungrounded == 1


def test_gate_drops_low_confidence():
    kept, report = _gate([ReviewComment(**comment(confidence=0.2))])
    assert kept == []
    assert report.dropped_low_confidence == 1


def test_gate_counts_both_lenses_separately():
    kept, report = _gate([
        ReviewComment(**comment(line=1, lens="code")),
        ReviewComment(**comment(line=2, lens="truth")),
        ReviewComment(**comment(line=3, lens="truth")),
    ])
    assert len(kept) == 3
    assert report.code_comments == 1
    assert report.truth_comments == 2


# --- DB-backed: the real service -----------------------------------------


def _db_available() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


pytestmark_db = pytest.mark.skipif(
    not _db_available(),
    reason="Postgres not reachable — start it with `docker compose up -d db`",
)


@pytest.fixture()
def db():
    Base.metadata.create_all(get_engine())
    with Session(get_engine(), expire_on_commit=False) as session:
        yield session
        session.rollback()
        for repo in session.scalars(
            select(models.Repo).where(models.Repo.full_name.like("axon-test/%"))
        ):
            session.delete(repo)
        session.commit()


@pytest.fixture()
def repo(db: Session) -> models.Repo:
    repo = models.Repo(
        full_name=f"axon-test/review-{uuid.uuid4().hex[:8]}",
        ingest_status=models.IngestStatus.READY,
    )
    db.add(repo)
    db.commit()
    return repo


@pytestmark_db
def test_review_persists_both_lenses(db: Session, repo: models.Repo):
    adapter = StubAdapter()
    llm = ScriptedCompletion([
        review_json([
            comment(line=2, lens="code", body="Hard-coded port."),
            comment(line=3, lens="truth", body="README says port 5000."),
        ])
    ])
    review = PRReviewService(db, adapter=adapter, completion_provider=llm).review(
        repo, 7
    )

    assert review.status == models.ReviewStatus.GENERATED
    assert review.head_sha == "abc1234"
    assert review.pr_title == "Serve on port 8080"
    lenses = sorted(c["lens"] for c in review.comments)
    assert lenses == ["code", "truth"]


@pytestmark_db
def test_review_drops_ungrounded_comments(db: Session, repo: models.Repo):
    adapter = StubAdapter()
    llm = ScriptedCompletion([
        review_json([
            comment(line=2, lens="code"),          # grounded
            comment(line=4242, lens="code"),       # invented line
            comment(path="ghost.py", line=1),      # file not in diff
        ])
    ])
    review = PRReviewService(db, adapter=adapter, completion_provider=llm).review(
        repo, 7
    )
    assert len(review.comments) == 1
    assert review.comments[0]["line"] == 2


@pytestmark_db
def test_llm_failure_marks_review_failed(db: Session, repo: models.Repo):
    service = PRReviewService(
        db, adapter=StubAdapter(), completion_provider=ScriptedCompletion(["RAISE"])
    )
    review = service.review(repo, 7)
    assert review.status == models.ReviewStatus.FAILED
    assert "LLM failure" in review.error
    assert review.comments == []


@pytestmark_db
def test_review_is_idempotent_per_head_sha(db: Session, repo: models.Repo):
    adapter = StubAdapter()
    llm = ScriptedCompletion([review_json([comment(line=2)])])
    service = PRReviewService(db, adapter=adapter, completion_provider=llm)

    first = service.review(repo, 7)
    # Second call: no second LLM response is scripted, so a re-generation
    # would raise IndexError — the stored review must be returned instead.
    second = PRReviewService(
        db, adapter=adapter, completion_provider=llm
    ).review(repo, 7)

    assert first.id == second.id
    assert len(llm.calls) == 1
    rows = db.scalars(
        select(models.PullRequestReview).where(
            models.PullRequestReview.repo_id == repo.id
        )
    ).all()
    assert len(rows) == 1


@pytestmark_db
def test_new_head_sha_produces_a_fresh_review(db: Session, repo: models.Repo):
    llm = ScriptedCompletion([
        review_json([comment(line=2)]),
        review_json([comment(line=3)]),
    ])
    PRReviewService(db, adapter=StubAdapter(head_sha="aaa1111"),
                    completion_provider=llm).review(repo, 7)
    PRReviewService(db, adapter=StubAdapter(head_sha="bbb2222"),
                    completion_provider=llm).review(repo, 7)

    rows = db.scalars(
        select(models.PullRequestReview).where(
            models.PullRequestReview.repo_id == repo.id
        )
    ).all()
    assert {r.head_sha for r in rows} == {"aaa1111", "bbb2222"}


@pytestmark_db
def test_post_review_publishes_once(db: Session, repo: models.Repo):
    adapter = StubAdapter()
    llm = ScriptedCompletion([
        review_json([
            comment(line=2, lens="code"),
            comment(line=3, lens="truth"),
        ])
    ])
    service = PRReviewService(db, adapter=adapter, completion_provider=llm)
    review = service.review(repo, 7)

    posted = service.post_review(review)
    assert posted.status == models.ReviewStatus.POSTED
    assert posted.review_url.endswith("#pullrequestreview-1")
    assert len(adapter.created_reviews) == 1

    call = adapter.created_reviews[0]
    assert call["event"] == "COMMENT"  # never approve / request changes
    assert call["commit_id"] == "abc1234"
    assert {c["side"] for c in call["comments"]} == {"RIGHT"}
    # Lens labels reach the GitHub comment bodies.
    bodies = " ".join(c["body"] for c in call["comments"])
    assert "Truth maintenance" in bodies and "Code review" in bodies

    # Posting again is a no-op — no duplicate GitHub review.
    service.post_review(posted)
    assert len(adapter.created_reviews) == 1


@pytestmark_db
def test_failed_review_is_not_postable(db: Session, repo: models.Repo):
    service = PRReviewService(
        db, adapter=StubAdapter(), completion_provider=ScriptedCompletion(["RAISE"])
    )
    review = service.review(repo, 7)
    with pytest.raises(ValueError):
        service.post_review(review)


@pytestmark_db
def test_truth_lens_receives_claims_linked_to_changed_files(
    db: Session, repo: models.Repo
):
    """The differentiator: claims the graph links to the PR's files are fed
    to the model, so truth comments are grounded in real beliefs."""
    entity = models.Entity(
        repo=repo, kind=models.EntityKind.CODE_FILE, name="config.py",
        path="app/config.py",
    )
    doc = models.Entity(
        repo=repo, kind=models.EntityKind.DOC_SECTION, name="Config",
        path="README.md#config",
    )
    claim = models.Claim(
        repo=repo, source_entity=doc,
        statement="The service runs on port 5000.",
        claim_type=models.ClaimType.BEHAVIOR,
        status=models.ClaimStatus.VERIFIED, anchor={},
    )
    db.add_all([entity, doc, claim])
    db.flush()
    db.add(
        models.ClaimLink(
            claim_id=claim.id,
            entity_id=entity.id,
            method=models.LinkMethod.PATH_MATCH,
        )
    )
    db.commit()

    llm = ScriptedCompletion([review_json([])])
    PRReviewService(db, adapter=StubAdapter(), completion_provider=llm).review(
        repo, 7
    )
    prompt = llm.calls[0]
    assert "The service runs on port 5000." in prompt
    assert "README.md#config" in prompt
