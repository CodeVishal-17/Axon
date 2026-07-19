"""Entity-linker eval harness (DB-free, offline, deterministic).

Run:  make eval-linker
      python -m axon.evals.linker_eval [--json PATH] [--no-color] [--strict]

Exercises the linker's deterministic tiers (PATH, SYMBOL) — the tiers that
must carry ≥70% of claims without LLM help — against gold fixtures: a
snapshot of the demo repository's real file inventory plus 20 hand-written
claims with expected link targets. The embedding/LLM tiers need API keys
and a database; they are covered by tests/test_linking.py with stubbed
providers and measured live once keys are configured.

For every produced link the report prints the WHY — the deterministic rule
that created it — satisfying the explainability contract.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any

from axon.services.linking import (
    Match,
    PathIndex,
    link_by_path,
    link_by_symbol,
)

FIXTURES_PATH = Path(__file__).resolve().parent / "linker" / "fixtures.json"
DEFAULT_REPORT_PATH = Path(__file__).resolve().parent / "out" / "linker_report.json"
MAX_LINKS_PER_CLAIM = 3
SAMPLE_SIZE = 20
SAMPLE_SEED = 42  # fixed: the "random" sample is reproducible run-to-run


def link_one(claim: dict, index: PathIndex) -> tuple[str, list[Match]]:
    """Tier order exactly as the service: PATH, then SYMBOL."""
    matches = link_by_path(claim.get("mentioned_paths", []), index)
    method = "path"
    if not matches:
        matches = link_by_symbol(claim["statement"], index)
        method = "symbol"
    best: dict[str, Match] = {}
    for match in matches:
        if match.path not in best or match.confidence > best[match.path].confidence:
            best[match.path] = match
    chosen = sorted(best.values(), key=lambda m: (-m.confidence, m.path))[
        :MAX_LINKS_PER_CLAIM
    ]
    return (method if chosen else "unresolved"), chosen


def evaluate(fixtures: dict) -> dict[str, Any]:
    index = PathIndex(fixtures["inventory"])
    rows = []
    for claim in fixtures["claims"]:
        method, links = link_one(claim, index)
        expected_paths = set(claim["expected_paths"])
        links_out = [
            {
                "path": link.path,
                "confidence": link.confidence,
                "reason": link.reason,
                "correct": link.path in expected_paths,
            }
            for link in links
        ]
        expected_method = claim["expected_method"]
        method_ok = (
            method == expected_method
            or (expected_method == "embedding_or_unresolved" and method == "unresolved")
        )
        rows.append(
            {
                "id": claim["id"],
                "statement": claim["statement"],
                "method": method,
                "expected_method": expected_method,
                "method_ok": method_ok,
                "links": links_out,
                # correct = every produced link is expected AND (if we were
                # supposed to find something deterministically, we did)
                "correct": all(l["correct"] for l in links_out)
                and (method != "unresolved" or expected_method in ("none", "embedding_or_unresolved")),
            }
        )

    total = len(rows)
    linked = [r for r in rows if r["method"] != "unresolved"]
    by_method: dict[str, int] = {}
    confidence_sum: dict[str, float] = {}
    all_links = []
    for row in rows:
        if row["method"] == "unresolved":
            continue
        by_method[row["method"]] = by_method.get(row["method"], 0) + 1
        for link in row["links"]:
            confidence_sum[row["method"]] = (
                confidence_sum.get(row["method"], 0.0) + link["confidence"]
            )
            all_links.append({**link, "claim_id": row["id"], "method": row["method"]})

    link_counts = {
        m: sum(1 for l in all_links if l["method"] == m) for m in by_method
    }
    summary = {
        "claims_total": total,
        "claims_linked": len(linked),
        "claims_unresolved": total - len(linked),
        "claims_by_method": by_method,
        "links_total": len(all_links),
        "avg_confidence_by_method": {
            m: round(confidence_sum[m] / link_counts[m], 3) for m in by_method
        },
        "pct_linked_without_llm": round(len(linked) / total, 3) if total else 0.0,
        "links_correct": sum(1 for l in all_links if l["correct"]),
        "link_precision": round(
            sum(1 for l in all_links if l["correct"]) / len(all_links), 3
        )
        if all_links
        else 1.0,
        "claims_fully_correct": sum(1 for r in rows if r["correct"]),
        "method_accuracy": round(
            sum(1 for r in rows if r["method_ok"]) / total, 3
        ),
    }
    return {"summary": summary, "rows": rows, "links": all_links}


def render(report: dict, color: bool, out=sys.stdout) -> None:
    green = "\x1b[32m" if color else ""
    red = "\x1b[31m" if color else ""
    dim = "\x1b[2m" if color else ""
    bold = "\x1b[1m" if color else ""
    reset = "\x1b[0m" if color else ""

    for row in report["rows"]:
        flag = f"{green}✓{reset}" if row["correct"] else f"{red}✗{reset}"
        print(f"{flag} {bold}{row['id']}{reset} [{row['method']}]", file=out)
        print(f"    claim: {row['statement']}", file=out)
        if row["links"]:
            for link in row["links"]:
                mark = "✓" if link["correct"] else "✗ WRONG"
                print(
                    f"    → {link['path']}  ({link['confidence']:.2f})  {mark}",
                    file=out,
                )
                print(f"      {dim}why: {link['reason']}{reset}", file=out)
        else:
            print(f"    → {dim}no link created{reset}", file=out)
        print(file=out)

    s = report["summary"]
    print(f"{bold}══ LINKER REPORT ══{reset}", file=out)
    print(
        f"  claims: {s['claims_total']}   linked: {s['claims_linked']}"
        f"   unresolved: {s['claims_unresolved']}",
        file=out,
    )
    print(f"  claims by method: {s['claims_by_method']}", file=out)
    print(f"  avg confidence by method: {s['avg_confidence_by_method']}", file=out)
    print(
        f"  linked without LLM: {s['pct_linked_without_llm']:.0%}"
        f"   (acceptance: ≥70%)",
        file=out,
    )
    print(
        f"  link precision: {s['link_precision']:.0%}"
        f" ({s['links_correct']}/{s['links_total']} links correct)",
        file=out,
    )
    print(f"  method accuracy: {s['method_accuracy']:.0%}", file=out)

    # Deterministic "random" spot-check sample with explanations.
    sample = random.Random(SAMPLE_SEED).sample(
        report["links"], min(SAMPLE_SIZE, len(report["links"]))
    )
    correct = sum(1 for link in sample if link["correct"])
    print(f"\n{bold}══ SPOT-CHECK SAMPLE ({len(sample)} links, seed {SAMPLE_SEED}) ══{reset}", file=out)
    for link in sample:
        mark = f"{green}✓{reset}" if link["correct"] else f"{red}✗{reset}"
        print(
            f"  {mark} [{link['method']}] {link['claim_id']} → {link['path']} "
            f"({link['confidence']:.2f})",
            file=out,
        )
        print(f"      {dim}{link['reason']}{reset}", file=out)
    print(
        f"  sample verdict: {correct}/{len(sample)} clearly correct "
        f"(acceptance: ≥17/20)",
        file=out,
    )


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--fixtures", type=Path, default=FIXTURES_PATH)
    parser.add_argument("--json", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument(
        "--strict", action="store_true",
        help="exit 1 if acceptance thresholds are not met",
    )
    args = parser.parse_args()

    fixtures = json.loads(args.fixtures.read_text(encoding="utf-8"))
    report = evaluate(fixtures)
    render(report, color=not args.no_color and sys.stdout.isatty())

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"\nreport: {args.json}")

    s = report["summary"]
    if args.strict and (
        s["pct_linked_without_llm"] < 0.70 or s["link_precision"] < 0.85
    ):
        sys.exit(1)


if __name__ == "__main__":
    main()
