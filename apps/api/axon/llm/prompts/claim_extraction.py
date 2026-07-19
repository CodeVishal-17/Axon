"""Claim extraction prompt (v1) — the belief-mining stage of the loop.

Tuned against the T2.1 eval fixtures (make eval-claims). Change checklist:
bump PROMPT_VERSION, run the eval, compare reports before committing.
"""

PROMPT_VERSION = "v1"

SYSTEM_PROMPT = """\
You extract ATOMIC, OBJECTIVELY VERIFIABLE engineering claims from a \
software company's documentation and issue tracker.

A claim is a statement about the CURRENT system that a reviewer could prove \
true or false by reading the repository's code and configuration. Claims \
become long-lived "beliefs" that are later re-verified against the code, so \
precision matters far more than recall: one excellent claim beats five weak \
ones, and an EMPTY list is the correct output for text that asserts nothing.

CLAIM TYPES
- behavior:      what the system does at runtime ("The API serves at http://localhost:8000.")
- architecture:  how the system is structured ("The FastAPI backend lives in apps/api.")
- process:       how humans operate it ("make migrate applies database migrations.")
- status:        the current state of work or environment ("Unauthenticated GitHub API requests are limited to 60 per hour.")

EVERY CLAIM MUST BE
1. Atomic — exactly one independent fact. Split compound sentences.
2. Verifiable — provable from code/config. No opinions, no quality judgments.
3. Self-contained — readable alone: name the subject explicitly, resolve all
   pronouns, and KEEP concrete identifiers verbatim (paths, ports, URLs,
   env var names, commands, numbers). A claim that loses its number or path
   is not atomic.
4. Stable — present tense, active voice, semantic fact; never describe the
   document itself ("This section explains...") or its formatting.
5. Anchored — report the line numbers (from the numbered source provided)
   of the text that asserts the fact.

DO NOT EXTRACT
- marketing, vision, or aspirational language
- opinions and quality judgments ("fast", "secure", "easy")
- future plans, roadmaps, feature requests, TODOs ("will", "planned", "coming soon")
- instructions or advice TO THE READER ("run X to get started", "read the
  guide", "check out the docs") — UNLESS the instruction reveals a fact
  about the system: "run `make test` to run the backend tests" implies the
  extractable fact "make test runs the backend tests."
- code samples and example values (extract facts a sample demonstrates only
  when stated as fact by surrounding prose or by the artifact itself, e.g.
  a served URL listed next to a command)
- badges, licenses, copyright, changelogs, link lists, navigation, greetings
- questions (but DO extract facts stated in answers)
- anything you are not confident is asserted by the text — when in doubt,
  leave it out

FIELD RULES
- statement: one sentence, ≤ 30 words, present tense.
- mentioned_paths: repo-relative file or directory paths the claim is about
  or explicitly names; [] when none.
- start_line/end_line: absolute line numbers of the asserting text; null
  only when the source has no line numbering (issues).
- confidence: your probability (0.0–1.0) that this is a faithful, atomic,
  verifiable restatement of what the text asserts.

Respond with JSON only, matching the provided schema.\
"""


def build_user_prompt(
    *,
    text: str,
    kind: str,
    path: str | None,
    doc_path: str | None,
    start_line: int | None,
    repo_paths: list[str] | None = None,
) -> str:
    """Render one source document for extraction.

    Doc sections get absolute line numbers so the model can anchor claims;
    issues have no line numbering (anchors null). ``repo_paths`` (optional)
    helps the model normalize mentioned_paths to real repo paths.
    """
    parts: list[str] = []
    if kind == "doc_section":
        parts.append(f"Source: section `{path}` of `{doc_path}` (documentation)")
        first = start_line or 1
        numbered = "\n".join(
            f"{first + i:>5}| {line}" for i, line in enumerate(text.splitlines())
        )
        parts.append("Numbered source text:\n" + numbered)
    else:
        parts.append(f"Source: {path or 'issue'} (issue tracker — no line numbers; "
                     "use null for start_line/end_line)")
        parts.append("Source text:\n" + text)

    if repo_paths:
        listing = "\n".join(sorted(repo_paths)[:200])
        parts.append(
            "Known repository paths (for normalizing mentioned_paths):\n" + listing
        )

    parts.append(
        "Extract the claims now. Remember: precision over recall; an empty "
        "list is a valid answer."
    )
    return "\n\n".join(parts)
