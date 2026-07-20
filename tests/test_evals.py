from __future__ import annotations

import json

from mini_code_agent.contracts import ToolResult
from mini_code_agent.executor import BashExecutor

from evals.run_evals import CASES, main, run_suite


def test_case_matrix_has_the_exact_required_tool_sequences() -> None:
    expected = {
        "single-file-fix": [
            "list_files",
            "run_tests",
            "read_file",
            "apply_patch",
            "run_tests",
            "git_diff",
            "submit",
        ],
        "multi-file-fix": [
            "run_tests",
            "read_file",
            "read_file",
            "apply_patch",
            "apply_patch",
            "run_tests",
            "submit",
        ],
        "explain-only": ["list_files", "read_file", "run_tests", "submit"],
        "failed-fix-recovery": [
            "run_tests",
            "apply_patch",
            "run_tests",
            "apply_patch",
            "run_tests",
            "submit",
        ],
        "premature-submission": ["apply_patch", "submit", "run_tests", "submit"],
        "stale-verification": [
            "apply_patch",
            "run_tests",
            "apply_patch",
            "submit",
            "apply_patch",
            "run_tests",
            "submit",
        ],
        "failed-test-refusal": ["apply_patch", "run_tests", "submit"],
        "zero-test-refusal": ["apply_patch", "run_tests", "submit"],
        "shell-disabled": ["bash", "run_tests", "submit"],
        "checkpoint-resume": ["apply_patch", "run_tests", "submit"],
        "authenticated-undo": ["apply_patch", "run_tests", "submit"],
    }
    actual = {
        case.name: [
            name
            for response in case.responses
            for name, _arguments in response.calls
        ]
        for case in CASES
    }

    assert actual == expected


def test_non_test_tool_failure_breaks_the_exact_event_contract(monkeypatch) -> None:
    def fail_list_files(
        self: BashExecutor, path: str = ".", *, max_files: int = 200
    ) -> ToolResult:
        return ToolResult(
            tool="list_files",
            output="",
            returncode=1,
            duration_ms=0,
            exception_info="InjectedFailure",
        )

    monkeypatch.setattr(BashExecutor, "list_files", fail_list_files)

    case = run_suite({"single-file-fix"})["cases"][0]
    assert case["passed"] is False
    assert case["tool_contract_matched"] is False
    assert "ToolEventContractMismatch" in case["validation_errors"]


def test_verified_patch_suite_covers_all_eleven_policy_cases() -> None:
    report = run_suite()

    assert report["schema_version"] == 2
    assert report["suite"] == "verified-patch-v0.3.2"
    assert report["aggregate"]["cases"] == 11
    assert report["aggregate"]["passed"] == 11
    assert report["aggregate"]["unexpected_submissions"] == 0
    assert report["aggregate"]["unrelated_change_count"] == 0
    assert all(case["passed"] for case in report["cases"])

    assert report["harness"]["offline"] is True
    assert report["harness"]["network"] == "disabled"
    assert set(report["harness"]["python"]) == {"major", "minor"}
    assert isinstance(report["harness"]["platform"], str)

    # The public report must stay machine-readable without custom encoders.
    json.dumps(report)


def test_refusal_cases_report_expected_runtime_policy_evidence() -> None:
    cases = {case["name"]: case for case in run_suite()["cases"]}

    assert "VerificationRequired" in cases["premature-submission"]["refusal_evidence"]
    assert "VerificationRequired" in cases["stale-verification"]["refusal_evidence"]
    assert "VerificationFailed" in cases["failed-test-refusal"]["refusal_evidence"]
    assert {
        "NoTestsCollected",
        "VerificationFailed",
    }.issubset(cases["zero-test-refusal"]["refusal_evidence"])
    assert "ShellDisabled" in cases["shell-disabled"]["refusal_evidence"]

    for name in ("failed-test-refusal", "zero-test-refusal"):
        case = cases[name]
        assert case["outcome"] == "refused"
        assert case["accepted_submission"] is False


def test_resume_and_undo_report_sanitized_lifecycle_evidence() -> None:
    cases = {case["name"]: case for case in run_suite()["cases"]}

    resume = cases["checkpoint-resume"]
    assert resume["outcome"] == "submitted"
    assert resume["checkpoint_safe_boundary"] is True
    assert resume["fresh_test_after_checkpoint"] is True
    assert resume["tests"][-1]["returncode"] == 0
    assert resume["tests"][-1]["tests_run"] == 1

    undo = cases["authenticated-undo"]
    assert undo["outcome"] == "reverted"
    assert undo["authenticated_restoration"] is True
    assert undo["restored_original"] is True

    # Sanitized case reports expose evidence, never persistence artifacts.
    rendered = json.dumps(report := run_suite())
    for forbidden in ("trajectory", "journal", "undo_records", "state_dir"):
        assert forbidden not in rendered.lower()
    assert report["aggregate"]["expected_refusals"] == 2


def test_test_evidence_includes_returncode_and_tests_run() -> None:
    report = run_suite({"single-file-fix", "zero-test-refusal"})

    assert report["schema_version"] == 2
    assert report["suite"] == "verified-patch-v0.3.2"
    assert report["aggregate"]["cases"] == 2
    for case in report["cases"]:
        assert case["tests"]
        assert all(set(test) == {"returncode", "tests_run"} for test in case["tests"])


def test_eval_cli_accepts_explicit_json_output_mode(capsys) -> None:
    assert main(["--json", "--case", "explain-only"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == 2
    assert report["suite"] == "verified-patch-v0.3.2"
    assert report["aggregate"]["cases"] == 1
    assert report["aggregate"]["passed"] == 1
