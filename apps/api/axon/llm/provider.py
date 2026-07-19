"""Provider-agnostic LLM layer — the ONLY entry point for model calls.

Public API:

* :func:`complete` — structured completion: prompt in, validated Pydantic
  model out. Schema validation is automatic; one retry (with the validation
  error fed back) on parse failure.
* :func:`embed` — batched embeddings, dimension-checked against
  ``EMBEDDING_DIM`` (the width of the ``claims.embedding`` column).

Design:

* Providers implement the tiny :class:`CompletionProvider` /
  :class:`EmbeddingProvider` protocols and return raw JSON text. Validation
  and retry live HERE, once — not per provider — so every provider gets the
  same reliability behavior and adding a provider is a single new module.
* The provider is selected entirely by env (``LLM_PROVIDER``); calling code
  never mentions a vendor.
* Embeddings always use OpenAI: Anthropic does not offer an embeddings
  endpoint, and ``EMBEDDING_DIM`` (1536) is pinned to
  ``text-embedding-3-small``. Running Anthropic completions therefore still
  requires ``OPENAI_API_KEY`` for the embedding path.

No module outside ``axon.llm`` may import an LLM SDK (enforced by test).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from axon.config import get_settings
from axon.db.models import EMBEDDING_DIM

logger = logging.getLogger("axon.llm")

T = TypeVar("T", bound=BaseModel)

# One retry on schema-validation failure (task requirement); more retries
# hide prompt problems that should be fixed at the prompt level instead.
MAX_ATTEMPTS = 2


class LLMError(RuntimeError):
    """Base error for LLM-layer failures (refusals, truncation, transport)."""


class SchemaValidationError(LLMError):
    """The model failed to produce schema-valid output after all attempts."""


class CompletionProvider(Protocol):
    """A vendor backend for structured completions.

    ``complete_json`` performs ONE model call and returns the raw JSON text.
    Implementations should use the vendor's native structured-output feature
    (OpenAI json_schema response_format; Anthropic output_config.format) so
    the wrapper's validation almost never needs its retry.
    """

    name: str

    def complete_json(
        self,
        *,
        prompt: str,
        system: str | None,
        schema: dict[str, Any],
        schema_name: str,
    ) -> str: ...


class EmbeddingProvider(Protocol):
    """A vendor backend for text embeddings."""

    def embed(self, texts: list[str]) -> list[list[float]]: ...


# --- Schema preparation --------------------------------------------------


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic model → JSON schema acceptable to strict structured-output
    modes on both providers.

    Both vendors' strict modes require ``additionalProperties: false`` and
    every property listed in ``required`` on every object (including nested
    ``$defs``). Consequence for schema authors: output models must declare
    all fields required — use explicit nullable fields (``str | None``)
    instead of defaults for optionality.
    """
    schema = model.model_json_schema()
    _make_strict(schema)
    return schema


def _make_strict(node: Any) -> None:
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            node["additionalProperties"] = False
            node["required"] = list(node["properties"].keys())
        for value in node.values():
            _make_strict(value)
    elif isinstance(node, list):
        for value in node:
            _make_strict(value)


# --- Provider factory ----------------------------------------------------


@lru_cache(maxsize=1)
def get_completion_provider() -> CompletionProvider:
    """Completion backend selected by ``LLM_PROVIDER`` (openai | anthropic).

    Imports are deferred so only the selected vendor's SDK is loaded.
    Adding a provider = one new module + one branch here; nothing else in
    the codebase changes.
    """
    settings = get_settings()
    if settings.llm_provider == "openai":
        from axon.llm.openai import OpenAIProvider

        return OpenAIProvider()
    if settings.llm_provider == "anthropic":
        from axon.llm.anthropic import AnthropicProvider

        return AnthropicProvider()
    raise LLMError(
        f"Unknown LLM_PROVIDER {settings.llm_provider!r} (expected 'openai' or 'anthropic')"
    )


@lru_cache(maxsize=1)
def get_embedding_provider() -> EmbeddingProvider:
    """Embeddings are always OpenAI (see module docstring)."""
    from axon.llm.openai import OpenAIProvider

    return OpenAIProvider()


# --- Public API ----------------------------------------------------------


def complete(
    prompt: str,
    schema: type[T],
    *,
    system: str | None = None,
    provider: CompletionProvider | None = None,
) -> T:
    """Run a structured completion and return a validated ``schema`` instance.

    Identical calling code works across providers — pass ``provider`` only
    in tests; production callers rely on the env-selected factory.

    Raises :class:`SchemaValidationError` if the model can't produce
    schema-valid output within ``MAX_ATTEMPTS``, or :class:`LLMError` for
    refusals/transport failures.
    """
    prov = provider or get_completion_provider()
    json_schema = strict_json_schema(schema)
    schema_name = schema.__name__

    attempt_prompt = prompt
    last_error: ValidationError | None = None
    last_raw = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        raw = prov.complete_json(
            prompt=attempt_prompt,
            system=system,
            schema=json_schema,
            schema_name=schema_name,
        )
        try:
            return schema.model_validate_json(raw)
        except ValidationError as exc:
            last_error, last_raw = exc, raw
            logger.warning(
                "structured output failed validation (provider=%s schema=%s attempt=%d/%d): %s",
                prov.name,
                schema_name,
                attempt,
                MAX_ATTEMPTS,
                exc,
            )
            # Feed the failure back — the model sees its own invalid output
            # and the validator's complaints on the retry.
            attempt_prompt = (
                f"{prompt}\n\n"
                f"Your previous response was rejected because it did not match "
                f"the required schema.\n"
                f"Previous response:\n{raw}\n\n"
                f"Validation errors:\n{exc}\n\n"
                f"Respond again with ONLY corrected JSON that satisfies the schema."
            )

    raise SchemaValidationError(
        f"{prov.name} failed to produce valid {schema_name} after "
        f"{MAX_ATTEMPTS} attempts. Last error: {last_error}. Last output: {last_raw[:500]}"
    )


def embed(
    texts: list[str],
    *,
    provider: EmbeddingProvider | None = None,
) -> list[list[float]]:
    """Embed ``texts`` → one ``EMBEDDING_DIM``-wide vector per input, in order.

    Dimension is asserted here because a silently wrong dimension would
    corrupt the pgvector column and every similarity search after it.
    """
    if not texts:
        return []
    prov = provider or get_embedding_provider()
    vectors = prov.embed(texts)

    if len(vectors) != len(texts):
        raise LLMError(f"expected {len(texts)} embeddings, got {len(vectors)}")
    for i, vector in enumerate(vectors):
        if len(vector) != EMBEDDING_DIM:
            raise LLMError(
                f"embedding {i} has dimension {len(vector)}, expected {EMBEDDING_DIM}"
            )
    return vectors
