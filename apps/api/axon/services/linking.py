"""Entity linker: claims → the code entities that evidence them.

Populates ``claim_links`` via a strict precision-first tier order
(architecture §7 — cheap and deterministic before expensive and fuzzy):

    1. PATH       claim.anchor.mentioned_paths vs the repo path inventory
    2. SYMBOL     identifiers mined from the statement vs basenames/stems
    3. EMBEDDING  claim embedding vs cached entity path-text embeddings
    4. LLM        candidate shortlist → best-or-null (smallest slice)

EXPLAINABILITY CONTRACT: every tier emits (entity, confidence, reason) from
a deterministic rule. The DB stores method + strength (existing columns);
:func:`explain_link` regenerates the human answer to "why was this claim
linked to this entity?" for any persisted link. No link is ever created
without a rule that can restate itself in a sentence.

Precision rules: ambiguous path/symbol matches (too many targets) produce
NO link; embedding matches below the configured threshold produce NO link;
the LLM may only pick from the shortlist and may answer null. No link is
better than a wrong link.

Incremental: each claim's link state is fingerprinted (statement +
mentioned_paths + inventory hash + algorithm version) into
``claim.anchor["link_state"]``. Unchanged fingerprint → the claim is not
recomputed (including LLM-no-match outcomes, so fallback spend is never
repeated). A changed statement produces a new claim row (T2.2), and a
changed file inventory changes the fingerprint — both trigger relinking.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from axon.config import get_settings
from axon.db.models import Claim, ClaimLink, Entity, EntityKind, LinkMethod, Repo
from axon.llm import provider as llm
from axon.llm.prompts.entity_linking import SYSTEM_PROMPT, build_user_prompt

logger = logging.getLogger("axon.services.linking")

ALGO_VERSION = "v1"

# Confidence table — one place, so every link's number is auditable.
CONF_PATH_EXACT = 0.95
CONF_PATH_SUFFIX = 0.90
CONF_PATH_BASENAME = 0.80
CONF_SYMBOL_BASENAME = 0.85
CONF_SYMBOL_STEM = 0.75
CONF_SYMBOL_STEM_PREFIX = 0.70
CONF_SYMBOL_SEGMENT = 0.65
# Prefix matching below this identifier length is noise, not evidence.
MIN_PREFIX_LEN = 6
LLM_CONFIDENCE_CAP = 0.70
# A token matching more than this many files is ambiguous → no link.
MAX_SYMBOL_TARGETS = 3

LINK_TARGET_KINDS = (EntityKind.CODE_FILE,)

_IDENTIFIER_RE = re.compile(
    r"`([^`]+)`"                                   # backtick spans
    r"|(\b[\w./-]+\.[A-Za-z]{1,4}\b)"              # filename-ish tokens
    r"|(\b[a-z]+(?:_[a-z0-9]+)+\b)"                # snake_case
    r"|(\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b)"    # CamelCase
    r"|((?<=\s)/[\w/-]{2,}\b)"                     # /route strings
)

_STOP_TOKENS = {
    "docker compose", "docker-compose", "e.g", "i.e", "http", "https",
}


# --- Pure matching core (used by the service AND the DB-free eval) --------


@dataclass(frozen=True)
class Match:
    path: str
    confidence: float
    reason: str


class PathIndex:
    """Lookup structures over the repo's linkable file paths."""

    def __init__(self, paths: list[str]) -> None:
        self.paths = sorted(set(paths))
        self.exact = set(self.paths)
        self.by_basename: dict[str, list[str]] = {}
        self.by_stem: dict[str, list[str]] = {}
        self.by_segment: dict[str, list[str]] = {}
        for path in self.paths:
            basename = path.rsplit("/", 1)[-1].lower()
            # '-' and '_' are interchangeable across naming conventions
            # (finding-card.tsx ↔ FindingCard) — normalize stems to '_'.
            stem = basename.rsplit(".", 1)[0].replace("-", "_")
            self.by_basename.setdefault(basename, []).append(path)
            self.by_stem.setdefault(stem, []).append(path)
            for segment in path.lower().split("/")[:-1]:
                self.by_segment.setdefault(segment, []).append(path)
        self.inventory_hash = hashlib.sha256(
            "\n".join(self.paths).encode()
        ).hexdigest()[:16]


