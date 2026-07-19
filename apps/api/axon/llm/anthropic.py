"""Anthropic provider — structured completions via output_config.format.

The only module besides axon/llm/openai.py allowed to import an LLM SDK.
No embed() here: Anthropic offers no embeddings endpoint — the embedding
path always uses the OpenAI provider (see axon/llm/provider.py).
"""

from __future__ import annotations

from typing import Any

from anthropic import Anthropic

from axon.config import get_settings
from axon.llm.provider import LLMError

# Non-streaming ceiling that stays under SDK HTTP timeouts; our structured
# outputs (claims, verdicts) are far smaller than this.
_MAX_TOKENS = 16000


class AnthropicProvider:
    """Structured completions using the Messages API's native
    ``output_config.format`` (json_schema) — the first content block is
    guaranteed to be text containing schema-valid JSON.

    Thinking config is deliberately omitted: on Claude Sonnet 5 / Fable-era
    models, omitting the parameter runs adaptive thinking (the recommended
    mode), and explicit configs are rejected on some of them.
    """

    name = "anthropic"

    def __init__(self, client: Anthropic | None = None) -> None:
        settings = get_settings()
        # `client` injection exists for tests (no network, no key needed).
        self._client = client or Anthropic(api_key=settings.anthropic_api_key)
        self._model = settings.anthropic_model

    def complete_json(
        self,
        *,
        prompt: str,
        system: str | None,
        schema: dict[str, Any],
        schema_name: str,  # unused: Anthropic's format takes the bare schema
    ) -> str:
        kwargs: dict[str, Any] = {}
        if system:
            kwargs["system"] = system

        response = self._client.messages.create(
            model=self._model,
            max_tokens=_MAX_TOKENS,
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )

        # Always branch on stop_reason before reading content: refusals are
        # HTTP 200, and a max_tokens stop means truncated (invalid) JSON.
        if response.stop_reason == "refusal":
            raise LLMError("Anthropic refused the request (stop_reason=refusal)")
        if response.stop_reason == "max_tokens":
            raise LLMError(
                f"Anthropic output truncated at {_MAX_TOKENS} tokens (stop_reason=max_tokens)"
            )

        text = next(
            (block.text for block in response.content if block.type == "text"), None
        )
        if not text:
            raise LLMError(
                f"Anthropic returned no text block (stop_reason={response.stop_reason!r})"
            )
        return text
