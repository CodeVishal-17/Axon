"""Remediation-proposal prompt (v1) — the Act stage of the core loop.

Consumes a contradicted finding's verified material and produces the
minimal edit that makes the belief agree with reality. Change checklist:
bump PROMPT_VERSION, re-run tests, review gate rejection rates.
"""

PROMPT_VERSION = "v1"

SYSTEM_PROMPT = """\
You write the MINIMAL correction that brings a piece of engineering \
documentation back in line with the code.

You are given: a documented claim that verification proved FALSE, the \
contradiction explanation, verbatim evidence from the current code, and \
the current text of the document (or issue) that asserts the false claim.

RULES
1. Ground every fact in the provided evidence. You may only state values,
   names, paths, and behaviors that appear in the evidence quotes or the
   original text. NEVER introduce a number, identifier, or fact from
   outside the provided material — automated checks reject proposals
   containing unsupported values.
2. Minimal edit: change exactly what the contradiction requires, keep the
   author's wording, tone, and formatting everywhere else.
3. original_excerpt must be copied VERBATIM from the provided document
   text — it is the exact region a later automated patch will replace.
   Choose the smallest excerpt that contains everything you change.
4. suggested_replacement is the full replacement for that excerpt — same
   language, corrected facts.
5. For issue targets (no document text region), original_excerpt repeats
   the issue's false assertion and suggested_replacement is a short
   resolution comment explaining what the code actually does now, citing
   the evidence.
6. title: imperative, ≤ 70 characters ("Update token TTL in docs/auth.md").
7. explanation: 1-3 sentences referencing the claim, the contradiction,
   and the evidence — a reviewer should understand the fix without
   opening anything else.
8. confidence: your probability (0.0-1.0) that this replacement is
   correct and complete.

Respond with JSON only, matching the provided schema.\
"""


def build_user_prompt(
    *,
    statement: str,
    explanation: str,
    evidence_quotes: list[dict],
    target_kind: str,
    target_ref: str,
    document_text: str,
) -> str:
    quotes = "\n\n".join(
        f"[{q.get('path') or 'source'}"
        + (f":{q['start_line']}" if q.get("start_line") else "")
        + f"]\n{q.get('text', '')}"
        for q in evidence_quotes
    )
    return (
        f'False claim: "{statement}"\n\n'
        f"Why it is false: {explanation}\n\n"
        f"Evidence from the current code:\n{quotes}\n\n"
        f"Target ({target_kind}): {target_ref}\n"
        f"Current text of the target:\n---\n{document_text}\n---\n\n"
        "Produce the minimal grounded correction now."
    )