def link_by_path(mentioned_paths: list[str], index: PathIndex) -> list[Match]:
    """Tier 1 — PATH. Exact > unique suffix > unique basename; ambiguity
    yields nothing."""
    matches: list[Match] = []
    for raw in mentioned_paths:
        mention = raw.strip().lstrip("./")
        if not mention:
            continue
        if mention in index.exact:
            matches.append(
                Match(mention, CONF_PATH_EXACT,
                      f"mentioned_paths contains '{raw}' — exact repository path")
            )
            continue
        suffix_hits = [p for p in index.paths if p.endswith("/" + mention)]
        if len(suffix_hits) == 1:
            matches.append(
                Match(suffix_hits[0], CONF_PATH_SUFFIX,
                      f"mentioned_paths contains '{raw}' — unique path suffix of "
                      f"'{suffix_hits[0]}'")
            )
            continue
        basename_hits = index.by_basename.get(mention.lower(), [])
        if len(basename_hits) == 1:
            matches.append(
                Match(basename_hits[0], CONF_PATH_BASENAME,
                      f"mentioned_paths contains '{raw}' — unique basename match")
            )
    return matches


def extract_symbols(statement: str) -> list[str]:
    tokens: list[str] = []
    for groups in _IDENTIFIER_RE.findall(statement):
        token = next(g for g in groups if g).strip()
        lowered = token.lower().strip("/")
        if lowered in _STOP_TOKENS or len(lowered) < 3:
            continue
        tokens.append(token)
    # de-dupe preserving order
    seen: set[str] = set()
    return [t for t in tokens if not (t.lower() in seen or seen.add(t.lower()))]


