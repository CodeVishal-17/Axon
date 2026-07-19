"""Claim-extraction eval harness.

Run:  make eval-claims               (from repo root)
      python -m axon.evals.claims_eval [--extractor auto|llm|echo|empty]

Fixtures live in apps/api/evals/claims/fixtures.json (or a directory of
JSON fixture files) — real doc sections and issue-style documents from the
demo repository, each with hand-written expected atomic claims (the gold
standard the extraction prompt is tuned against). Fixture schema:

    {
      "id": "readme-quickstart-docker",
      "source": {
        "kind": "doc_section" | "issue",
        "doc_path": "README.md" | null,
        "path": "README.md#quickstart-docker-everything" | "issue #12",
        "start_line": 19, "end_line": 26,        # null for issues
        "text": "..."
      },
      "expected_claims": [
        {
          "statement": "...",                     # atomic, self-contained
          "claim_type": "behavior|architecture|process|status",
          "mentioned_paths": ["apps/api"],       # may be []
          "anchor": {"path": "README.md", "start_line": 20, "end_line": 22}
        }
      ],
      "notes": "why these claims / judgment calls"
    }

Extractors:
  llm    the real extraction service (axon.services.claims.extract_for_eval,
         lands in T2.2). Contract: (text, doc_path, kind, start_line) ->
         list[dict] shaped like expected_claims entries.
  echo   returns the expected claims verbatim — self-test of the harness
         pipeline; byte-identical output across runs (determinism check).
  empty  returns nothing — renders the all-MISSING regression view.
  auto   llm if importable, else echo with a notice (default, so the make
         target works before T2.2 lands).

Statement matching is fuzzy (normalized SequenceMatcher, greedy one-to-one
assignment) because two phrasings of the same claim must count as a match;
type and anchor correctness are reported per matched pair.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

VALID_CLAIM_TYPES = {"behavior", "architecture", "process", "status"}
MATCH_THRESHOLD = 0.55

_PKG_ROOT = Path(__file__).resolve().parent
DEFAULT_FIXTURES_DIR = _PKG_ROOT / "claims"
DEFAULT_FIXTURES_PATH = DEFAULT_FIXTURES_DIR / "fixtures.json"
DEFAULT_REPORT_PATH = _PKG_ROOT / "out" / "claims_report.json"

Extractor = Callable[["Fixture"], list[dict[str, Any]]]


class FixtureError(ValueError):
    pass


@dataclass(frozen=True)
class Fixture:
    id: str
    kind: str
    path: str
    doc_path: str | None
    start_line: int | None
    end_line: int | None
    text: str
    expected: list[dict[str, Any]]
    notes: str


# --- Loading -------------------------------------------------------------


def _validate_claim(claim: dict, where: str) -> None:
    for key in ("statement", "claim_type", "mentioned_paths", "anchor"):
        if key not in claim:
            raise FixtureError(f"{where}: expected claim missing key {key!r}")
    if claim["claim_type"] not in VALID_CLAIM_TYPES:
        raise FixtureError(
            f"{where}: invalid claim_type {claim['claim_type']!r} "
            f"(valid: {sorted(VALID_CLAIM_TYPES)})"
        )
    if not isinstance(claim["mentioned_paths"], list):
        raise FixtureError(f"{where}: mentioned_paths must be a list")
    if not isinstance(claim["anchor"], dict):
        raise FixtureError(f"{where}: anchor must be an object")


def load_fixtures(directory: Path = DEFAULT_FIXTURES_PATH) -> list[Fixture]:
    if directory.is_dir():
        files = sorted(directory.glob("*.json"))
    elif directory.is_file():
        files = [directory]
    else:
        raise FixtureError(f"fixture path does not exist: {directory}")
    if not files:
        raise FixtureError(f"no fixture files found in {directory}")
    fixtures: list[Fixture] = []
    for file in files:
        raw = json.loads(file.read_text(encoding="utf-8"))
        entries = raw if isinstance(raw, list) else [raw]
        for index, entry in enumerate(entries):
            label = f"{file.name}[{index}]" if len(entries) > 1 else file.name
            for key in ("id", "source", "expected_claims"):
                if key not in entry:
                    raise FixtureError(f"{label}: missing key {key!r}")
            source = entry["source"]
            if source.get("kind") not in ("doc_section", "issue"):
                raise FixtureError(f"{label}: source.kind must be doc_section|issue")
            if not source.get("text", "").strip():
                raise FixtureError(f"{label}: source.text is empty")
            for i, claim in enumerate(entry["expected_claims"]):
                _validate_claim(claim, f"{label}[{i}]")
            fixtures.append(
                Fixture(
                    id=entry["id"],
                    kind=source["kind"],
                    path=source.get("path", ""),
                    doc_path=source.get("doc_path"),
                    start_line=source.get("start_line"),
                    end_line=source.get("end_line"),
                    text=source["text"],
                    expected=entry["expected_claims"],
                    notes=entry.get("notes", ""),
                )
            )
    fixtures.sort(key=lambda f: f.id)
    ids = [f.id for f in fixtures]
    if len(ids) != len(set(ids)):
        raise FixtureError("duplicate fixture ids")
    return fixtures


# --- Matching ------------------------------------------------------------


def _normalize(statement: str) -> str:
    cleaned = "".join(
        ch.lower() if ch.isalnum() or ch.isspace() else " " for ch in statement
    )
    return " ".join(cleaned.split())


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def _anchor_ok(expected: dict, actual: dict) -> bool:
    """Path must agree; line ranges must overlap (when both provided)."""
    exp_anchor, act_anchor = expected.get("anchor") or {}, actual.get("anchor") or {}
    if exp_anchor.get("path") != act_anchor.get("path"):
        return False
    exp_start, exp_end = exp_anchor.get("start_line"), exp_anchor.get("end_line")
    act_start, act_end = act_anchor.get("start_line"), act_anchor.get("end_line")
    if None in (exp_start, exp_end, act_start, act_end):
        return True  # lines not asserted on one side
    return act_start <= exp_end and act_end >= exp_start


def match_claims(
    expected: list[dict], actual: list[dict], threshold: float = MATCH_THRESHOLD
) -> dict[str, Any]:
    """Greedy one-to-one assignment by descending statement similarity."""
    pairs = sorted(
        (
            (similarity(e["statement"], a["statement"]), ei, ai)
            for ei, e in enumerate(expected)
            for ai, a in enumerate(actual)
        ),
        key=lambda t: (-t[0], t[1], t[2]),
    )
    matched_e: set[int] = set()
    matched_a: set[int] = set()
    matches = []
    for score, ei, ai in pairs:
        if score < threshold or ei in matched_e or ai in matched_a:
            continue
        matched_e.add(ei)
        matched_a.add(ai)
        matches.append(
            {
                "expected_index": ei,
                "actual_index": ai,
                "score": round(score, 3),
                "type_match": expected[ei]["claim_type"]
                == actual[ai].get("claim_type"),
                "anchor_ok": _anchor_ok(expected[ei], actual[ai]),
            }
        )
    matches.sort(key=lambda m: m["expected_index"])
    return {
        "matches": matches,
        "missing": [i for i in range(len(expected)) if i not in matched_e],
        "unexpected": [i for i in range(len(actual)) if i not in matched_a],
    }


# --- Extractors ----------------------------------------------------------


def get_extractor(name: str) -> tuple[str, Extractor]:
    if name == "echo":
        return "echo", lambda f: [dict(c) for c in f.expected]
    if name == "empty":
        return "empty", lambda f: []
    if name in ("llm", "auto"):
        try:
            from axon.services.claims import extract_for_eval  # noqa: PLC0415
        except ImportError:
            if name == "llm":
                raise SystemExit(
                    "The LLM extractor (axon.services.claims.extract_for_eval) "
                    "is not implemented yet — it lands with T2.2.\n"
                    "Use --extractor echo (pipeline self-test) or empty."
                )
            print(
                "note: LLM extractor not available yet (lands in T2.2) — "
                "falling back to 'echo' (harness self-test).\n",
                file=sys.stderr,
            )
            return get_extractor("echo")
        return "llm", lambda f: extract_for_eval(
            text=f.text, doc_path=f.doc_path, kind=f.kind, start_line=f.start_line
        )
    raise SystemExit(f"unknown extractor {name!r}")


# --- Rendering -----------------------------------------------------------


class Palette:
    def __init__(self, enabled: bool) -> None:
        self.green = "\x1b[32m" if enabled else ""
        self.red = "\x1b[31m" if enabled else ""
        self.yellow = "\x1b[33m" if enabled else ""
        self.dim = "\x1b[2m" if enabled else ""
        self.bold = "\x1b[1m" if enabled else ""
        self.reset = "\x1b[0m" if enabled else ""


def render_fixture(
    fixture: Fixture, actual: list[dict], result: dict, p: Palette
) -> str:
    lines = [
        f"{p.bold}── {fixture.id}{p.reset} {p.dim}({fixture.path}){p.reset}"
    ]
    for match in result["matches"]:
        expected = fixture.expected[match["expected_index"]]
        got = actual[match["actual_index"]]
        flags = []
        if not match["type_match"]:
            flags.append(f"type {got.get('claim_type')}≠{expected['claim_type']}")
        if not match["anchor_ok"]:
            flags.append("anchor wrong")
        flag_text = f"  {p.yellow}[{', '.join(flags)}]{p.reset}" if flags else ""
        lines.append(
            f"  {p.green}✓ MATCH{p.reset} ({match['score']:.2f}) "
            f"[{expected['claim_type']}]{flag_text}"
        )
        lines.append(f"      expected: {expected['statement']}")
        lines.append(f"      actual:   {got['statement']}")
    for i in result["missing"]:
        expected = fixture.expected[i]
        lines.append(f"  {p.red}✗ MISSING{p.reset} [{expected['claim_type']}]")
        lines.append(f"      expected: {expected['statement']}")
    for i in result["unexpected"]:
        got = actual[i]
        lines.append(
            f"  {p.yellow}? UNEXPECTED{p.reset} [{got.get('claim_type', '?')}]"
        )
        lines.append(f"      actual:   {got.get('statement', '')}")
    if not fixture.expected and not actual:
        lines.append(f"  {p.green}✓ correctly extracted nothing{p.reset}")
    return "\n".join(lines)


def summarize(per_fixture: list[dict]) -> dict[str, Any]:
    total_expected = sum(r["expected_count"] for r in per_fixture)
    total_actual = sum(r["actual_count"] for r in per_fixture)
    total_matched = sum(len(r["result"]["matches"]) for r in per_fixture)
    type_correct = sum(
        1 for r in per_fixture for m in r["result"]["matches"] if m["type_match"]
    )
    anchor_correct = sum(
        1 for r in per_fixture for m in r["result"]["matches"] if m["anchor_ok"]
    )
    recall = total_matched / total_expected if total_expected else 1.0
    precision = total_matched / total_actual if total_actual else 1.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    return {
        "fixtures": len(per_fixture),
        "expected_claims": total_expected,
        "actual_claims": total_actual,
        "matched": total_matched,
        "recall": round(recall, 3),
        "precision": round(precision, 3),
        "f1": round(f1, 3),
        "type_accuracy": round(type_correct / total_matched, 3) if total_matched else 1.0,
        "anchor_accuracy": round(anchor_correct / total_matched, 3) if total_matched else 1.0,
    }


# --- Entry point ---------------------------------------------------------


def run(
    extractor_name: str = "auto",
    fixtures_dir: Path = DEFAULT_FIXTURES_PATH,
    report_path: Path | None = DEFAULT_REPORT_PATH,
    color: bool = True,
    out=sys.stdout,
) -> dict[str, Any]:
    fixtures = load_fixtures(fixtures_dir)
    resolved_name, extractor = get_extractor(extractor_name)
    palette = Palette(color)

    per_fixture = []
    for fixture in fixtures:
        actual = extractor(fixture)
        result = match_claims(fixture.expected, actual)
        per_fixture.append(
            {
                "fixture_id": fixture.id,
                "source_path": fixture.path,
                "expected_count": len(fixture.expected),
                "actual_count": len(actual),
                "actual_claims": actual,
                "result": result,
            }
        )
        print(render_fixture(fixture, actual, result, palette), file=out)
        print(file=out)

    summary = summarize(per_fixture)
    p = palette
    print(f"{p.bold}══ SUMMARY ({resolved_name} extractor) ══{p.reset}", file=out)
    print(
        f"  fixtures: {summary['fixtures']}   expected: {summary['expected_claims']}"
        f"   extracted: {summary['actual_claims']}   matched: {summary['matched']}",
        file=out,
    )
    print(
        f"  recall: {summary['recall']:.1%}   precision: {summary['precision']:.1%}"
        f"   f1: {summary['f1']:.3f}",
        file=out,
    )
    print(
        f"  type accuracy: {summary['type_accuracy']:.1%}"
        f"   anchor accuracy: {summary['anchor_accuracy']:.1%}",
        file=out,
    )

    report = {"extractor": resolved_name, "summary": summary, "fixtures": per_fixture}
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"\nreport: {report_path}", file=out)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--extractor", default="auto", choices=["auto", "llm", "echo", "empty"]
    )
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES_PATH)
    parser.add_argument("--json", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 unless recall and precision are both 100%%",
    )
    args = parser.parse_args()

    report = run(
        extractor_name=args.extractor,
        fixtures_dir=args.fixtures,
        report_path=args.json,
        color=not args.no_color and sys.stdout.isatty(),
    )
    if args.strict and (
        report["summary"]["recall"] < 1.0 or report["summary"]["precision"] < 1.0
    ):
        sys.exit(1)


if __name__ == "__main__":
    main()
