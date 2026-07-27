"""Pull-request review prompt (v1) — Axon reviewing work humans already opened.

Two lenses, deliberately separate:

  code   — ordinary review: correctness, edge cases, risk.
  truth  — Axon's differentiator: does this change contradict or stale a
           claim the knowledge graph has verified? The claims supplied in the
           prompt are the ones LINKED to the files this PR touches, so the
           model reasons about real documented beliefs, not guesses.

Change checklist: bump PROMPT_VERSION, re-run tests, review gate drop rates.
"""

PROMPT_VERSION = "v1"

SYSTEM_PROMPT = """\
You review a pull request for an engineering team, through TWO lenses.

LENS 1 — "code": ordinary code review. Correctness bugs, unhandled edge \
cases, resource/security risks, breaking changes. Report only what a senior \
reviewer would genuinely block or question.

LENS 2 — "truth": documentation truth maintenance. You are given CLAIMS that \
the team's documentation asserts, each already linked to a file this PR \
touches. Flag where this diff makes a claim FALSE or STALE (e.g. the docs \
say the service listens on port 5000 and this PR changes it to 8080). Quote \
the claim you are contradicting in your comment body.

RULES
1. Ground every comment in the diff you are given. `path` must be a file in \
   the diff and `line` MUST be a line number that appears in that file's \
   patch hunks. Automated checks DROP comments citing lines outside the \
   diff — never guess or extrapolate a line number.
2. Comment on the CHANGE, not on pre-existing code you merely can see.
3. No style nits, no praise, no restating what the diff obviously does. If \
   the PR is fine, return zero comments and say so in the summary.
4. Each comment sets `lens` to exactly "code" or "truth".
5. `severity` is one of "critical", "high", "medium", "low" — reserve \
   critical/high for real defects or genuine documentation contradictions.
6. `confidence` is your probability (0.0-1.0) that the comment is correct \
   and worth a reviewer's attention. Low-confidence comments are filtered.
7. `body`: 1-3 sentences, specific and actionable. For "truth" comments, \
   name the claim and what the diff changed that breaks it.
8. `summary`: 2-4 sentences for the review header — what this PR does and \
   the headline concerns. Plain prose, no bullet lists.

Respond with JSON only, matching the provided schema.\
"""


def _render_claims(claims: list[dict]) -> str:
    if not claims:
        return (
            "(No documented claims are linked to the files this PR touches — "
            "report truth-lens comments only if the diff plainly contradicts "
            "documentation quoted in the diff itself.)"
        )
    return "\n".join(
        f"- [{c.get('status', 'unchecked')}] \"{c.get('statement', '')}\""
        + (f" (documented in {c['source']})" if c.get("source") else "")
        for c in claims
    )


def _render_files(files: list[dict]) -> str:
    blocks = []
    for f in files:
        patch = f.get("patch")
        header = f"### {f.get('path')} ({f.get('status', 'modified')})"
        if not patch:
            blocks.append(f"{header}\n(no textual diff available — binary or too large)")
            continue
        blocks.append(f"{header}\n```diff\n{patch}\n```")
    return "\n\n".join(blocks)


def build_user_prompt(
    *,
    title: str,
    body: str,
    author: str | None,
    files: list[dict],
    claims: list[dict],
) -> str:
    """`files` are {path, status, patch}; `claims` are {statement, status,
    source} for claims linked to the touched files."""
    return (
        f"Pull request: {title}\n"
        f"Author: {author or 'unknown'}\n\n"
        f"Description:\n{body or '(no description)'}\n\n"
        f"Documented claims linked to the files this PR touches:\n"
        f"{_render_claims(claims)}\n\n"
        f"Diff:\n{_render_files(files)}\n\n"
        "Review this pull request through both lenses now. Cite only lines "
        "present in the patch hunks above."
    )
