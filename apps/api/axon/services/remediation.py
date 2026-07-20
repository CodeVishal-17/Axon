"""Remediation planning: contradicted findings → grounded fix proposals.

Position in the loop: strictly downstream of verification. This service
consumes FINDINGS (open, with still-contradicted claims), never raw
claims, and persists proposals into the existing ``fixes`` table — whose
unique finding_id already guarantees at most one remediation per finding.

Grounding contract (never invent facts), enforced in code:
  * excerpt gate  — original_excerpt must appear verbatim (whitespace-
                    normalized) in the target's actual current text; a
                    proposal that misquotes what it fixes is rejected.
  * value gate    — every number in suggested_replacement must appear in
                    the evidence quotes or the original excerpt; numbers
                    (TTLs, ports, limits) are where invention does damage.
  * confidence    — proposals below REMEDIATION_MIN_CONFIDENCE are
                    rejected.
Rejections and LLM failures persist as Fix(status=failed, error=reason) —
visible, retried on the next pass, and never surfaced as suggestions.

The persisted JSON (fixes.patch) is a complete patch instruction for the
future PR generator: target + verbatim original + replacement + provenance.
No GitHub writes happen here.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from axon.config import get_settings
from axon.db.models import (
    Claim,
    ClaimStatus,
    EntityKind,
    Finding,
    FindingStatus,
    Fix,
    FixStatus,
    Repo,
)
from axon.llm import provider as llm
from axon.llm.prompts.remediation import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_user_prompt,
)

logger = logging.getLogger("axon.services.remediation")

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


class RemediationProposal(BaseModel):
    """Structured output schema — also the shape persisted in fixes.patch."""

    title: str
    explanation: str
    original_excerpt: str
    suggested_replacement: str
    confidence: float = Field(ge=0.0, le=1.0)


@dataclass
class RemediationReport:
    findings_considered: int = 0
    proposals_created: int = 0
    retried_after_failure: int = 0
    skipped_existing: int = 0
    skipped_not_eligible: int = 0
    skipped_no_source_text: int = 0
    rejected_ungrounded_excerpt: int = 0
    rejected_ungrounded_values: int = 0
    rejected_low_confidence: int = 0
    llm_failures: int = 0
    duration_s: float = 0.0

    def summary(self) -> str:
        return (
            f"findings: considered={self.findings_considered} "
            f"proposals={self.proposals_created} retried={self.retried_after_failure} "
            f"skipped(existing)={self.skipped_existing} "
            f"skipped(ineligible)={self.skipped_not_eligible} "
            f"skipped(no source)={self.skipped_no_source_text}\n"
            f"rejected: excerpt={self.rejected_ungrounded_excerpt} "
            f"values={self.rejected_ungrounded_values} "
            f"confidence={self.rejected_low_confidence} "
            f"llm failures={self.llm_failures}\n"
            f"duration: {self.duration_s:.1f}s"
        )


def _normalize_ws(text: str) -> str:
    return " ".join(text.split())


def excerpt_in_text(excerpt: str, text: str) -> bool:
    needle = _normalize_ws(excerpt)
    return bool(needle) and needle in _normalize_ws(text)


def unsupported_numbers(replacement: str, allowed_material: str) -> list[str]:
    """Numbers in the replacement that appear nowhere in evidence/original."""
    allowed = set(_NUMBER_RE.findall(allowed_material))
    return sorted(
        {n for n in _NUMBER_RE.findall(replacement) if n not in allowed}
    )


class RemediationService:
    """Generates and persists remediation proposals for one repository."""

    def __init__(
        self,
        db: Session,
        completion_provider: llm.CompletionProvider | None = None,
        budget: int | None = None,
        min_confidence: float | None = None,
    ) -> None:
        settings = get_settings()
        self.db = db
        self._completion = completion_provider
        self.budget = budget if budget is not None else settings.remediation_budget
        self.min_confidence = (
            min_confidence
            if min_confidence is not None
            else settings.remediation_min_confidence
        )
        self.report = RemediationReport()

    # -- public ------------------------------------------------------------

    def run(self, repo: Repo) -> RemediationReport:
        started = time.monotonic()
        findings = self.db.scalars(
            select(Finding)
            .options(joinedload(Finding.claim).joinedload(Claim.source_entity))
            .where(
                Finding.repo_id == repo.id,
                Finding.status == FindingStatus.OPEN,
            )
            .order_by(Finding.created_at)
        ).all()
        self.report.findings_considered = len(findings)

        handled = 0
        for finding in findings:
            if handled >= self.budget:
                break
            if self._handle_finding(repo, finding):
                handled += 1

        self.db.commit()
        self.report.duration_s = time.monotonic() - started
        logger.info(
            "remediation for %s finished\n%s", repo.full_name, self.report.summary()
        )
        return self.report

    # -- internals ---------------------------------------------------------

    def _handle_finding(self, repo: Repo, finding: Finding) -> bool:
        """Returns True when a budget slot was consumed (LLM attempted)."""
        claim = finding.claim
        # ONLY contradicted beliefs get remediation. A claim re-verified
        # since the finding opened, or any non-contradicted state, is
        # ineligible — VERIFIED / INSUFFICIENT never generate proposals.
        if claim is None or claim.status != ClaimStatus.CONTRADICTED:
            self.report.skipped_not_eligible += 1
            return False

        existing = self.db.scalars(
            select(Fix).where(Fix.finding_id == finding.id)
        ).first()
        if existing is not None and existing.status != FixStatus.FAILED:
            self.report.skipped_existing += 1
            return False

        target_kind, target_ref, document_text = self._target(claim)
        if not document_text:
            self.report.skipped_no_source_text += 1
            return False

        quotes = (finding.evidence or {}).get("quotes") or []
        try:
            proposal = llm.complete(
                build_user_prompt(
                    statement=claim.statement,
                    explanation=finding.explanation,
                    evidence_quotes=quotes,
                    target_kind=target_kind,
                    target_ref=target_ref,
                    document_text=document_text,
                ),
                RemediationProposal,
                system=SYSTEM_PROMPT,
                provider=self._completion,
            )
        except llm.LLMError as exc:
            self.report.llm_failures += 1
            self._persist(finding, existing, status=FixStatus.FAILED,
                          error=f"LLM failure: {exc}"[:500], payload=None)
            return True

        rejection = self._gate(proposal, document_text, quotes)
        if rejection is not None:
            self._persist(finding, existing, status=FixStatus.FAILED,
                          error=rejection, payload=None)
            return True

        payload = {
            "prompt_version": PROMPT_VERSION,
            "title": proposal.title,
            "explanation": proposal.explanation,
            "target_kind": target_kind,
            "target_path": target_ref,
            "original_excerpt": proposal.original_excerpt,
            "suggested_replacement": proposal.suggested_replacement,
            "confidence": proposal.confidence,
            "claim_id": str(claim.id),
            "claim_statement": claim.statement,
            "finding_id": str(finding.id),
            "evidence": quotes,
        }
        if existing is not None:
            self.report.retried_after_failure += 1
        self._persist(finding, existing, status=FixStatus.GENERATED,
                      error=None, payload=payload)
        self.report.proposals_created += 1
        return True

    def _gate(
        self, proposal: RemediationProposal, document_text: str, quotes: list[dict]
    ) -> str | None:
        """The never-invent-facts contract, enforced. Returns a rejection
        reason or None."""
        if not excerpt_in_text(proposal.original_excerpt, document_text):
            self.report.rejected_ungrounded_excerpt += 1
            return (
                "ungrounded excerpt: original_excerpt does not appear "
                "verbatim in the target text"
            )
        allowed_material = document_text + " " + " ".join(
            q.get("text", "") for q in quotes
        )
        invented = unsupported_numbers(
            proposal.suggested_replacement, allowed_material
        )
        if invented:
            self.report.rejected_ungrounded_values += 1
            return (
                f"ungrounded values: replacement contains numbers not present "
                f"in evidence or original: {invented}"
            )
        if proposal.confidence < self.min_confidence:
            self.report.rejected_low_confidence += 1
            return (
                f"below confidence threshold "
                f"({proposal.confidence:.2f} < {self.min_confidence})"
            )
        return None

    def _target(self, claim: Claim) -> tuple[Literal["doc", "issue"], str, str]:
        """(kind, patch target reference, current text of the belief)."""
        source = claim.source_entity
        if source is None:
            return "doc", "", ""
        if source.kind == EntityKind.DOC_SECTION:
            meta = source.meta or {}
            path = (claim.anchor or {}).get("path") or meta.get("doc_path") or ""
            return "doc", path, meta.get("text", "")
        # issue / pull_request sources
        meta = source.meta or {}
        title = meta.get("title") or source.name
        body = meta.get("body") or ""
        ref = f"{source.kind.value} #{source.external_id}"
        return "issue", ref, f"Title: {title}\n\n{body}".strip()

    def _persist(
        self,
        finding: Finding,
        existing: Fix | None,
        *,
        status: FixStatus,
        error: str | None,
        payload: dict | None,
    ) -> None:
        patch = json.dumps(payload, sort_keys=True) if payload is not None else None
        if existing is not None:
            existing.status = status
            existing.error = error
            existing.patch = patch
            return
        self.db.add(
            Fix(finding_id=finding.id, status=status, error=error, patch=patch)
        )


def load_proposal(fix: Fix) -> dict | None:
    """Deserialize a persisted proposal (None for failed fixes)."""
    if not fix.patch:
        return None
    return json.loads(fix.patch)
