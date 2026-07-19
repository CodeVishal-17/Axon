"""OpenAI provider — completions (structured outputs) and embeddings.

The only module besides axon/llm/anthropic.py allowed to import an LLM SDK.
"""

from __future__ import annotations

from typing import Any

from openai import OpenAI

from axon.config import get_settings
from axon.llm.provider import LLMError

# Embedding API caps batch size; chunking also keeps request payloads sane.
_EMBED_BATCH_SIZE = 512


class OpenAIProvider:
    """Structured completions via json_schema response_format (strict mode)
    and embeddings via the embeddings endpoint."""

    name = "openai"

    def __init__(self, client: OpenAI | None = None) -> None:
        settings = get_settings()
        # `client` injection exists for tests (no network, no key needed).
        self._client = client or OpenAI(api_key=settings.openai_api_key)
        self._model = settings.openai_model
        self._embedding_model = settings.embedding_model

    def complete_json(
        self,
        *,
        prompt: str,
        system: str | None,
        schema: dict[str, Any],
        schema_name: str,
    ) -> str:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        response = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema, "strict": True},
            },
        )
        choice = response.choices[0]
        content = choice.message.content
        if not content:
            raise LLMError(
                f"OpenAI returned no content (finish_reason={choice.finish_reason!r})"
            )
        return content

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _EMBED_BATCH_SIZE):
            batch = texts[start : start + _EMBED_BATCH_SIZE]
            response = self._client.embeddings.create(
                model=self._embedding_model, input=batch
            )
            # The API documents index-ordered results; sort defensively so a
            # reordering can never silently misalign vectors with texts.
            ordered = sorted(response.data, key=lambda item: item.index)
            vectors.extend(item.embedding for item in ordered)
        return vectors
