"""Drift verification: claims vs current reality → status flips + findings.

Pipeline (DriftVerifier.run):

    unchecked claims, strongest-link first, budget-capped
        ▼
    gather current source for each claim's linked entities
      (code files fetched on demand — the DB stores no code content;
       doc targets reassembled from their sections' stored text)
        ▼
    verification prompt → structured verdict
        ▼
    EVIDENCE GATE (code-enforced): a CONTRADICTED/VERIFIED verdict whose
    quote is empty or not verbatim-present in the supplied sources is
    downgraded to INSUFFICIENT_EVIDENCE. A finding without real evidence
    is structurally impossible.
        ▼
    VERIFIED      → claim.status=verified, last_verified_at stamped
    CONTRADICTED  → claim.status=contradicted + ONE open finding per claim
                    (created or updated, never duplicated), evidence in the
                    FindingOut contract shape, event=None (at-rest scan)
    INSUFFICIENT  → counted; nothing changes (conservative bias)

False-alarm posture: this service tells an organization its documentation
is lying. Every guard errs toward silence — INSUFFICIENT is free; a wrong
CONTRADICTED costs the product its credibility.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from axon.config import get_settings
from axon.db.models import (
    Claim,
    ClaimStatus,
    Entity,
    EntityKind,
    Event,
    Finding,
    FindingKind,
    FindingSeverity,
    FindingStatus,
    Repo,
)
from axon.llm import provider as llm
from axon.llm.prompts.drift_verification import SYSTEM_PROMPT, build_user_prompt

logger = logging.getLogger("axon.services.verification")

SEVERITY_HIGH_CONF = 0.9
SEVERITY_MEDIUM_CONF = 0.7

FetchFile = Callable[[str], bytes | None]


class Verdict(BaseModel):
    verdict: Literal["VERIFIED", "CONTRADICTED", "INSUFFICIENT_EVIDENCE"]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_quote: str | None
    evidence_path: str | None
    explanation: str


@dataclass(frozen=True)
class Source:
    path: str
    text: str
    base_line: int  # line number of text's first line within the real file


@dataclass
class VerifyReport:
    claims_considered: int = 0
    claims_checked: int = 0
    verified: int = 0
    contradicted: int = 0
    insufficient: int = 0
    skipped_no_links: int = 0
    skipped_no_source: int = 0
    findings_created: int = 0
    findings_updated: int = 0
    findings_resolved: int = 0
    evidence_guard_downgrades: int = 0
    llm_failures: int = 0
    duration_s: float = 0.0

    def summary(self) -> str:
        return (
            f"claims: considered={self.claims_considered} checked={self.claims_checked} "
            f"skipped(no links)={self.skipped_no_links} "
            f"skipped(no source)={self.skipped_no_source}\n"
            f"verdicts: verified={self.verified} contradicted={self.contradicted} "
            f"insufficient={self.insufficient} "
            f"evidence-guard downgrades={self.evidence_guard_downgrades}\n"
            f"findings: created={self.findings_created} updated={self.findings_updated} "
            f"resolved={self.findings_resolved}\n"
            f"llm failures: {self.llm_failures}   duration: {self.duration_s:.1f}s"
        )


def _normalize_ws(text: str) -> str:
    return " ".join(text.split())


def quote_in_sources(quote: str, sources: list[Source]) -> Source | None:
    """The honesty check: is the quote genuinely (whitespace-normalized)
    present in what the model was shown?"""
    needle = _normalize_ws(quote)
    if not needle:
        return None
    for source in sources:
        if needle in _normalize_ws(source.text):
            return source
    return None


def locate_quote(quote: str, source: Source) -> int | None:
    """Best-effort line number of the quote's first line."""
    first = quote.strip().splitlines()[0].strip() if quote.strip() else ""
    if not first:
        return None
    for offset, line in enumerate(source.text.splitlines()):
        if first in line:
            return source.base_line + offset
    return None


