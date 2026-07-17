from __future__ import annotations

import json

from evals.run_evals import main, run_suite


def test_offline_behavior_baseline_covers_three_core_categories() -> None:
    report = run_suite()

    assert report["schema_version"] == 1
    assert report["aggregate"]["cases"] == 3
    assert report["aggregate"]["success"] == 3
    assert report["aggregate"]["verified"] == 3
    assert report["aggregate"]["unrelated_changes"] == 0
    assert {case["category"] for case in report["cases"]} == {
        "single_file_fix",
        "no_change_explanation",
        "failure_recovery",
    }
    assert all(case["success"] and case["verified"] for case in report["cases"])
    assert all(case["tool_calls"] >= case["steps"] for case in report["cases"])
    assert all(not case["unrelated_changes"] for case in report["cases"])

    explain = next(case for case in report["cases"] if case["name"] == "explain-only")
    recovery = next(
        case for case in report["cases"] if case["name"] == "failed-fix-recovery"
    )
    assert explain["workspace_changes"] == {
        "created": [],
        "deleted": [],
        "modified": [],
    }
    assert recovery["recovered_from_failure"] is True
    assert recovery["test_returncodes"][-1] == 0

    # The public report must stay machine-readable without custom encoders.
    json.dumps(report)


def test_eval_cli_accepts_explicit_json_output_mode(capsys) -> None:
    assert main(["--json", "--case", "explain-only"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["aggregate"]["cases"] == 1
    assert report["aggregate"]["success"] == 1
