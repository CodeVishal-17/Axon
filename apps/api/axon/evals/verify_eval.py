"""Drift-verification eval harness.

Run:  make eval-verify
      python -m axon.evals.verify_eval [--provider auto|llm|scripted] [--strict]

Fixtures (axon/evals/verify/fixtures.json) pair claims with CURRENT source
snippets and an expected verdict:

  - seeded drift     → must come back CONTRADICTED (with valid evidence)
  - known-true       → must stay VERIFIED (false positives are the
                       product-killing failure mode)
  - insufficient     → must NOT produce a verdict either way

Every model verdict passes through the SAME evidence gate as production
(quote must appear verbatim in the sources) before scoring — the eval
measures the shipped pipeline, not the raw model.

Providers:
  llm       real verification prompt via the configured provider (needs keys)
  scripted  deterministic self-test: returns the expected verdict with a
            genuine quote from the sources — proves harness + gate plumbing
  auto      llm when keys are configured, else scripted with a notice
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from axon.services.verification import Source, Verdict, quote_in_sources

FIXTURES_PATH = Path(__file__).resolve().parent / "verify" / "fixtures.json"
DEFAULT_REPORT_PATH = Path(__file__).resolve().parent / "out" / "verify_report.json"


def scripted_verdict(case: dict) -> Verdict:
    expected = case["expected"]
    if expected == "INSUFFICIENT_EVIDENCE":
        return Verdict(
            verdict=expected, confidence=0.6, evidence_quote=None,
            evidence_path=None, explanation="self-test: nothing decisive shown",
        )
    first_source = case["sources"][0]
    quote = next(
        line for line in first_source["content"].splitlines() if line.strip()
    )
    return Verdict(
        verdict=expected,
        confidence=0.92 if expected == "CONTRADICTED" else 0.9,
        evidence_quote=quote,
        evidence_path=first_source["path"],
        explanation=f"self-test {expected.lower()} verdict",
    )


def llm_verdict(case: dict) -> Verdict:
    from axon.llm import provider as llm  # noqa: PLC0415
    from axon.llm.prompts.drift_verification import (  # noqa: PLC0415
        SYSTEM_PROMPT,
        build_user_prompt,
    )

    return llm.complete(
        build_user_prompt(
            case["claim"]["statement"],
            case["claim"]["claim_type"],
            [(s["path"], s["content"]) for s in case["sources"]],
        ),
        Verdict,
        system=SYSTEM_PROMPT,
    )


def resolve_provider(name: str):
    if name == "scripted":
        return "scripted", scripted_verdict
    from axon.services.claims import llm_configured  # noqa: PLC0415

    if not llm_configured():
        if name == "llm":
            raise SystemExit(
                "No LLM API key configured — set OPENAI_API_KEY (and "
                "ANTHROPIC_API_KEY when LLM_PROVIDER=anthropic), then re-run."
            )
        print(
            "note: no LLM API key configured — falling back to 'scripted' "
            "(pipeline self-test). Set OPENAI_API_KEY for the real eval.\n",
            file=sys.stderr,
        )
        return "scripted", scripted_verdict
    return "llm", llm_verdict


def evaluate(fixtures: dict, get_verdict) -> dict[str, Any]:
    rows = []
    for case in fixtures["cases"]:
        sources = [Source(s["path"], s["content"], 1) for s in case["sources"]]
        verdict = get_verdict(case)

        # production evidence gate
        gated = verdict.verdict
        gate_downgraded = False
        if verdict.verdict in ("VERIFIED", "CONTRADICTED"):
            if quote_in_sources(verdict.evidence_quote or "", sources) is None:
                gated = "INSUFFICIENT_EVIDENCE"
                gate_downgraded = True

        rows.append(
            {
                "id": case["id"],
                "expected": case["expected"],
                "model_verdict": verdict.verdict,
                "final_verdict": gated,
                "gate_downgraded": gate_downgraded,
                "confidence": verdict.confidence,
                "evidence_quote": verdict.evidence_quote,
                "evidence_path": verdict.evidence_path,
                "explanation": verdict.explanation,
                "correct": gated == case["expected"],
            }
        )

    drift = [r for r in rows if r["expected"] == "CONTRADICTED"]
    true_ = [r for r in rows if r["expected"] == "VERIFIED"]
    insufficient = [r for r in rows if r["expected"] == "INSUFFICIENT_EVIDENCE"]
    false_positives = [
        r for r in rows
        if r["expected"] != "CONTRADICTED" and r["final_verdict"] == "CONTRADICTED"
    ]
    contradicted_with_evidence = [
        r for r in rows
        if r["final_verdict"] == "CONTRADICTED" and r["evidence_quote"]
    ]
    contradicted_total = [r for r in rows if r["final_verdict"] == "CONTRADICTED"]

    summary = {
        "cases": len(rows),
        "accuracy": round(sum(r["correct"] for r in rows) / len(rows), 3),
        "drift_detected": sum(
            1 for r in drift if r["final_verdict"] == "CONTRADICTED"
        ),
        "drift_total": len(drift),
        "known_true_kept": sum(
            1 for r in true_ if r["final_verdict"] == "VERIFIED"
        ),
        "known_true_total": len(true_),
        "insufficient_correct": sum(1 for r in insufficient if r["correct"]),
        "insufficient_total": len(insufficient),
        "false_positives": len(false_positives),
        "evidence_gate_downgrades": sum(1 for r in rows if r["gate_downgraded"]),
        "contradicted_with_evidence": (
            f"{len(contradicted_with_evidence)}/{len(contradicted_total)}"
        ),
    }
    return {"summary": summary, "rows": rows}


def render(report: dict, color: bool, out=sys.stdout) -> None:
    green = "\x1b[32m" if color else ""
    red = "\x1b[31m" if color else ""
    dim = "\x1b[2m" if color else ""
    bold = "\x1b[1m" if color else ""
    reset = "\x1b[0m" if color else ""

    for row in report["rows"]:
        flag = f"{green}✓{reset}" if row["correct"] else f"{red}✗{reset}"
        gate = "  [evidence-gate downgrade]" if row["gate_downgraded"] else ""
        print(
            f"{flag} {bold}{row['id']}{reset}: expected {row['expected']}, "
            f"got {row['final_verdict']} ({row['confidence']:.2f}){gate}",
            file=out,
        )
        if row["evidence_quote"]:
            print(
                f"    {dim}evidence [{row['evidence_path']}]: "
                f"{row['evidence_quote'][:100]}{reset}",
                file=out,
            )
        print(f"    {dim}{row['explanation'][:140]}{reset}", file=out)

    s = report["summary"]
    print(f"\n{bold}══ VERIFY REPORT ══{reset}", file=out)
    print(
        f"  seeded drift detected:  {s['drift_detected']}/{s['drift_total']}"
        f"   (acceptance: all)",
        file=out,
    )
    print(
        f"  known-true kept:        {s['known_true_kept']}/{s['known_true_total']}"
        f"   (acceptance: all — false positives kill trust)",
        file=out,
    )
    print(
        f"  insufficient handled:   {s['insufficient_correct']}/{s['insufficient_total']}",
        file=out,
    )
    print(f"  false positives:        {s['false_positives']}", file=out)
    print(
        f"  contradicted w/ evidence: {s['contradicted_with_evidence']}"
        f"   (acceptance: all)",
        file=out,
    )
    print(
        f"  evidence-gate downgrades: {s['evidence_gate_downgrades']}"
        f"   overall accuracy: {s['accuracy']:.0%}",
        file=out,
    )


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--provider", default="auto", choices=["auto", "llm", "scripted"]
    )
    parser.add_argument("--fixtures", type=Path, default=FIXTURES_PATH)
    parser.add_argument("--json", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument(
        "--strict", action="store_true",
        help="exit 1 unless all drift detected, no false positives, and "
             "every contradiction carries evidence",
    )
    args = parser.parse_args()

    fixtures = json.loads(args.fixtures.read_text(encoding="utf-8"))
    name, get_verdict = resolve_provider(args.provider)
    report = evaluate(fixtures, get_verdict)
    print(f"provider: {name}\n")
    render(report, color=not args.no_color and sys.stdout.isatty())

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"\nreport: {args.json}")

    s = report["summary"]
    with_evidence, total = s["contradicted_with_evidence"].split("/")
    if args.strict and (
        s["drift_detected"] < s["drift_total"]
        or s["false_positives"] > 0
        or with_evidence != total
    ):
        sys.exit(1)


if __name__ == "__main__":
    main()
