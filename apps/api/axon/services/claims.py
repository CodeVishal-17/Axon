"""Claim extraction pipeline: KnowledgeDoc entities → verified beliefs.

Flow per repository (ClaimExtractionService.run):

    claim-bearing entities (doc_section | issue | pull_request)
        │  skip: entity.meta["claims_hash"] == entity.content_hash
        ▼
    LLM structured extraction (prompt: axon/llm/prompts/claim_extraction.py)
        ▼
    precision gates (code-enforced, not just prompted — see _passes_filters)
        ▼
    repo-wide dedupe + replace-on-change persistence (insert/update/delete)
        ▼
    batched embeddings for new/changed statements

Persistence contract: a claim belongs to its source entity. Re-extracting an
entity updates matched claims in place (statement identity), inserts new
ones, and deletes claims the source no longer asserts. A statement already
claimed by a DIFFERENT entity is skipped as a duplicate — overlapping
sections must not create twin beliefs.

Schema note (schema is final): ``mentioned_paths`` — needed by the entity
linker (T2.3) — is carried inside the anchor JSONB alongside path/lines.
The findings API's AnchorOut ignores extra keys by design.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from axon.config import Settings, get_settings
from axon.db.models import Claim, ClaimStatus, ClaimType, Entity, EntityKind, Repo
from axon.llm import provider as llm
from axon.llm.prompts.claim_extraction import SYSTEM_PROMPT, build_user_prompt

logger = logging.getLogger("axon.services.claims")

CLAIM_BEARING_KINDS = (
    EntityKind.DOC_SECTION,
    EntityKind.ISSUE,
    EntityKind.PULL_REQUEST,
)

MIN_CONFIDENCE = 0.5
MIN_WORDS, MAX_WORDS = 4, 40
# Hedges and future tense signal speculation/plans — claims must be present
# facts. Checked as whole words against the lowercased statement.
BANNED_WORDS = {
    "should", "might", "may", "could", "probably", "possibly", "perhaps",
    "planned", "upcoming", "soon", "todo", "eventually", "hopefully",
}
BANNED_PREFIXES = ("we ", "you ", "please ", "let's ", "this section", "this document")


# --- Structured output schema (strict-mode compatible: all fields required,
# --- optionality via explicit nulls) --------------------------------------


class ExtractedClaim(BaseModel):
    statement: str
    claim_type: Literal["behavior", "architecture", "process", "status"]
    mentioned_paths: list[str]
    start_line: int | None
    end_line: int | None
    confidence: float = Field(ge=0.0, le=1.0)


class ExtractionResult(BaseModel):
    claims: list[ExtractedClaim]


# --- Precision gates ------------------------------------------------------


def normalize_statement(statement: str) -> str:
    cleaned = "".join(
        ch.lower() if ch.isalnum() or ch.isspace() else " " for ch in statement
    )
    return " ".join(cleaned.split())


def _passes_filters(claim: ExtractedClaim) -> tuple[bool, str]:
    """Code-level enforcement of the precision rules. The prompt asks;
    this layer insists."""
    statement = claim.statement.strip()
    words = normalize_statement(statement).split()
    if claim.confidence < MIN_CONFIDENCE:
        return False, f"confidence {claim.confidence:.2f} < {MIN_CONFIDENCE}"
    if not MIN_WORDS <= len(words) <= MAX_WORDS:
        return False, f"length {len(words)} words outside [{MIN_WORDS}, {MAX_WORDS}]"
    banned = BANNED_WORDS.intersection(words)
    if banned:
        return False, f"hedge/future word: {sorted(banned)}"
    lowered = statement.lower()
    if any(lowered.startswith(prefix) for prefix in BANNED_PREFIXES):
        return False, "instruction/meta phrasing"
    if "?" in statement:
        return False, "question, not a claim"
    return True, ""


def _clamp_anchor(
    claim: ExtractedClaim, start_line: int | None, end_line: int | None
) -> tuple[int | None, int | None]:
    """Anchors must stay inside the source section; a hallucinated range is
    replaced by the section bounds rather than trusted."""
    if start_line is None or end_line is None:
        return None, None  # issues: no line numbering
    s = claim.start_line if claim.start_line is not None else start_line
    e = claim.end_line if claim.end_line is not None else end_line
    if s > e:
        s, e = e, s
    s = min(max(s, start_line), end_line)
    e = min(max(e, start_line), end_line)
    return s, e


# --- Shared extraction core (service + eval harness use the same path) ----


def _extract_raw(
    *,
    text: str,
    kind: str,
    path: str | None,
    doc_path: str | None,
    start_line: int | None,
    end_line: int | None,
    repo_paths: list[str] | None = None,
    completion_provider: llm.CompletionProvider | None = None,
) -> list[dict[str, Any]]:
    """One LLM call + filters + anchor clamping → harness-shaped dicts."""
    if not text.strip():
        return []
    result = llm.complete(
        build_user_prompt(
            text=text, kind=kind, path=path, doc_path=doc_path,
            start_line=start_line, repo_paths=repo_paths,
        ),
        ExtractionResult,
        system=SYSTEM_PROMPT,
        provider=completion_provider,
    )
    accepted: list[dict[str, Any]] = []
    seen: set[str] = set()
    for claim in result.claims:
        ok, reason = _passes_filters(claim)
        if not ok:
            logger.info("claim rejected (%s): %s", reason, claim.statement[:120])
            continue
        key = normalize_statement(claim.statement)
        if key in seen:
            continue  # in-document duplicate
        seen.add(key)
        anchor_start, anchor_end = _clamp_anchor(claim, start_line, end_line)
        accepted.append(
            {
                "statement": claim.statement.strip(),
                "claim_type": claim.claim_type,
                "mentioned_paths": claim.mentioned_paths,
                "anchor": {
                    "path": doc_path,
                    "start_line": anchor_start,
                    "end_line": anchor_end,
                },
                "confidence": claim.confidence,
            }
        )
    return accepted


def extract_for_eval(
    text: str,
    doc_path: str | None,
    kind: str,
    start_line: int | None,
) -> list[dict[str, Any]]:
    """T2.1 eval-harness contract — same prompt, filters, and clamping as
    production, minus the database."""
    return _extract_raw(
        text=text, kind=kind, path=doc_path, doc_path=doc_path,
        start_line=start_line, end_line=None if start_line is None else 10**9,
    )


def llm_configured(settings: Settings | None = None) -> bool:
    """True when the configured completion provider AND embeddings can run.
    Embeddings always need OpenAI (see axon/llm/provider.py)."""
    settings = settings or get_settings()
    if not settings.openai_api_key:
        return False
    return settings.llm_provider != "anthropic" or bool(settings.anthropic_api_key)


# --- Service --------------------------------------------------------------


@dataclass
class ExtractionReport:
    entities_processed: int = 0
    entities_skipped_unchanged: int = 0
    entities_failed: int = 0
    claims_created: int = 0
    claims_updated: int = 0
    claims_deleted: int = 0
    duplicates_skipped: int = 0
    rejected_by_filters: int = 0
    duration_s: float = 0.0

    def summary(self) -> str:
        return (
            f"entities: processed={self.entities_processed} "
            f"skipped(unchanged)={self.entities_skipped_unchanged} "
            f"failed={self.entities_failed}\n"
            f"claims: created={self.claims_created} updated={self.claims_updated} "
            f"deleted={self.claims_deleted} duplicates_skipped={self.duplicates_skipped}\n"
            f"duration: {self.duration_s:.1f}s"
        )


class ClaimExtractionService:
    """Extracts and persists claims for one repository.

    Provider injection exists for tests; production uses the env-selected
    factories. ``batch_size`` controls entities per commit AND per embedding
    batch (settings.extraction_batch_size by default).
    """

    def __init__(
        self,
        db: Session,
        completion_provider: llm.CompletionProvider | None = None,
        embedding_provider: llm.EmbeddingProvider | None = None,
        batch_size: int | None = None,
    ) -> None:
        self.db = db
        self._completion = completion_provider
        self._embedding = embedding_provider
        self.batch_size = batch_size or get_settings().extraction_batch_size
        self.report = ExtractionReport()

    # -- public entry point ------------------------------------------------

    def run(self, repo: Repo) -> ExtractionReport:
        started = time.monotonic()
        entities = self.db.scalars(
            select(Entity)
            .where(Entity.repo_id == repo.id, Entity.kind.in_(CLAIM_BEARING_KINDS))
            .order_by(Entity.path.nulls_last(), Entity.external_id)
        ).all()

        repo_paths = list(
            self.db.scalars(
                select(Entity.path).where(
                    Entity.repo_id == repo.id,
                    Entity.kind.in_((EntityKind.CODE_FILE, EntityKind.DOC)),
                    Entity.path.is_not(None),
                )
            )
        )

        # Repo-wide statement index for cross-entity dedupe.
        existing_claims = self.db.scalars(
            select(Claim).where(Claim.repo_id == repo.id)
        ).all()
        by_statement: dict[str, Claim] = {
            normalize_statement(c.statement): c for c in existing_claims
        }

        for start in range(0, len(entities), self.batch_size):
            batch = entities[start : start + self.batch_size]
            to_embed: list[Claim] = []
            for entity in batch:
                try:
                    to_embed.extend(
                        self._process_entity(repo, entity, repo_paths, by_statement)
                    )
                except llm.LLMError:
                    self.report.entities_failed += 1
                    logger.exception(
                        "extraction failed for entity %s (%s) — continuing",
                        entity.id, entity.path or entity.external_id,
                    )
            self._embed_claims(to_embed)
            self.db.commit()

        self.report.duration_s = time.monotonic() - started
        logger.info("extraction for %s finished\n%s", repo.full_name, self.report.summary())
        return self.report

    # -- internals ---------------------------------------------------------

    def _source_text(self, entity: Entity) -> tuple[str, str | None, int | None, int | None]:
        """(text, doc_path, start_line, end_line) for one entity."""
        if entity.kind == EntityKind.DOC_SECTION:
            meta = entity.meta or {}
            return (
                meta.get("text", ""),
                meta.get("doc_path"),
                meta.get("start_line"),
                meta.get("end_line"),
            )
        meta = entity.meta or {}
        title = meta.get("title") or entity.name
        body = meta.get("body") or ""
        state = meta.get("state")
        state_note = f"\n\nState: {state}" if state else ""
        return f"Title: {title}\n\n{body}{state_note}", None, None, None

    def _process_entity(
        self,
        repo: Repo,
        entity: Entity,
        repo_paths: list[str],
        by_statement: dict[str, Claim],
    ) -> list[Claim]:
        """Extract for one entity; returns claims needing embeddings."""
        if (
            entity.content_hash is not None
            and (entity.meta or {}).get("claims_hash") == entity.content_hash
        ):
            self.report.entities_skipped_unchanged += 1
            return []

        text, doc_path, start_line, end_line = self._source_text(entity)
        kind = "doc_section" if entity.kind == EntityKind.DOC_SECTION else "issue"
        raw_count_before = self.report.rejected_by_filters
        extracted = _extract_raw(
            text=text,
            kind=kind,
            path=entity.path or f"{entity.kind.value} #{entity.external_id}",
            doc_path=doc_path,
            start_line=start_line,
            end_line=end_line,
            repo_paths=repo_paths,
            completion_provider=self._completion,
        ) if text.strip() else []
        del raw_count_before  # filter rejections are logged in _extract_raw

        existing_for_entity = {
            normalize_statement(c.statement): c
            for c in self.db.scalars(
                select(Claim).where(Claim.source_entity_id == entity.id)
            )
        }

        needs_embedding: list[Claim] = []
        new_keys: set[str] = set()
        for item in extracted:
            key = normalize_statement(item["statement"])
            new_keys.add(key)
            anchor = {
                **item["anchor"],
                "section": entity.path,
                "mentioned_paths": item["mentioned_paths"],
            }
            current = existing_for_entity.get(key)
            if current is not None:
                # same belief, same source — refresh anchor/type/confidence
                current.anchor = anchor
                current.claim_type = ClaimType(item["claim_type"])
                current.confidence = item["confidence"]
                if current.embedding is None:
                    needs_embedding.append(current)
                self.report.claims_updated += 1
                continue
            twin = by_statement.get(key)
            if twin is not None and twin.source_entity_id != entity.id:
                self.report.duplicates_skipped += 1
                continue
            claim = Claim(
                repo_id=repo.id,
                source_entity_id=entity.id,
                statement=item["statement"],
                claim_type=ClaimType(item["claim_type"]),
                anchor=anchor,
                status=ClaimStatus.UNCHECKED,
                confidence=item["confidence"],
            )
            self.db.add(claim)
            by_statement[key] = claim
            needs_embedding.append(claim)
            self.report.claims_created += 1

        # Beliefs this source no longer asserts are removed with it.
        for key, stale in existing_for_entity.items():
            if key not in new_keys:
                by_statement.pop(key, None)
                self.db.delete(stale)
                self.report.claims_deleted += 1

        entity.meta = {**(entity.meta or {}), "claims_hash": entity.content_hash}
        self.report.entities_processed += 1
        return needs_embedding

    def _embed_claims(self, claims: list[Claim]) -> None:
        if not claims:
            return
        vectors = llm.embed(
            [c.statement for c in claims], provider=self._embedding
        )
        for claim, vector in zip(claims, vectors):
            claim.embedding = vector