class DriftVerifier:
    """Verifies one repository's claims against current sources.

    ``fetch_file`` retrieves current code content by repo-relative path;
    defaults to the GitHub contents API via the repo's stored token.
    Providers are injectable for tests.
    """

    def __init__(
        self,
        db: Session,
        completion_provider: llm.CompletionProvider | None = None,
        fetch_file: FetchFile | None = None,
        budget: int | None = None,
        max_source_chars: int | None = None,
        event: "Event | None" = None,
    ) -> None:
        settings = get_settings()
        self.db = db
        self._completion = completion_provider
        self._fetch_file = fetch_file
        # Provenance: event-driven passes stamp findings with the event
        # that triggered them ("caused by PR #47" in the feed).
        self._event = event
        self.budget = budget if budget is not None else settings.verify_budget
        self.max_source_chars = (
            max_source_chars
            if max_source_chars is not None
            else settings.verify_max_source_chars
        )
        self.report = VerifyReport()
        self._file_cache: dict[str, str | None] = {}

    # -- public ------------------------------------------------------------

    def run(self, repo: Repo, claim_ids: list | None = None) -> VerifyReport:
        started = time.monotonic()
        if self._fetch_file is None:
            self._fetch_file = self._default_fetcher(repo)

        query = (
            select(Claim)
            .options(joinedload(Claim.links), joinedload(Claim.source_entity))
            .where(Claim.repo_id == repo.id)
        )
        if claim_ids is not None:
            query = query.where(Claim.id.in_(claim_ids))
        else:
            query = query.where(Claim.status == ClaimStatus.UNCHECKED)
        claims = list(self.db.scalars(query).unique())
        self.report.claims_considered = len(claims)

        # Strongest evidence first: claims with the best links are the ones
        # a budgeted pass should spend on (architecture §9).
        claims.sort(
            key=lambda c: max((l.strength for l in c.links), default=0.0),
            reverse=True,
        )

        for claim in claims[: self.budget]:
            self._verify_claim(repo, claim)

        self.db.commit()
        self.report.duration_s = time.monotonic() - started
        logger.info(
            "verification for %s finished\n%s", repo.full_name, self.report.summary()
        )
        return self.report

    # -- internals -----------------------------------------------------------

    def _default_fetcher(self, repo: Repo) -> FetchFile:
        from axon.adapters.github.adapter import GitHubAdapter  # noqa: PLC0415
        from axon.adapters.github.app_auth import token_for_repo  # noqa: PLC0415

        adapter = GitHubAdapter(repo.full_name, token=token_for_repo(repo))
        return adapter.fetch_file

    def _verify_claim(self, repo: Repo, claim: Claim) -> None:
        if not claim.links:
            self.report.skipped_no_links += 1
            return
        sources = self._gather_sources(claim)
        if not sources:
            self.report.skipped_no_source += 1
            return

        try:
            verdict = llm.complete(
                build_user_prompt(
                    claim.statement,
                    claim.claim_type.value,
                    [(s.path, s.text) for s in sources],
                ),
                Verdict,
                system=SYSTEM_PROMPT,
                provider=self._completion,
            )
        except llm.LLMError:
            self.report.llm_failures += 1
            logger.exception("verification LLM call failed for claim %s", claim.id)
            return
        self.report.claims_checked += 1

        # --- evidence gate ---------------------------------------------
        evidence_source: Source | None = None
        if verdict.verdict in ("VERIFIED", "CONTRADICTED"):
            evidence_source = (
                quote_in_sources(verdict.evidence_quote or "", sources)
            )
            if evidence_source is None:
                logger.warning(
                    "evidence guard: %s verdict without verbatim quote for "
                    "claim %s — downgraded to INSUFFICIENT_EVIDENCE",
                    verdict.verdict, claim.id,
                )
                self.report.evidence_guard_downgrades += 1
                self.report.insufficient += 1
                return

        if verdict.verdict == "VERIFIED":
            claim.status = ClaimStatus.VERIFIED
            claim.last_verified_at = datetime.now(timezone.utc)
            self.report.verified += 1
            self._resolve_open_findings(claim)
            return

        if verdict.verdict == "INSUFFICIENT_EVIDENCE":
            self.report.insufficient += 1
            return

        # CONTRADICTED — evidence already validated
        claim.status = ClaimStatus.CONTRADICTED
        self.report.contradicted += 1
        self._persist_finding(repo, claim, verdict, evidence_source)

    def _gather_sources(self, claim: Claim) -> list[Source]:
        sources: list[Source] = []
        for link in sorted(claim.links, key=lambda l: -l.strength):
            entity = self.db.get(Entity, link.entity_id)
            if entity is None or entity.path is None:
                continue
            if entity.kind == EntityKind.CODE_FILE:
                text = self._file_text(entity.path)
                if text:
                    sources.append(Source(entity.path, text, base_line=1))
            elif entity.kind == EntityKind.DOC:
                section_text = self._doc_text(entity)
                if section_text:
                    sources.append(section_text)
        return sources

    def _file_text(self, path: str) -> str | None:
        if path not in self._file_cache:
            raw = self._fetch_file(path) if self._fetch_file else None
            text = raw.decode("utf-8", errors="replace") if raw else None
            if text and len(text) > self.max_source_chars:
                text = text[: self.max_source_chars] + "\n… [truncated]"
            self._file_cache[path] = text
        return self._file_cache[path]

    def _doc_text(self, doc: Entity) -> Source | None:
        """Docs store their text in section entities — reassemble."""
        sections = self.db.scalars(
            select(Entity).where(
                Entity.repo_id == doc.repo_id,
                Entity.kind == EntityKind.DOC_SECTION,
                Entity.path.like(f"{doc.path}#%"),
            )
        ).all()
        if not sections:
            return None
        sections.sort(key=lambda s: (s.meta or {}).get("start_line") or 0)
        text = "\n\n".join((s.meta or {}).get("text", "") for s in sections)
        base = min(((s.meta or {}).get("start_line") or 1) for s in sections)
        if len(text) > self.max_source_chars:
            text = text[: self.max_source_chars] + "\n… [truncated]"
        return Source(doc.path, text, base_line=base)

    def _resolve_open_findings(self, claim: Claim) -> None:
        """Reality re-verified a claim: its open findings are moot. A
        finding auto-closed by fresh evidence is 'dismissed' — 'actioned'
        stays reserved for humans/fix PRs."""
        open_findings = self.db.scalars(
            select(Finding).where(
                Finding.claim_id == claim.id,
                Finding.status == FindingStatus.OPEN,
            )
        ).all()
        for finding in open_findings:
            finding.status = FindingStatus.DISMISSED
            self.report.findings_resolved += 1

    def _persist_finding(
        self, repo: Repo, claim: Claim, verdict: Verdict, source: Source
    ) -> None:
        kind = (
            FindingKind.DOC_DRIFT
            if claim.source_entity is not None
            and claim.source_entity.kind == EntityKind.DOC_SECTION
            else FindingKind.STALE_ISSUE
        )
        if verdict.confidence >= SEVERITY_HIGH_CONF:
            severity = FindingSeverity.HIGH
        elif verdict.confidence >= SEVERITY_MEDIUM_CONF:
            severity = FindingSeverity.MEDIUM
        else:
            severity = FindingSeverity.LOW

        language = source.path.rsplit(".", 1)[-1] if "." in source.path else None
        evidence = {
            "quotes": [
                {
                    "text": verdict.evidence_quote,
                    "path": source.path,
                    "start_line": locate_quote(verdict.evidence_quote or "", source),
                    "language": language,
                }
            ],
            "diff": None,
        }
        anchor = claim.anchor or {}
        suggested = (
            f"Update {anchor.get('path') or 'the source document'} "
            f"to match the current implementation."
        )

        existing = self.db.scalars(
            select(Finding).where(
                Finding.claim_id == claim.id,
                Finding.status == FindingStatus.OPEN,
            )
        ).first()
        if existing is not None:
            existing.kind = kind
            existing.severity = severity
            existing.explanation = verdict.explanation
            existing.evidence = evidence
            existing.suggested_action = suggested
            if self._event is not None:
                existing.event_id = self._event.id  # latest trigger wins
            self.report.findings_updated += 1
            return

        self.db.add(
            Finding(
                repo_id=repo.id,
                claim_id=claim.id,
                # provenance: the reality event that triggered this pass
                # (None for at-rest scans)
                event_id=self._event.id if self._event is not None else None,
                kind=kind,
                severity=severity,
                explanation=verdict.explanation,
                evidence=evidence,
                suggested_action=suggested,
                status=FindingStatus.OPEN,
            )
        )
        self.report.findings_created += 1
