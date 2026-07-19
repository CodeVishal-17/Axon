from pathlib import Path

import pytest

from axon.evals.claims_eval import DEFAULT_FIXTURES_PATH, DEFAULT_REPORT_PATH, load_fixtures, run


def test_load_fixtures_from_bundle() -> None:
    fixtures = load_fixtures(DEFAULT_FIXTURES_PATH)
    assert len(fixtures) == 20
    assert fixtures[0].id == "config-cors-origins" or fixtures[0].id
    assert all(f.id for f in fixtures)


def test_run_echo_extractor_generates_report(tmp_path: Path) -> None:
    report_path = tmp_path / "claims_report.json"
    report = run(
        extractor_name="echo",
        fixtures_dir=DEFAULT_FIXTURES_PATH,
        report_path=report_path,
        color=False,
        out=open(tmp_path / "output.txt", "w", encoding="utf-8"),
    )
    assert report_path.exists()
    assert report["summary"]["fixtures"] == 20
    assert report["summary"]["expected_claims"] > 0
    assert report["summary"]["actual_claims"] == report["summary"]["expected_claims"]
    assert report["summary"]["recall"] == 1.0
    assert report["summary"]["precision"] == 1.0
    assert report["summary"]["f1"] == 1.0
