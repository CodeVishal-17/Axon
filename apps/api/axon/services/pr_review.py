"""Pull-request review: an AI review of work humans (or bots) already opened.

Position in the product: the Truth Feed acts on drift Axon *discovers*; this
service acts on the PRs a repository already has. Two lenses, kept separate
all the way to the UI:

  code   — ordinary review (correctness, edge cases, risk).
  truth  — Axon's differentiator. The claims fed to the model are exactly the
           ones the knowledge graph LINKS to the files this PR touches,
           resolved with the same ``ScopedVerificationPlanner`` the drift
           engine uses. So "this PR contradicts documented claim X" is
           grounded in the graph, not guessed.

Grounding contract (mirrors remediation's never-invent-facts gate):
  * diff gate       — a comment's (path, line) must exist in the PR's patch
                      hunks. Line numbers are the review equivalent of an
                      excerpt: inventing one produces a comment GitHub would
                      reject and a human cannot trust. Dropped, and counted.
  * confidence gate — comments below REVIEW_MIN_CONFIDENCE are dropped.
  * lens gate       — a comment must declare a known lens.

Reviews are persisted, never auto-posted: publishing to GitHub happens in
``post_review`` behind an explicit user click (architecture §12), authored by
the GitHub App's bot identity.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from axon.adapters.base import PullRequestFile, PullRequestInfo
from axon.config import get_settings
from axon.db.models import (
    Claim,
    Entity,
    Event,
    PullRequestReview,
    Repo,
    ReviewStatus,
)
from axon.llm import provider as llm
from axon.llm.prompts.pr_review import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_user_prompt,
)
from axon.services.events import ScopedVerificationPlanner

logger = logging.getLogger("axon.services.pr_review")

LENSES = ("code", "truth")
_HUNK_RE = re.compile(r"^@@[^+]*\+(\d+)")


# --- Structured LLM output ------------------------------------------------


class ReviewComment(BaseModel):
    path: str
    line: int
    lens: Literal["code", "truth"]
    severity: Literal["critical", "high", "medium", "low"]
    body: str
    confidence: float = Field(ge=0.0, le=1.0)


class PRReviewOutput(BaseModel):
    summary: str
    comments: list[ReviewComment] = []


@dataclass
class ReviewReport:
    files_reviewed: int = 0
    claims_in_scope: int = 0
    comments_proposed: int = 0
    comments_kept: int = 0
    dropped_ungrounded: int = 0
    dropped_low_confidence: int = 0
    code_comments: int = 0
    truth_comments: int = 0
    llm_failures: int = 0
    duration_s: float = 0.0
    dropped_paths: list[str] = field(default_factory=list)

    def summary_line(self) -> str:
        return (
            f"pr review: files={self.files_reviewed} "
            f"claims_in_scope={self.claims_in_scope} "
            f"proposed={self.comments_proposed} kept={self.comments_kept} "
            f"(code={self.code_comments} truth={self.truth_comments})\n"
            f"dropped: ungrounded={self.dropped_ungrounded} "
            f"confidence={self.dropped_low_confidence} "
            f"llm failures={self.llm_failures}\n"
            f"duration: {self.duration_s:.1f}s"
        )


# --- Pure helpers (unit-testable without a DB or network) -----------------


def diff_line_numbers(patch: str | None) -> set[int]:
    """New-side line numbers a review may comment on.

    GitHub anchors review comments to lines present in the diff. Added and
    context lines have a new-side number; removed lines do not. A file with no
    patch (binary/too large) yields an empty set — nothing is commentable.
    """
    if not patch:
        return set()
    lines: set[int] = set()
    new_line = 0
    for raw in patch.splitlines():
        if raw.startswith("@@"):
            match = _HUNK_RE.match(raw)
            new_line = int(match.group(1)) if match else 0
            continue
        if new_line == 0:
            continue  # content before the first hunk header
        if raw.startswith("+"):
            lines.add(new_line)
            new_line += 1
        elif raw.startswith("-"):
            continue  # removed: no new-side line number
        elif raw.startswith("\\"):
            continue  # "\ No newline at end of file"
        else:
            lines.add(new_line)  # context line
            new_line += 1
    return lines


def commentable_lines(files: tuple[PullRequestFile, ...]) -> dict[str, set[int]]:
    return {f.path: diff_line_numbers(f.patch) for f in files}


# --- Service ---------------------------------------------------------------


class PRReviewService:
    """Generates (and, on request, posts) the review of one pull request."""

    def __init__(
        self,
        db: Session,
        adapter=None,
        completion_provider: llm.CompletionProvider | None = None,
        min_confidence: float | None = None,
    ) -> None:
        settings = get_settings()
        self.db = db
        self._adapter = adapter
        self._completion = completion_provider
        self.min_confidence = (
            min_confidence
            if min_confidence is not None
            else settings.review_min_confidence
        )
        self.report = ReviewReport()

    def _adapter_for(self, repo: Repo):
        if self._adapter is not None:
            return self._adapter
        from axon.adapters.github.adapter import GitHubAdapter  # noqa: PLC0415
        from axon.adapters.github.app_auth import token_for_repo  # noqa: PLC0415

        return GitHubAdapter(repo.full_name, token=token_for_repo(repo))

    # -- generate ----------------------------------------------------------

    def review(self, repo: Repo, pr_number: int) -> PullRequestReview:
        """Review one PR revision. Idempotent per head_sha: an existing review
        of the same revision is returned untouched (a new push re-reviews)."""
        started = time.monotonic()
        settings = get_settings()
        adapter = self._adapter_for(repo)
        pull: PullRequestInfo = adapter.fetch_pull(pr_number)

        existing = self._existing(repo, pr_number, pull.head_sha)
        if existing is not None and existing.status != ReviewStatus.FAILED:
            logger.info(
                "pr #%s @%s already reviewed — returning stored review",
                pr_number, pull.head_sha[:7],
            )
            return existing

        files = adapter.fetch_pr_diff(pr_number, limit=settings.review_max_files)
        self.report.files_reviewed = len(files)
        if not files:
            return self._persist(
                repo, pull, existing, status=ReviewStatus.FAILED,
                summary=None, comments=[],
                error="pull request has no reviewable file changes",
            )

        claims = self._claims_in_scope(repo, [f.path for f in files])
        self.report.claims_in_scope = len(claims)

        try:
            output = llm.complete(
                build_user_prompt(
                    title=pull.title,
                    body=pull.body,
                    author=pull.author,
                    files=self._render_files(files, settings.review_max_diff_chars),
                    claims=claims,
                ),
                PRReviewOutput,
                system=SYSTEM_PROMPT,
                provider=self._completion,
            )
        except llm.LLMError as exc:
            self.report.llm_failures += 1
            return self._persist(
                repo, pull, existing, status=ReviewStatus.FAILED,
                summary=None, comments=[], error=f"LLM failure: {exc}"[:500],
            )

        kept = self._gate(output.comments, commentable_lines(files))
        review = self._persist(
            repo, pull, existing, status=ReviewStatus.GENERATED,
            summary=output.summary, comments=kept, error=None,
        )
        self.report.duration_s = time.monotonic() - started
        logger.info("repo %s %s", repo.full_name, self.report.summary_line())
        return review

    def _gate(
        self, comments: list[ReviewComment], allowed: dict[str, set[int]]
    ) -> list[dict]:
        """The grounding contract, enforced. A comment survives only if its
        lens is known, its confidence clears the bar, and its (path, line)
        exists in the diff."""
        kept: list[dict] = []
        self.report.comments_proposed = len(comments)
        for comment in comments:
            if comment.lens not in LENSES:
                self.report.dropped_ungrounded += 1
                continue
            if comment.confidence < self.min_confidence:
                self.report.dropped_low_confidence += 1
                continue
            if comment.line not in allowed.get(comment.path, set()):
                self.report.dropped_ungrounded += 1
                self.report.dropped_paths.append(f"{comment.path}:{comment.line}")
                continue
            kept.append(
                {
                    "path": comment.path,
                    "line": comment.line,
                    "lens": comment.lens,
                    "severity": comment.severity,
                    "body": comment.body,
                    "confidence": comment.confidence,
                }
            )
            if comment.lens == "truth":
                self.report.truth_comments += 1
            else:
                self.report.code_comments += 1
        self.report.comments_kept = len(kept)
        return kept

    def _claims_in_scope(self, repo: Repo, paths: list[str]) -> list[dict]:
        """Claims the graph links to the files this PR touches — the truth
        lens's raw material. Reuses the drift engine's planner: a transient
        (unsaved) Event carries the empty payload it reads."""
        settings = get_settings()
        plan = ScopedVerificationPlanner(self.db).plan(repo, Event(payload={}), paths)
        if not plan.impacted_claim_ids:
            return []
        rows = self.db.scalars(
            select(Claim)
            .where(Claim.id.in_(plan.impacted_claim_ids))
            .limit(settings.review_max_claims)
        ).all()
        claims: list[dict] = []
        for claim in rows:
            source = self.db.get(Entity, claim.source_entity_id)
            claims.append(
                {
                    "statement": claim.statement,
                    "status": claim.status.value,
                    "source": source.path if source else None,
                }
            )
        return claims

    @staticmethod
    def _render_files(
        files: tuple[PullRequestFile, ...], max_chars: int
    ) -> list[dict]:
        """Diff payload for the prompt within a char budget: a huge PR
        degrades (later files dropped) instead of failing. The first file is
        always included — truncated if it alone exceeds the budget — so the
        model never receives an empty diff.

        Truncation is safe for grounding: the gate validates comments against
        the FULL diff, so a dropped hunk can only cost a comment, never
        produce an ungrounded one.
        """
        rendered: list[dict] = []
        used = 0
        for f in files:
            patch = f.patch or ""
            remaining = max_chars - used
            if len(patch) > remaining:
                if rendered:
                    break  # later file doesn't fit — stop here
                patch = patch[:max_chars] + "\n… (diff truncated)"
            used += len(patch)
            rendered.append(
                {"path": f.path, "status": f.status, "patch": patch or f.patch}
            )
        return rendered

    # -- persistence -------------------------------------------------------

    def _existing(
        self, repo: Repo, pr_number: int, head_sha: str
    ) -> PullRequestReview | None:
        return self.db.scalar(
            select(PullRequestReview).where(
                PullRequestReview.repo_id == repo.id,
                PullRequestReview.pr_number == pr_number,
                PullRequestReview.head_sha == head_sha,
            )
        )

    def _persist(
        self,
        repo: Repo,
        pull: PullRequestInfo,
        existing: PullRequestReview | None,
        *,
        status: ReviewStatus,
        summary: str | None,
        comments: list[dict],
        error: str | None,
    ) -> PullRequestReview:
        review = existing or PullRequestReview(
            repo_id=repo.id, pr_number=pull.number, head_sha=pull.head_sha
        )
        review.pr_title = pull.title
        review.summary = summary
        review.comments = comments
        review.status = status
        review.error = error
        if existing is None:
            self.db.add(review)
        self.db.commit()
        self.db.refresh(review)
        return review

    # -- publish -----------------------------------------------------------

    def post_review(self, review: PullRequestReview) -> PullRequestReview:
        """Publish a stored review to GitHub as the app's bot identity.

        Idempotent: an already-posted review is returned untouched, so a
        double click never produces two GitHub reviews.
        """
        if review.status == ReviewStatus.POSTED:
            return review
        if review.status != ReviewStatus.GENERATED:
            raise ValueError(f"review is {review.status.value}, not postable")

        repo = self.db.get(Repo, review.repo_id)
        adapter = self._adapter_for(repo)
        comments = [
            {
                "path": c["path"],
                "line": c["line"],
                "side": "RIGHT",
                "body": _render_comment_body(c),
            }
            for c in (review.comments or [])
        ]
        url = adapter.create_review(
            review.pr_number,
            body=_render_review_body(review),
            comments=comments,
            commit_id=review.head_sha,
            event="COMMENT",
        )
        review.status = ReviewStatus.POSTED
        review.review_url = url
        review.error = None
        self.db.commit()
        self.db.refresh(review)
        logger.info("posted review for PR #%s: %s", review.pr_number, url)
        return review


_LENS_LABEL = {"code": "Code review", "truth": "Truth maintenance"}


def _render_comment_body(comment: dict) -> str:
    lens = _LENS_LABEL.get(comment.get("lens", ""), "Review")
    severity = str(comment.get("severity", "")).upper()
    return f"**{lens} · {severity}**\n\n{comment.get('body', '')}"


def _render_review_body(review: PullRequestReview) -> str:
    comments = review.comments or []
    truth = sum(1 for c in comments if c.get("lens") == "truth")
    code = len(comments) - truth
    counts = f"{code} code · {truth} truth-maintenance"
    return (
        f"{review.summary or ''}\n\n"
        f"---\n"
        f"_{len(comments)} comment(s) ({counts}). Reviewed by Axon against "
        f"`{review.head_sha[:7]}`; every comment is anchored to a line in this "
        f"diff, and truth-maintenance comments cite claims Axon has verified "
        f"from your documentation._\n\n"
        f"🤖 Generated with Axon · prompt {PROMPT_VERSION}"
    )
