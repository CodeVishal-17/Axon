"""Live golden-call check for the LLM provider layer (T1.1 acceptance).

Runs the SAME structured-output call against every provider whose API key is
configured, proving that identical calling code works across providers.
Providers without keys are skipped, not failed — this script needs real
credentials and is meant for manual/demo-prep runs, not CI.

Usage (from apps/api/):
    python scripts/llm_golden_call.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import BaseModel  # noqa: E402

from axon.config import get_settings  # noqa: E402
from axon.llm import provider as llm  # noqa: E402


class ClaimExtraction(BaseModel):
    """Toy schema shaped like the real claim-extraction output."""

    statement: str
    claim_type: str
    mentioned_paths: list[str]
    confidence: float


PROMPT = (
    "Extract the single engineering claim from this documentation snippet.\n\n"
    'Snippet (from docs/auth.md): "Access tokens issued by src/auth/token.ts '
    'expire after 24 hours."\n\n'
    "claim_type must be one of: behavior, architecture, process, status."
)


def run_provider(name: str, prov: llm.CompletionProvider) -> bool:
    print(f"\n--- {name} ---")
    result = llm.complete(PROMPT, ClaimExtraction, provider=prov)
    print(f"  statement:  {result.statement}")
    print(f"  type:       {result.claim_type}")
    print(f"  paths:      {result.mentioned_paths}")
    print(f"  confidence: {result.confidence}")
    assert "24" in result.statement, "statement should mention the 24-hour expiry"
    print("  OK")
    return True


def main() -> None:
    settings = get_settings()
    ran = []

    if settings.openai_api_key:
        from axon.llm.openai import OpenAIProvider

        run_provider("openai", OpenAIProvider())
        ran.append("openai")

        print("\n--- openai embeddings ---")
        vectors = llm.embed(["hello", "world"])
        print(f"  {len(vectors)} vectors of dim {len(vectors[0])}")
        ran.append("embeddings")
    else:
        print("skipping openai (+embeddings): OPENAI_API_KEY not set")

    if settings.anthropic_api_key:
        from axon.llm.anthropic import AnthropicProvider

        run_provider("anthropic", AnthropicProvider())
        ran.append("anthropic")
    else:
        print("skipping anthropic: ANTHROPIC_API_KEY not set")

    if not ran:
        print("\nNo API keys configured — nothing verified. Set keys in .env.")
        sys.exit(1)
    print(f"\nGolden call passed for: {', '.join(ran)}")


if __name__ == "__main__":
    main()
