"""T1.1 verification — runs fully offline (no API keys, no network).

Covers: golden structured-output path, schema validation catching malformed
responses, the single retry, identical calling code across both real
provider classes (SDK clients stubbed), embedding smoke + dimension guard,
and the "no LLM SDK imports outside axon.llm" architecture rule.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from axon.db.models import EMBEDDING_DIM
from axon.llm import provider as llm
from axon.llm.anthropic import AnthropicProvider
from axon.llm.openai import OpenAIProvider


class WeatherReport(BaseModel):
    city: str
    temperature_c: float
    sunny: bool


VALID_JSON = json.dumps({"city": "Paris", "temperature_c": 21.5, "sunny": True})
MALFORMED_JSON = json.dumps({"city": "Paris", "temperature_c": "warm-ish"})


class FakeProvider:
    """Scriptable CompletionProvider: returns canned responses in order."""

    name = "fake"

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    def complete_json(
        self, *, prompt: str, system: str | None, schema: dict, schema_name: str
    ) -> str:
        self.prompts.append(prompt)
        return self._responses.pop(0)


# --- Golden path ---------------------------------------------------------


def test_golden_structured_output() -> None:
    fake = FakeProvider([VALID_JSON])
    result = llm.complete("What's the weather in Paris?", WeatherReport, provider=fake)

    assert isinstance(result, WeatherReport)
    assert result.city == "Paris"
    assert result.temperature_c == 21.5
    assert result.sunny is True
    assert len(fake.prompts) == 1


# --- Validation + retry --------------------------------------------------


def test_retry_recovers_from_malformed_response() -> None:
    fake = FakeProvider([MALFORMED_JSON, VALID_JSON])
    result = llm.complete("Weather?", WeatherReport, provider=fake)

    assert result.city == "Paris"
    assert len(fake.prompts) == 2
    # The retry prompt must feed the failure back to the model.
    assert MALFORMED_JSON in fake.prompts[1]
    assert "rejected" in fake.prompts[1]


def test_validation_failure_after_retry_raises() -> None:
    fake = FakeProvider([MALFORMED_JSON, "not even json"])
    with pytest.raises(llm.SchemaValidationError):
        llm.complete("Weather?", WeatherReport, provider=fake)
    assert len(fake.prompts) == 2  # exactly one retry, then give up


def test_strict_schema_marks_all_objects() -> None:
    class Nested(BaseModel):
        inner: WeatherReport
        label: str

    schema = llm.strict_json_schema(Nested)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"inner", "label"}
    inner = schema["$defs"]["WeatherReport"]
    assert inner["additionalProperties"] is False
    assert set(inner["required"]) == {"city", "temperature_c", "sunny"}


# --- Identical calling code across both real providers -------------------


def _openai_client_stub(payload: str) -> Any:
    """Minimal object mimicking the OpenAI SDK response shape."""
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(content=payload)
            )
        ]
    )
    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kwargs: response)
        )
    )


def _anthropic_client_stub(payload: str, stop_reason: str = "end_turn") -> Any:
    """Minimal object mimicking the Anthropic SDK response shape."""
    response = SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=payload)],
    )
    return SimpleNamespace(
        messages=SimpleNamespace(create=lambda **kwargs: response)
    )


@pytest.mark.parametrize(
    "provider_factory",
    [
        lambda: OpenAIProvider(client=_openai_client_stub(VALID_JSON)),
        lambda: AnthropicProvider(client=_anthropic_client_stub(VALID_JSON)),
    ],
    ids=["openai", "anthropic"],
)
def test_identical_calling_code_across_providers(provider_factory) -> None:
    # This exact call is what services will write — no provider-specific code.
    result = llm.complete(
        "Weather in Paris?",
        WeatherReport,
        system="You are a weather extractor.",
        provider=provider_factory(),
    )
    assert result == WeatherReport(city="Paris", temperature_c=21.5, sunny=True)


def test_anthropic_refusal_raises_llm_error() -> None:
    provider = AnthropicProvider(
        client=_anthropic_client_stub("", stop_reason="refusal")
    )
    with pytest.raises(llm.LLMError, match="refus"):
        llm.complete("Weather?", WeatherReport, provider=provider)


# --- Embeddings ----------------------------------------------------------


def _embeddings_client_stub(dim: int) -> Any:
    def create(*, model: str, input: list[str]) -> Any:  # noqa: A002
        return SimpleNamespace(
            data=[
                SimpleNamespace(index=i, embedding=[0.1] * dim)
                for i in range(len(input))
            ]
        )

    return SimpleNamespace(embeddings=SimpleNamespace(create=create))


def test_embedding_smoke() -> None:
    provider = OpenAIProvider(client=_embeddings_client_stub(EMBEDDING_DIM))
    vectors = llm.embed(["hello", "world", "axon"], provider=provider)

    assert len(vectors) == 3
    assert all(len(v) == EMBEDDING_DIM for v in vectors)


def test_embedding_dimension_mismatch_raises() -> None:
    provider = OpenAIProvider(client=_embeddings_client_stub(EMBEDDING_DIM - 1))
    with pytest.raises(llm.LLMError, match="dimension"):
        llm.embed(["hello"], provider=provider)


def test_embed_empty_input_is_noop() -> None:
    assert llm.embed([], provider=None) == []


# --- Architecture rule: SDK imports live ONLY in axon.llm ----------------


def test_no_llm_sdk_imports_outside_llm_package() -> None:
    package_root = Path(llm.__file__).resolve().parents[1]  # axon/
    llm_dir = package_root / "llm"
    offenders = []
    for path in package_root.rglob("*.py"):
        if llm_dir in path.parents:
            continue
        text = path.read_text(encoding="utf-8")
        for needle in ("import openai", "from openai", "import anthropic", "from anthropic"):
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert not offenders, f"LLM SDK imported outside axon.llm: {offenders}"