def _snake(token: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", token).lower()


def link_by_symbol(statement: str, index: PathIndex) -> list[Match]:
    """Tier 2 — SYMBOL. Filename token > stem identity > path segment.
    Tokens hitting more than MAX_SYMBOL_TARGETS files are ambiguous."""
    matches: list[Match] = []
    for token in extract_symbols(statement):
        lowered = token.lower().strip("/")
        candidates: list[tuple[str, float, str]] = []

        if "." in lowered:  # filename-like: match basename (path-ish handled by tier 1)
            basename = lowered.rsplit("/", 1)[-1]
            for path in index.by_basename.get(basename, []):
                candidates.append(
                    (path, CONF_SYMBOL_BASENAME,
                     f"statement names file '{token}' — basename of '{path}'")
                )
        else:
            # snake-case the ORIGINAL token (lowercasing first would erase
            # CamelCase word boundaries: RepoHeader → repo_header).
            stem = _snake(token).replace("-", "_")
            for path in index.by_stem.get(stem, []):
                candidates.append(
                    (path, CONF_SYMBOL_STEM,
                     f"identifier '{token}' equals file stem of '{path}'")
                )
            if not candidates and len(stem) >= MIN_PREFIX_LEN:
                # ClassName → module convention: IngestionService lives in
                # ingestion.py — stem is a prefix of the snaked identifier.
                # When several stems prefix-match, only the LONGEST (most
                # specific) wins: 'ingestion' beats 'ingest'.
                prefix_hits: list[tuple[str, str]] = [
                    (indexed_stem, path)
                    for indexed_stem, paths in index.by_stem.items()
                    if len(indexed_stem) >= MIN_PREFIX_LEN
                    and (stem.startswith(indexed_stem) or indexed_stem.startswith(stem))
                    for path in paths
                ]
                if prefix_hits:
                    longest = max(len(s) for s, _ in prefix_hits)
                    for indexed_stem, path in prefix_hits:
                        if len(indexed_stem) == longest:
                            candidates.append(
                                (path, CONF_SYMBOL_STEM_PREFIX,
                                 f"identifier '{token}' shares its prefix with "
                                 f"file stem '{indexed_stem}' of '{path}' "
                                 f"(longest stem match)")
                            )
            if not candidates:
                for path in index.by_segment.get(stem, []):
                    candidates.append(
                        (path, CONF_SYMBOL_SEGMENT,
                         f"identifier '{token}' is a directory segment of '{path}'")
                    )

        if 0 < len(candidates) <= MAX_SYMBOL_TARGETS:
            matches.extend(Match(*c) for c in candidates)
        elif len(candidates) > MAX_SYMBOL_TARGETS:
            logger.debug(
                "symbol %r ambiguous (%d targets) — skipped", token, len(candidates)
            )
    return matches


# --- LLM fallback schema --------------------------------------------------


class LinkChoice(BaseModel):
    entity_path: str | None
    confidence: float = Field(ge=0.0, le=1.0)


# --- Reports --------------------------------------------------------------


@dataclass
class LinkReport:
    claims_total: int = 0
    claims_skipped_unchanged: int = 0
    claims_linked: int = 0
    claims_unresolved: int = 0
    links_created: int = 0
    links_replaced: int = 0
    claims_by_method: dict[str, int] = field(default_factory=dict)
    by_method: dict[str, int] = field(default_factory=dict)
    confidence_sum: dict[str, float] = field(default_factory=dict)
    llm_calls: int = 0
    embedding_calls: int = 0
    unresolved_statements: list[str] = field(default_factory=list)
    duration_s: float = 0.0

    def record(self, method: str, confidence: float) -> None:
        self.by_method[method] = self.by_method.get(method, 0) + 1
        self.confidence_sum[method] = self.confidence_sum.get(method, 0.0) + confidence

    def avg_confidence(self) -> dict[str, float]:
        return {
            m: round(self.confidence_sum[m] / n, 3)
            for m, n in self.by_method.items()
        }

    def pct_without_llm(self) -> float:
        """Share of PROCESSED claims linked by the non-LLM tiers."""
        processed = self.claims_total - self.claims_skipped_unchanged
        non_llm = sum(n for m, n in self.claims_by_method.items() if m != "llm")
        return non_llm / processed if processed else 0.0

    def summary(self) -> str:
        methods = ", ".join(
            f"{m}={n} (avg conf {self.avg_confidence()[m]:.2f})"
            for m, n in sorted(self.by_method.items())
        ) or "none"
        return (
            f"claims: total={self.claims_total} linked={self.claims_linked} "
            f"unresolved={self.claims_unresolved} skipped(unchanged)={self.claims_skipped_unchanged}\n"
            f"links: created={self.links_created} replaced={self.links_replaced}\n"
            f"by method: {methods}\n"
            f"linked without LLM: {self.pct_without_llm():.0%} of all claims\n"
            f"llm calls: {self.llm_calls}   embedding calls: {self.embedding_calls}\n"
            f"duration: {self.duration_s:.1f}s"
        )


# --- The service ----------------------------------------------------------


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


class EntityLinker:
    """Links one repository's claims to code/doc entities.

    Providers are injectable for tests; when None AND no API key is
    configured, tiers 3–4 are skipped gracefully (tiers 1–2 are free and
    always run).
    """

    def __init__(
        self,
        db: Session,
        embedding_provider: llm.EmbeddingProvider | None = None,
        completion_provider: llm.CompletionProvider | None = None,
        similarity_threshold: float | None = None,
        top_k: int | None = None,
        max_links_per_claim: int | None = None,
    ) -> None:
        settings = get_settings()
        self.db = db
        self._embedding = embedding_provider
        self._completion = completion_provider
        self.similarity_threshold = (
            similarity_threshold
            if similarity_threshold is not None
            else settings.linker_similarity_threshold
        )
        self.top_k = top_k or settings.linker_top_k
        self.max_links = max_links_per_claim or settings.linker_max_links_per_claim
        self.report = LinkReport()

    # -- helpers -----------------------------------------------------------

    def _providers_available(self) -> tuple[bool, bool]:
        from axon.services.claims import llm_configured  # noqa: PLC0415

        keyed = llm_configured()
        return (
            self._embedding is not None or keyed,
            self._completion is not None or keyed,
        )

    def _fingerprint(self, claim: Claim, inventory_hash: str) -> str:
        mentioned = ",".join(sorted((claim.anchor or {}).get("mentioned_paths", [])))
        blob = f"{ALGO_VERSION}|{claim.statement}|{mentioned}|{inventory_hash}"
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def _entity_vector(self, entity: Entity) -> list[float] | None:
        """Path-text embedding, cached in entity.meta (entities have no
        vector column; schema is final)."""
        text = entity.path or entity.name
        key = hashlib.sha256(text.encode()).hexdigest()[:12]
        cached = (entity.meta or {}).get("link_emb")
        if cached and cached.get("h") == key:
            return cached["v"]
        if self._embed_texts is None:
            return None
        vector = self._embed_texts([text])[0]
        entity.meta = {**(entity.meta or {}), "link_emb": {"h": key, "v": vector}}
        return vector

    # -- main --------------------------------------------------------------

    def run(self, repo: Repo) -> LinkReport:
        started = time.monotonic()
        embeddings_ok, completion_ok = self._providers_available()
        self._embed_texts = (
            (lambda texts: llm.embed(texts, provider=self._embedding))
            if embeddings_ok
            else None
        )

        targets = self.db.scalars(
            select(Entity).where(
                Entity.repo_id == repo.id,
                Entity.kind.in_(LINK_TARGET_KINDS),
                Entity.path.is_not(None),
            )
        ).all()
        by_path: dict[str, Entity] = {e.path: e for e in targets}
        index = PathIndex(list(by_path.keys()))

        claims = self.db.scalars(
            select(Claim).where(Claim.repo_id == repo.id)
        ).all()
        self.report.claims_total = len(claims)

        for claim in claims:
            fingerprint = self._fingerprint(claim, index.inventory_hash)
            state = (claim.anchor or {}).get("link_state")
            if state and state.get("fp") == fingerprint:
                self.report.claims_skipped_unchanged += 1
                continue
            self._link_claim(claim, index, by_path, completion_ok, fingerprint)

        self.report.duration_s = time.monotonic() - started
        self.db.commit()
        logger.info("linking for %s finished\n%s", repo.full_name, self.report.summary())
        return self.report

    def _link_claim(
        self,
        claim: Claim,
        index: PathIndex,
        by_path: dict[str, Entity],
        completion_ok: bool,
        fingerprint: str,
    ) -> None:
        mentioned = (claim.anchor or {}).get("mentioned_paths", [])

        matches = link_by_path(mentioned, index)
        method = LinkMethod.PATH_MATCH
        if not matches:
            matches = link_by_symbol(claim.statement, index)
            method = LinkMethod.SYMBOL_MATCH
        if not matches:
            matches = self._embedding_tier(claim, index, by_path)
            method = LinkMethod.EMBEDDING
        if not matches and completion_ok:
            matches = self._llm_tier(claim, index, by_path)
            method = LinkMethod.LLM

        # best-per-entity, capped, deterministic order
        best: dict[str, Match] = {}
        for match in matches:
            if match.path not in best or match.confidence > best[match.path].confidence:
                best[match.path] = match
        chosen = sorted(best.values(), key=lambda m: (-m.confidence, m.path))[
            : self.max_links
        ]

        self._replace_links(claim, chosen, method, by_path)
        claim.anchor = {
            **(claim.anchor or {}),
            "link_state": {"fp": fingerprint, "n": len(chosen)},
        }
        if chosen:
            self.report.claims_linked += 1
            self.report.claims_by_method[method.value] = (
                self.report.claims_by_method.get(method.value, 0) + 1
            )
            for match in chosen:
                self.report.record(method.value, match.confidence)
        else:
            self.report.claims_unresolved += 1
            self.report.unresolved_statements.append(claim.statement)

    def _embedding_tier(
        self, claim: Claim, index: PathIndex, by_path: dict[str, Entity]
    ) -> list[Match]:
        if self._embed_texts is None or claim.embedding is None:
            return []
        claim_vector = list(claim.embedding)
        scored: list[tuple[float, str]] = []
        for path in index.paths:
            vector = self._entity_vector(by_path[path])
            if vector is None:
                continue
            scored.append((_cosine(claim_vector, vector), path))
        self.report.embedding_calls += 1
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [
            Match(path, round(score, 3),
                  f"embedding similarity {score:.2f} between claim and path text "
                  f"(threshold {self.similarity_threshold})")
            for score, path in scored[: self.top_k]
            if score >= self.similarity_threshold
        ]

    def _llm_tier(
        self, claim: Claim, index: PathIndex, by_path: dict[str, Entity]
    ) -> list[Match]:
        candidates = self._llm_candidates(claim, index, by_path)
        if not candidates:
            return []
        self.report.llm_calls += 1
        try:
            choice = llm.complete(
                build_user_prompt(claim.statement, candidates, repo_name=""),
                LinkChoice,
                system=SYSTEM_PROMPT,
                provider=self._completion,
            )
        except llm.LLMError:
            logger.exception("LLM link fallback failed for claim %s", claim.id)
            return []
        if choice.entity_path is None or choice.entity_path not in candidates:
            return []  # null answers and hallucinated paths create no link
        return [
            Match(choice.entity_path, min(choice.confidence, LLM_CONFIDENCE_CAP),
                  "LLM fallback chose this file from the candidate shortlist "
                  f"(model confidence {choice.confidence:.2f}, capped at "
                  f"{LLM_CONFIDENCE_CAP})")
        ]

    def _llm_candidates(
        self, claim: Claim, index: PathIndex, by_path: dict[str, Entity]
    ) -> list[str]:
        """Shortlist for the fallback: embedding-nearest when available,
        else nothing (a blind shortlist would invite speculative links)."""
        if self._embed_texts is None or claim.embedding is None:
            return []
        claim_vector = list(claim.embedding)
        scored = []
        for path in index.paths:
            vector = self._entity_vector(by_path[path])
            if vector is not None:
                scored.append((_cosine(claim_vector, vector), path))
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [path for _, path in scored[:8]]

    def _replace_links(
        self,
        claim: Claim,
        matches: list[Match],
        method: LinkMethod,
        by_path: dict[str, Entity],
    ) -> None:
        existing = {
            link.entity_id: link
            for link in self.db.scalars(
                select(ClaimLink).where(ClaimLink.claim_id == claim.id)
            )
        }
        kept: set[Any] = set()
        for match in matches:
            entity = by_path[match.path]
            self.db.flush()  # entity/claim ids must exist
            link = existing.get(entity.id)
            if link is None:
                self.db.add(
                    ClaimLink(
                        claim_id=claim.id, entity_id=entity.id,
                        strength=match.confidence, method=method,
                    )
                )
                self.report.links_created += 1
            else:
                if link.method != method or link.strength != match.confidence:
                    link.method = method
                    link.strength = match.confidence
                    self.report.links_replaced += 1
            kept.add(entity.id)
        for entity_id, link in existing.items():
            if entity_id not in kept:
                self.db.delete(link)
                self.report.links_replaced += 1


# --- Explainability -------------------------------------------------------


def explain_link(db: Session, link: ClaimLink) -> str:
    """Answer 'why was this claim linked to this entity?' for any persisted
    link, by re-deriving the deterministic rule for its method."""
    claim = db.get(Claim, link.claim_id)
    entity = db.get(Entity, link.entity_id)
    where = f"'{claim.statement[:80]}' → {entity.path}"
    if link.method == LinkMethod.PATH_MATCH:
        mentioned = (claim.anchor or {}).get("mentioned_paths", [])
        return (f"PATH ({link.strength:.2f}): {where} — the claim's "
                f"mentioned_paths {mentioned} resolve to this repository path.")
    if link.method == LinkMethod.SYMBOL_MATCH:
        symbols = extract_symbols(claim.statement)
        return (f"SYMBOL ({link.strength:.2f}): {where} — identifier(s) "
                f"{symbols} in the statement match this file's name/stem/segment.")
    if link.method == LinkMethod.EMBEDDING:
        return (f"EMBEDDING ({link.strength:.2f}): {where} — cosine similarity "
                f"{link.strength:.2f} between the claim's embedding and the "
                f"file-path text embedding (no path/symbol evidence existed).")
    return (f"LLM ({link.strength:.2f}): {where} — deterministic tiers found "
            f"nothing; the fallback model chose this file from an "
            f"embedding-ranked shortlist.")
