"""Entity-linking LLM fallback prompt (v1).

Used ONLY for claims the deterministic tiers (path, symbol) and embedding
search could not resolve — by design the smallest slice of links.
"""

PROMPT_VERSION = "v1"

SYSTEM_PROMPT = """\
You link an engineering claim to the repository file that implements or \
evidences it.

You are given one claim and a shortlist of candidate file paths from the \
repository. Choose the single best candidate — the file a reviewer would \
open to verify the claim — or answer null when no candidate is clearly \
related.

Rules:
- Choose ONLY from the provided candidates, verbatim.
- No speculative links: if the connection is not obvious from the claim's \
subject matter, answer null. A missing link is recoverable; a wrong link \
poisons verification.
- confidence is your probability (0.0-1.0) that a reviewer would agree the \
chosen file is where this claim should be verified.

Respond with JSON only, matching the provided schema.\
"""


def build_user_prompt(statement: str, candidates: list[str], repo_name: str) -> str:
    listing = "\n".join(f"- {path}" for path in candidates)
    return (
        f"Repository: {repo_name}\n\n"
        f"Claim: {statement}\n\n"
        f"Candidate files:\n{listing}\n\n"
        "Pick the single best candidate path, or null if none clearly fits."
    )
