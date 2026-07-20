"""GitHub PR generation — a DETERMINISTIC renderer of remediation records.

No LLM calls. No regeneration. Every byte of the pull request — patch,
branch name, commit message, title, body — is rendered from the persisted
Fix proposal (T3.2) and its finding's provenance. Same fix → same PR,
bit for bit.

Flow:  generated Fix → locate target file (current default branch)
       → verify original_excerpt still applies (exactly once)
       → apply replacement (pure function)
       → ensure branch axon/fix-{id} → one commit → one PR → persist URL.

Terminal data outcomes are fix states, not errors:
  stale      excerpt no longer present (file moved on) → status=failed,
             error="stale: …". T3.2's remediation pass may later regenerate
             a fresh proposal from the new text — never this service.
  ambiguous  excerpt occurs more than once → replacing "the first" would be
             a guess, and this renderer does not guess.
Only transient GitHub failures raise (AdapterError) — the job queue's
retry/backoff applies, and every retry re-enters this idempotent flow.

Idempotency anchors: the status gate (pr_opened is never reprocessed), the
deterministic branch name, and recovery probes — an existing PR for the
branch is adopted; an existing branch whose file already matches the
patched content skips the commit. One commit per fix, one PR per fix.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from typing import Literal, Protocol

from sqlalchemy.orm import Session

from axon.db.models import Finding, FindingStatus, Fix, FixStatus, Repo
from axon.services.remediation import load_proposal

logger = logging.getLogger("axon.services.pr")


class WriteCapableRepo(Protocol):
    """The five write primitives a provider adapter must offer."""

    def get_branch_head(self, branch: str) -> str: ...
    def branch_exists(self, branch: str) -> bool: ...
    def create_branch(self, branch: str, from_sha: str) -> None: ...
    def fetch_file_with_sha(
        self, path: str, ref: str | None = None
    ) -> tuple[bytes, str] | None: ...
    def put_file(
        self, path: str, content: bytes, message: str, branch: str, sha: str
    ) -> None: ...
    def find_pull(self, head_branch: str) -> str | None: ...
    def create_pull(self, title: str, body: str, head: str, base: str) -> str: ...


# --- Pure, deterministic patch application --------------------------------


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n")


def apply_replacement(
    text: str, excerpt: str, replacement: str
) -> tuple[str | None, str]:
    """Replace ``excerpt`` with ``replacement`` in ``text``.

    Returns (new_text, how) on success; (None, reason) otherwise. Exact
    unique substring first; then a whitespace-flexible unique match. More
    than one occurrence is ambiguous → no patch.
    """
    text_n = _normalize_newlines(text)
    excerpt_n = _normalize_newlines(excerpt)
    replacement_n = _normalize_newlines(replacement)
    if not excerpt_n.strip():
        return None, "stale: empty excerpt"

    count = text_n.count(excerpt_n)
    if count == 1:
        return text_n.replace(excerpt_n, replacement_n, 1), "exact"
    if count > 1:
        return None, f"ambiguous: excerpt occurs {count} times in target"

    tokens = excerpt_n.split()
    pattern = re.compile(r"\s+".join(re.escape(token) for token in tokens))
    matches = list(pattern.finditer(text_n))
    if len(matches) == 1:
        match = matches[0]
        return (
            text_n[: match.start()] + replacement_n + text_n[match.end() :],
            "whitespace-flexible",
        )
    if len(matches) > 1:
        return None, f"ambiguous: excerpt matches {len(matches)} regions"
    return None, "stale: original excerpt no longer present in target file"


def branch_name_for(fix: Fix) -> str:
    return f"axon/fix-{fix.id.hex[:12]}"


def render_commit_message(proposal: dict) -> str:
    return (
        f"{proposal['title']}\n\n"
        f"{proposal['explanation']}\n\n"
        f"Axon fix {proposal['finding_id']}"
    )


def render_pr_body(proposal: dict) -> str:
    quotes = "\n\n".join(
        f"```{(q.get('language') or '')}\n{q.get('text', '')}\n```\n"
        f"— `{q.get('path') or 'source'}`"
        + (f" line {q['start_line']}" if q.get("start_line") else "")
        for q in proposal.get("evidence", [])
    )
    return (
        f"{proposal['explanation']}\n\n"
        f"### Contradicted claim\n> {proposal['claim_statement']}\n\n"
        f"### Evidence (verbatim from the current code)\n{quotes}\n\n"
        f"### Change\nReplaces the drifted text in `{proposal['target_path']}` "
        f"with wording that matches the implementation. Confidence: "
        f"{proposal['confidence']:.0%}.\n\n"
        f"---\n_Axon finding `{proposal['finding_id']}` · claim "
        f"`{proposal['claim_id']}` · rendered deterministically from verified "
        f"evidence — no content was generated at PR time._\n\n"
        f"🤖 Generated with Axon"
    )


# --- Service ---------------------------------------------------------------


@dataclass(frozen=True)
class PROutcome:
    status: Literal[
        "opened", "already_open", "stale", "skipped_issue_target", "invalid_state"
    ]
    pr_url: str | None = None
    reason: str = ""


class GitHubPRService:
    """Turns one generated Fix into one pull request."""

    def __init__(self, db: Session, adapter: WriteCapableRepo | None = None) -> None:
        self.db = db
        self._adapter = adapter

    def _adapter_for(self, repo: Repo) -> WriteCapableRepo:
        if self._adapter is not None:
            return self._adapter
        from axon.adapters.github.adapter import GitHubAdapter  # noqa: PLC0415

        return GitHubAdapter(repo.full_name, token=repo.settings.get("token"))

    def open_pr_for_fix(self, fix_id: uuid.UUID) -> PROutcome:
        fix = self.db.get(Fix, fix_id)
        if fix is None:
            return PROutcome(status="invalid_state", reason="fix not found")
        if fix.status == FixStatus.PR_OPENED:
            return PROutcome(
                status="already_open", pr_url=fix.pr_url,
                reason="fix already has a pull request",
            )
        if fix.status != FixStatus.GENERATED:
            return PROutcome(
                status="invalid_state",
                reason=f"fix is {fix.status.value}, not generated",
            )
        proposal = load_proposal(fix)
        if proposal is None:
            return PROutcome(status="invalid_state", reason="fix has no proposal")
        if proposal.get("target_kind") != "doc":
            # Issue targets are resolution comments, not patches — a later
            # capability, and never a PR. The fix stays generated.
            return PROutcome(
                status="skipped_issue_target",
                reason="issue-target proposals are not patchable via PR",
            )

        finding: Finding = self.db.get(Finding, fix.finding_id)
        repo: Repo = self.db.get(Repo, finding.repo_id)
        adapter = self._adapter_for(repo)
        branch = branch_name_for(fix)
        base = repo.default_branch

        # Recovery probe: a PR may already exist (crash after creation,
        # or lost URL). Never open a duplicate.
        existing_url = adapter.find_pull(branch)
        if existing_url:
            self._mark_opened(fix, finding, existing_url)
            return PROutcome(
                status="already_open", pr_url=existing_url,
                reason="adopted existing pull request for this fix's branch",
            )

        # 1-2. Locate target + verify the excerpt still applies (against
        # the CURRENT default branch — the patch must be true today).
        target_path = proposal["target_path"]
        current = adapter.fetch_file_with_sha(target_path, ref=base)
        if current is None:
            return self._mark_stale(fix, f"stale: {target_path} no longer exists")
        current_text = current[0].decode("utf-8", errors="replace")
        new_text, how = apply_replacement(
            current_text,
            proposal["original_excerpt"],
            proposal["suggested_replacement"],
        )
        if new_text is None:
            return self._mark_stale(fix, how)

        # 3. Ensure the branch.
        if not adapter.branch_exists(branch):
            adapter.create_branch(branch, adapter.get_branch_head(base))

        # 4. One commit — skipped if a prior run already made it.
        on_branch = adapter.fetch_file_with_sha(target_path, ref=branch)
        branch_text = (
            on_branch[0].decode("utf-8", errors="replace") if on_branch else None
        )
        if branch_text is None or _normalize_newlines(branch_text) != new_text:
            blob_sha = on_branch[1] if on_branch else current[1]
            adapter.put_file(
                target_path,
                new_text.encode("utf-8"),
                render_commit_message(proposal),
                branch,
                blob_sha,
            )
            logger.info(
                "fix %s: committed patch to %s (%s match)", fix.id, branch, how
            )
        else:
            logger.info("fix %s: patch already committed on %s", fix.id, branch)

        # 5. One PR.
        pr_url = adapter.create_pull(
            proposal["title"], render_pr_body(proposal), head=branch, base=base
        )
        self._mark_opened(fix, finding, pr_url)
        logger.info("fix %s: pull request %s", fix.id, pr_url)
        return PROutcome(status="opened", pr_url=pr_url)

    # -- state transitions --------------------------------------------------

    def _mark_opened(self, fix: Fix, finding: Finding, pr_url: str) -> None:
        fix.status = FixStatus.PR_OPENED
        fix.pr_url = pr_url
        fix.error = None
        if finding.status == FindingStatus.OPEN:
            finding.status = FindingStatus.ACTIONED
        self.db.commit()

    def _mark_stale(self, fix: Fix, reason: str) -> PROutcome:
        fix.status = FixStatus.FAILED
        fix.error = reason[:500]
        self.db.commit()
        logger.warning("fix %s rejected: %s", fix.id, reason)
        return PROutcome(status="stale", reason=reason)
